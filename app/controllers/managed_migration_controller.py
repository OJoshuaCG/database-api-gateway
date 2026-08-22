"""
Controller de aplicación de migraciones sobre BDs gestionadas (TOCA el motor).

Orquesta el ``MigrationRunner`` (Alembic embebido) y el inventario del gateway:
- ``status``   : versión actual de la BD (leída de ``_gw_v_{slug}``) vs. pendientes.
- ``apply``    : aplica pendientes; registra ``database_migration_history`` y
                 actualiza ``managed_database.model_version``.
- ``rollback`` : revierte la última (409 si la versión actual no tiene ``down_sql``).
- ``stamp``    : marca versión sin ejecutar (BDs pre-existentes).
- ``apply_all``: aplica a TODAS las BDs del blueprint (síncrono, acotado; el job
                 asíncrono real es del Plan 06).

Integridad: antes de tocar el motor se re-valida el ``checksum`` de cada migración
(detecta alteración directa en la BD del gateway).
"""

import re

from sqlalchemy import or_ as sa_or

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.core.database import Database
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    MIGRATION_CAPTURE_ENABLED,
)
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.database_model import DatabaseModel
from app.models.environment import Environment
from app.models.enums import EngineType, MigrationStatus, ProvisionStatus
from app.models.managed_database import ManagedDatabase
from app.models.model_migration import ModelMigration
from app.models.model_migration_statement import ModelMigrationStatement
from app.services import audit
from app.services import environment_catalog as ecodes
from app.services.db_admin import migration_facts, migration_progress, migration_results
from app.services.db_admin.migration_integrity import compute_checksum, version_sort_key
from app.services.db_admin.migrations import (
    ManifestStatement,
    MigrationResult,
    MigrationRunner,
    MigrationSpec,
)
from app.services.db_admin.identifiers import references_gateway_internal_table
from app.services.db_admin.sql_dialect import SqlTranslator, split_sql_statements

logger = get_logger(__name__)


class ManagedMigrationController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)
        self.runner = MigrationRunner()

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Carga de contexto                                                   #
    # ------------------------------------------------------------------ #
    def _load_context(self, session, db_id: int):
        """Devuelve (managed_db, server, model) validando blueprint asignado."""
        md = session.get(ManagedDatabase, db_id)
        if not md:
            raise AppHttpException(
                message="Base de datos gestionada no encontrada.",
                status_code=404,
                context={"managed_database_id": db_id},
            )
        if md.model_id is None:
            raise AppHttpException(
                message="La BD no tiene un blueprint asignado; nada que migrar.",
                status_code=422,
                context={"managed_database_id": db_id},
            )
        server = get_server_or_404(session, md.server_id)
        model = session.get(DatabaseModel, md.model_id)
        if model is None:
            raise AppHttpException(
                message="El blueprint asignado a la BD ya no existe.",
                status_code=409,
                context={"managed_database_id": db_id, "model_id": md.model_id},
            )
        return md, server, model

    @staticmethod
    def _load_specs(session, model_id: int) -> list[MigrationSpec]:
        rows = (
            session.query(ModelMigration)
            .filter(ModelMigration.model_id == model_id)
            .all()
        )
        # MANIFIESTO de sentencias por migración (una sola consulta para todas). Es
        # OPCIONAL: las migraciones escritas a mano no lo tienen y siguen funcionando por
        # el camino del splitter.
        manifests: dict[int, list[ModelMigrationStatement]] = {}
        if rows:
            for st in (
                session.query(ModelMigrationStatement)
                .filter(
                    ModelMigrationStatement.model_migration_id.in_([r.id for r in rows])
                )
                .order_by(ModelMigrationStatement.seq.asc())
                .all()
            ):
                manifests.setdefault(st.model_migration_id, []).append(st)
        specs = [
            MigrationSpec(
                id=r.id,
                version=r.version,
                name=r.name,
                up_sql=r.up_sql,
                up_sql_mysql=r.up_sql_mysql,
                up_sql_postgresql=r.up_sql_postgresql,
                down_sql=r.down_sql,
                checksum=r.checksum,
                kind=r.kind,
                has_non_portable=r.has_non_portable,
                source_engine=r.source_engine,
                capture_selects=r.capture_selects,
                manifest=tuple(
                    ManifestStatement(
                        seq=st.seq,
                        up_sql=st.up_sql,
                        down_sql=st.down_sql,
                        down_confirmed=st.down_confirmed,
                        object_type=st.object_type,
                        object_name=st.object_name,
                        destructive=st.destructive,
                    )
                    for st in manifests.get(r.id, [])
                ),
            )
            for r in rows
        ]
        # Orden NUMÉRICO de versión (no lexicográfico): status/latest dependen de él.
        specs.sort(key=lambda s: version_sort_key(s.version))
        return specs

    @staticmethod
    def _verify_integrity(specs: list[MigrationSpec]) -> None:
        for spec in specs:
            expected = compute_checksum(
                spec.up_sql, spec.up_sql_mysql, spec.up_sql_postgresql,
                spec.down_sql, spec.version,
            )
            if expected != spec.checksum:
                raise AppHttpException(
                    message=(
                        f"Integridad: la migración {spec.version} fue alterada "
                        "(checksum no coincide). Se aborta para no aplicar SQL no verificado."
                    ),
                    status_code=409,
                    context={"version": spec.version},
                )

    # ------------------------------------------------------------------ #
    # Estado                                                              #
    # ------------------------------------------------------------------ #
    def status(self, db_id: int) -> dict:
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            slug = model.slug
            db_name, model_id = md.name, model.id
            engine = EngineType(engine_value(server))
            target = build_target(server)
        finally:
            session.close()

        current = self.runner.get_current_version(target, db_name, slug)
        latest = specs[-1].version if specs else None
        pending = self.runner.compute_pending(current, specs)
        # Aplicación PARCIAL pendiente: ``current_version`` NO la refleja (Alembic no
        # alcanzó a registrarla), así que sin este campo el estado se lee como sano y el
        # admin descubre el problema recién cuando el rollback se niega.
        incomplete = migration_progress.incomplete_progress_for_database(db_id, direction="up")
        by_id = {s.id: s for s in specs}
        return {
            "managed_database_id": db_id,
            "model_id": model_id,
            "slug": slug,
            "current_version": current,
            "latest_available": latest,
            "pending_count": len(pending),
            "pending_versions": [s.version for s in pending],
            "has_partial_application": bool(incomplete),
            "partial_application": [
                self._partial_entry(by_id.get(row["model_migration_id"]), engine, row)
                for row in incomplete
            ],
        }

    @classmethod
    def _partial_entry(cls, spec: MigrationSpec | None, engine: EngineType, row: dict) -> dict:
        """
        Una entrada de ``partial_application``, ya resuelta con si se puede reconciliar.

        El frontend necesita saberlo ANTES de ofrecer el botón: sin manifiesto de
        sentencias la reconciliación automática no es posible y la salida es
        ``stamp?force=true`` tras reconciliar a mano.
        """
        plan = cls._reconcile_plan(spec, engine, row) if spec is not None else None
        return {
            "version": spec.version if spec is not None else None,
            "model_migration_id": row["model_migration_id"],
            "applied_statements": row["last_statement_index"],
            "total_statements": row["total_statements"],
            "reconcilable": bool(plan and plan["reconcilable"]),
            "reason": (
                None
                if plan and plan["reconcilable"]
                else (
                    plan["reason"] if plan
                    else "la migración ya no existe en el blueprint"
                )
            ),
            "statements_to_undo": plan["count"] if plan else 0,
        }

    # ------------------------------------------------------------------ #
    # Guard de entorno: DDL destructivo                                   #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _env_policy_for(session, db_ids: list[int]) -> dict[int, tuple[bool, str]]:
        """
        Política de entorno por BD: ``{db_id: (bloquea_destructivas, slug)}``.

        Se resuelve UNA vez por lote y devuelve **valores planos, no filas ORM**: en
        ``apply_all`` la sesión se cierra antes del bucle, así que instancias del ORM quedarían
        desligadas y cualquier acceso a un atributo explotaría. Mismo criterio que el cacheo de
        ``ServerTarget`` por servidor, y que el desempaquetado temprano de ``md.name``/
        ``md.server_id`` que ya hace ``apply``.

        Las BDs sin entorno NO aparecen en el dict. Eso las deja PERMISIVAS, y es deliberado:
        romper todos los ``apply-all`` que hoy funcionan no es aceptable, y toda BD existente
        nació antes de que existiera esta columna.

        ATENCIÓN — esta asimetría no se traslada. Si algún día se agrega el gate de consultas
        para agentes (plan 11 §6), ahí un entorno sin asignar debe NEGAR, porque no hay
        comportamiento previo que romper y un default permisivo dejaría toda BD sin clasificar
        consultable. No "unifiques" los dos comportamientos.
        """
        if not db_ids:
            return {}
        rows = (
            session.query(
                ManagedDatabase.id,
                Environment.blocks_destructive_migrations,
                Environment.slug,
            )
            .join(Environment, Environment.id == ManagedDatabase.environment_id)
            .filter(ManagedDatabase.id.in_(db_ids))
            .all()
        )
        return {db_id: (bool(blocks), slug) for db_id, blocks, slug in rows}

    def _destructive_versions(
        self, specs: list[MigrationSpec], engine: EngineType
    ) -> dict[str, tuple[str, ...]]:
        """
        Qué versiones son destructivas y por qué, para ESTE motor.

        Dos decisiones acá, y las dos son la corrección de un fail-open real:

        1. **NO se lee el manifiesto como fuente primaria.** ``ModelMigrationStatement`` tiene
           una columna ``destructive`` que parecería la fuente natural, pero esas filas CASI
           NUNCA EXISTEN: el manifiesto es opcional y lo escribe únicamente la adopción de un
           diff estructural (``_write_statement_manifest`` corta con
           ``if not statements or not source_engine: return``, y ``ModelMigrationCreate`` no
           tiene ni campo ``statements``). O sea: una migración escrita a mano con
           ``up_sql = "DROP TABLE clientes"`` no produce ni una fila, y un guard basado en el
           manifiesto la dejaría pasar. La fuente es ``migration_facts.analyze``, que decide
           por AST y es lo mismo que el listado ya publica como insignia ``destructive``.
        2. **Se analiza el SQL RESUELTO PARA EL MOTOR**, vía ``select_up_sql``, y no
           ``spec.up_sql``. Una migración puede traer override por motor
           (``up_sql_mysql`` / ``up_sql_postgresql``), y el ``DROP`` puede vivir SOLO en el
           override: mirando ``up_sql`` sería invisible justo en el motor donde se ejecuta.

        El manifiesto se usa igual, en OR: donde existe, agrega información y nunca la quita
        (fail-closed). ``analyze`` está memoizado, así que el costo real es una vez por SQL.
        """
        out: dict[str, tuple[str, ...]] = {}
        for spec in specs:
            sql = self.runner.select_up_sql(spec, engine)
            reasons: list[str] = []
            facts = migration_facts.analyze(sql, spec.kind, spec.has_non_portable)
            if facts.destructive_statements:
                reasons.append(
                    "sentencias "
                    + ",".join(str(i) for i in facts.destructive_statements)
                )
            if any(st.destructive for st in spec.manifest):
                reasons.append("manifiesto")
            if reasons:
                out[spec.version] = tuple(reasons)
        return out

    @staticmethod
    def _guard_environment_destructive(
        db_id: int,
        pending: list[MigrationSpec],
        destructive: dict[str, tuple[str, ...]],
        env_slug: str,
    ) -> None:
        """
        Rechaza aplicar versiones destructivas a una BD de un entorno que las bloquea.

        Va dentro de ``_run_apply``, justo después de calcular ``pending``, y eso NO es un
        detalle de ubicación: es lo que hace que el guard cubra los DOS entrypoints (el
        ``apply`` por BD y ``apply_all``) con una sola inserción, sin una lectura extra al
        motor y sin abrir una ventana TOCTOU nueva. Los dos guards vecinos
        (``_guard_reviewed_capture`` y ``_guard_capture_consent``) están acá por el mismo
        motivo, y el comentario de arriba lo dice explícito.

        Si estuviera solo en el bucle de ``apply_all``, el incentivo sería perverso: ``apply_all``
        va siempre al head, así que una versión destructiva bloquearía producción de forma
        permanente en el lote, mientras el ``apply`` por BD la aplicaría con ``?version=`` sin
        ningún gate. El guard empujaría al camino no cubierto.

        NO se ejecuta en dry-run, y tampoco hace falta pedirlo: ni ``apply`` ni ``apply_all``
        llaman a ``_run_apply`` cuando ``dry_run=True`` (usan ``_dry_run_plan``). El plan sí
        informa qué versiones bloquearían, en ``blocked_by``: bloquear el dry-run le quitaría al
        operador justo la llamada con la que descubre qué lo frena.

        ``force`` NO lo saltea. ``force`` es override de CUARENTENA y nada más; si algún día
        hace falta un override de política, va un parámetro propio con su propio nombre.
        """
        hits = [s.version for s in pending if s.version in destructive]
        if not hits:
            return
        raise AppHttpException(
            message=(
                f"El entorno '{env_slug}' bloquea las migraciones destructivas y las versiones "
                f"{', '.join(hits)} contienen sentencias que pueden perder datos "
                "(DROP / TRUNCATE / DELETE sin WHERE / ALTER DROP COLUMN). "
                "'force' no habilita esta operación."
            ),
            status_code=409,
            public_context={
                "code": ecodes.CODE_DESTRUCTIVE_BLOCKED,
                "environment_slug": env_slug,
                "blocked_versions": hits,
            },
            context={"managed_database_id": db_id, "reasons": {v: destructive[v] for v in hits}},
        )

    # ------------------------------------------------------------------ #
    # Aplicación                                                          #
    # ------------------------------------------------------------------ #
    def apply(
        self,
        db_id: int,
        *,
        up_to_version: str | None = None,
        force: bool = False,
        dry_run: bool = False,
        on_failure: str = "auto",
        allow_result_capture: bool = False,
        admin: dict | None = None,
    ) -> dict:
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            self._verify_integrity(specs)
            slug, engine = model.slug, EngineType(engine_value(server))
            self._guard_cross_engine(session, model.id, engine)
            self._guard_reviewed_baseline(session, model.id)
            # El gate de ``reviewed`` de la captura NO va acá: se evalúa en ``_run_apply``
            # sobre las versiones REALMENTE pendientes (``apply?version=X`` aplica un prefijo
            # estricto, así que una versión posterior sin revisar no debe bloquear ni el
            # dry_run). Ver ``_guard_reviewed_capture``.
            self._guard_untranslatable_sql(specs, engine)
            self._guard_gateway_internal_sql(specs)
            db_name, server_id = md.name, md.server_id
            quarantined = md.status == ProvisionStatus.error
            target = build_target(server)
            # Política del entorno, resuelta mientras la sesión sigue abierta (valores planos:
            # después del close una fila ORM quedaría desligada).
            env_policy = self._env_policy_for(session, [db_id]).get(db_id)
        finally:
            session.close()

        if not specs:
            raise AppHttpException(
                message="El blueprint no tiene migraciones definidas.",
                status_code=422,
                context={"model_id": model.id},
            )

        # Versión objetivo inexistente: evita aplicar silenciosamente "todo lo ≤ X"
        # cuando X no es una versión real del blueprint. Comparación NUMÉRICA.
        if up_to_version is not None and version_sort_key(up_to_version) not in {
            version_sort_key(s.version) for s in specs
        }:
            raise AppHttpException(
                message=(
                    f"La versión objetivo {up_to_version} no existe en el blueprint. "
                    f"Versiones disponibles: {', '.join(s.version for s in specs)}."
                ),
                status_code=422,
                context={"target_version": up_to_version, "model_id": model.id},
            )

        # ROB1 — cuarentena: una migración fallida previa pudo dejar la BD en estado
        # parcial (DDL no transaccional en MySQL). Se exige inspección + force=true.
        self._guard_quarantine(db_id, quarantined, force, dry_run)

        if dry_run:
            return self._dry_run_plan(
                db_id, db_name, server_id, target, slug, specs, up_to_version,
                env_policy=env_policy,
                destructive=self._destructive_versions(specs, engine) if env_policy else None,
            )

        if on_failure not in self._ON_FAILURE_MODES:
            raise AppHttpException(
                message="'on_failure' invalido.",
                status_code=422,
                context={"on_failure": on_failure, "allowed": list(self._ON_FAILURE_MODES)},
            )

        return self._run_apply(
            db_id, db_name=db_name, server_id=server_id, target=target,
            engine=engine, slug=slug, specs=specs, model_id=model.id,
            up_to_version=up_to_version, was_quarantined=quarantined, admin=admin,
            on_failure=on_failure, allow_result_capture=allow_result_capture,
            env_blocks_destructive=bool(env_policy and env_policy[0]),
            env_slug=env_policy[1] if env_policy else None,
        )

    @staticmethod
    def _guard_cross_engine(session, model_id: int, engine: EngineType) -> None:
        """
        Un baseline de snapshot queda atado a su ``source_engine`` en dos casos:

        - objetos NO portables (rutinas/triggers/events): sqlglot no transpila código
          procedural;
        - migraciones de DATOS (``kind='data'``): la sintaxis upsert difiere por motor
          (``ON DUPLICATE KEY UPDATE`` vs ``ON CONFLICT``) y no se traduce.

        En ambos casos, aplicar ese blueprint a un servidor de otro motor se bloquea (422).
        """
        row = (
            session.query(ModelMigration)
            .filter(
                ModelMigration.model_id == model_id,
                ModelMigration.source_engine.isnot(None),
                sa_or(
                    ModelMigration.has_non_portable.is_(True),
                    ModelMigration.kind == "data",
                ),
            )
            .first()
        )
        if row and row.source_engine != engine.value:
            reason = (
                "datos-semilla (INSERT con sintaxis upsert por motor)"
                if row.kind == "data"
                else "objetos no portables (rutinas/triggers)"
            )
            raise AppHttpException(
                message=(
                    f"El blueprint tiene una migración de snapshot del motor "
                    f"'{row.source_engine}' con {reason}: no puede aplicarse a un servidor "
                    f"'{engine.value}'. Genere un baseline específico para este motor."
                ),
                status_code=422,
                context={"source_engine": row.source_engine, "target_engine": engine.value},
            )

    # Construcciones de MySQL que, en el SQL CRUDO, delatan que la traducción a PostgreSQL
    # no va a ser confiable. Es un pre-filtro BARATO: solo si alguna aparece se paga el
    # transpilado completo para confirmarlo (ver _guard_untranslatable_sql).
    _MYSQLISM_RE = re.compile(
        r"\b(?:MODIFY\s+COLUMN|CHANGE\s+COLUMN|DROP\s+PRIMARY\s+KEY"
        r"|DROP\s+FOREIGN\s+KEY|DROP\s+CHECK|ENGINE\s*=|AUTO_INCREMENT\s*=)",
        re.IGNORECASE,
    )

    def _guard_untranslatable_sql(self, specs: list[MigrationSpec], engine: EngineType) -> None:
        """
        Bloquea (422) aplicar a PostgreSQL una migración cuyo DDL de MySQL no se puede
        traducir con certeza.

        sqlglot transpila bien expresiones y tipos, pero deja VERBATIM varias formas de DDL
        de MySQL al escribir PostgreSQL: ``MODIFY COLUMN``, ``DROP PRIMARY KEY``,
        ``ENGINE=``… El resultado es SQL sintácticamente inválido que antes solo se
        descubría cuando el motor lo rechazaba, a mitad de la migración. Peor: cuando
        ``translate`` devuelve ``None``, ``select_up_sql`` cae al ``up_sql`` base, que en
        dialecto MySQL crudo es igual de inválido contra PostgreSQL — o sea que "fallar
        callado" no era una defensa. Ahora se detecta ANTES de tocar el motor y se le pide
        al admin lo único que resuelve el caso: un ``up_sql_postgresql`` explícito.

        Las reescrituras seguras (``DROP INDEX … ON``, ``DROP FOREIGN KEY``/``INDEX``/
        ``CHECK`` → ``DROP CONSTRAINT``) las hace el traductor y NO bloquean.

        Coste: el pre-filtro es una regex sobre el SQL crudo; el transpilado (caro) solo se
        ejecuta si esa regex encuentra algo, y encima está memoizado.
        """
        if engine != EngineType.postgresql:
            return
        translator = SqlTranslator()
        problems: dict[str, list[str]] = {}
        for spec in specs:
            if spec.up_sql_postgresql or spec.kind == "data":
                continue  # override explícito, o ya cubierto por _guard_cross_engine
            if not self._MYSQLISM_RE.search(spec.up_sql):
                continue  # pre-filtro barato: nada sospechoso, no se paga el transpilado
            blockers = translator.translation_blockers(spec.up_sql, engine)
            if blockers:
                problems[spec.version] = blockers
        if problems:
            detail = "; ".join(f"{v}: {', '.join(b)}" for v, b in sorted(problems.items()))
            raise AppHttpException(
                message=(
                    "No se puede aplicar a PostgreSQL: hay migraciones con DDL específico "
                    f"de MySQL que no se traduce de forma confiable ({detail}). Define un "
                    "'up_sql_postgresql' explícito en esas versiones (PATCH) y reintenta."
                ),
                status_code=422,
                context={"target_engine": engine.value, "untranslatable": problems},
                public_context={"untranslatable": problems},
            )

    @staticmethod
    def _guard_gateway_internal_sql(specs: list[MigrationSpec]) -> None:
        """
        Bloquea (409) aplicar una migración cuyo SQL toque la contabilidad INTERNA del
        gateway (``_gw_v_{slug}`` / ``_gw_stg_*``).

        Es la red de seguridad para versiones creadas ANTES del fix del snapshot: el diff
        estructural incluía la tabla de versión del destino y podía generar
        ``DROP TABLE _gw_v_{slug}``. Aplicar eso ejecuta todo el DDL y después mata a
        Alembic al registrar la versión (``1146``), dejando la BD con los cambios hechos
        pero SIN puntero de versión — y como el fallo ocurre en la contabilidad y no en una
        sentencia, la auto-reconciliación no lo detecta como aplicación parcial.

        Se valida ANTES de tocar el motor y sobre TODAS las variantes de SQL (base +
        overrides por motor + rollback). La salida es corregir el ``up_sql`` de esa versión
        con un ``PATCH`` (la creación y la edición ya rechazan este SQL de entrada).
        """
        offenders: dict[str, list[str]] = {}
        for spec in specs:
            hits: list[str] = []
            for sql in (spec.up_sql, spec.up_sql_mysql, spec.up_sql_postgresql, spec.down_sql):
                for prefix in references_gateway_internal_table(sql or ""):
                    if prefix not in hits:
                        hits.append(prefix)
            if hits:
                offenders[spec.version] = hits
        if offenders:
            detail = ", ".join(
                f"{v} (toca {', '.join(p)}*)" for v, p in sorted(offenders.items())
            )
            raise AppHttpException(
                message=(
                    f"No se puede aplicar: hay versiones cuyo SQL toca la contabilidad "
                    f"interna del gateway — {detail}. Esas sentencias borrarían o "
                    "alterarían la tabla de versión de Alembic y dejarían la BD sin "
                    "puntero de versión. Corregí el 'up_sql' de esas versiones con un "
                    "PATCH (quitando esas sentencias) antes de aplicar. Si una BD ya "
                    "quedó así, recreá su puntero con 'stamp' en la versión que "
                    "físicamente tiene aplicada."
                ),
                status_code=409,
                context={"model_id": None, "offending_versions": offenders},
                public_context={"offending_versions": offenders},
            )

    @staticmethod
    def _guard_reviewed_baseline(session, model_id: int) -> None:
        """
        R1: un baseline de SNAPSHOT contiene DDL capturado del motor (potencialmente no
        confiable). Bloquea (409) ``apply``/``apply-all`` mientras el blueprint tenga un
        baseline ``reviewed=false``: un admin debe revisar el SQL y aprobarlo
        (PATCH reviewed=true). NO afecta a ``stamp`` (que no ejecuta SQL).
        """
        rows = (
            session.query(ModelMigration.version)
            .filter(
                ModelMigration.model_id == model_id,
                ModelMigration.is_baseline.is_(True),
                ModelMigration.reviewed.is_(False),
            )
            .all()
        )
        if rows:
            versions = [r[0] for r in rows]
            raise AppHttpException(
                message=(
                    f"El blueprint tiene un baseline de snapshot SIN revisar ({', '.join(versions)}). "
                    "Contiene DDL capturado del motor: revísalo y apruébalo "
                    "(PATCH reviewed=true en esa versión) antes de aplicar."
                ),
                status_code=409,
                context={"model_id": model_id, "unreviewed_baseline": versions},
            )

    @staticmethod
    def _guard_reviewed_capture(
        session, model_id: int, *, migration_ids: list[int] | None = None
    ) -> None:
        """
        Una versión con ``capture_selects=true`` va a EXTRAER datos de negocio de la BD
        destino y guardarlos (cifrados) en la BD del gateway. Mismo mecanismo que el gate R1
        de los baselines de snapshot: nace ``reviewed=false`` y no se puede aplicar hasta que
        un admin revise QUÉ consulta y lo apruebe explícitamente (PATCH reviewed=true).

        Va aparte de ``_guard_reviewed_baseline`` porque el motivo del 409 es distinto y el
        operador necesita leer el correcto: allí "DDL capturado del motor sin revisar", acá
        "esta versión extrae datos".

        ``migration_ids`` ACOTA el chequeo a las versiones que REALMENTE se van a ejecutar, y
        ambos caminos lo usan así: ``rollback`` con el camino a revertir, ``apply`` con las
        pendientes hasta la versión objetivo. Evaluarlo sobre TODO el blueprint estaba mal
        (una premisa falsa: ``apply?version=X`` aplica un prefijo ESTRICTO, no la cadena
        completa) — con 0001..0010 y solo 0010 sin revisar, un ``apply?version=0007`` devolvía
        409 nombrando una versión que esa corrida no iba a tocar, y ni siquiera se podía
        previsualizar con ``dry_run``. Es el mismo criterio que gobierna el rollback: bloquear
        por una versión futura sin revisar le quita al operador la salida que tiene ante una
        migración mala.

        **Kill switch global.** Con ``MIGRATION_CAPTURE_ENABLED=False`` el codegen no emite ni
        una llamada a ``capture_statement``, así que capturar es FÍSICAMENTE imposible: no hay
        riesgo que gatear, y mantener el 409 solo bloqueaba la recuperación (el ``rollback`` no
        tiene ningún ``force`` con el que saltearlo). Mismo early-return que
        ``_guard_capture_consent``.
        """
        if not MIGRATION_CAPTURE_ENABLED:
            return
        if migration_ids is not None and not migration_ids:
            return  # nada que revisar en este subconjunto: ni se consulta
        q = session.query(ModelMigration.version).filter(
            ModelMigration.model_id == model_id,
            ModelMigration.capture_selects.is_(True),
            ModelMigration.reviewed.is_(False),
        )
        if migration_ids is not None:
            q = q.filter(ModelMigration.id.in_(migration_ids))
        rows = q.all()
        if rows:
            versions = [r[0] for r in rows]
            raise AppHttpException(
                message=(
                    f"El blueprint tiene versiones con captura de resultados SIN revisar "
                    f"({', '.join(versions)}). Esas migraciones guardan el resultado de sus "
                    "SELECT (datos de la BD destino) en el gateway: revisá qué consultan y "
                    "aprobalas (PATCH reviewed=true) antes de aplicar o revertir."
                ),
                status_code=409,
                context={"model_id": model_id, "unreviewed_capture": versions},
                public_context={"unreviewed_capture": versions},
            )

    @staticmethod
    def _guard_capture_consent(
        pending: list[MigrationSpec],
        allow_result_capture: bool,
        *,
        direction: str = "up",
    ) -> None:
        """
        Consentimiento EXPLÍCITO por corrida: ejecutar una versión con ``capture_selects``
        exige ``allow_result_capture=true`` en el request (409 antes de tocar el motor).

        No es redundante con el opt-in de la versión: un blueprint se replica sobre N BDs de
        dueños potencialmente distintos, y quien dispara el ``apply`` sobre UNA de ellas
        tiene que saber que esa corrida va a extraer datos de ESA base. El opt-in lo decidió
        quien escribió la migración, quizá meses antes y para otra BD.

        Cubre AMBAS direcciones: el codegen emite ``capture_statement`` también para las
        sentencias del ``down_sql`` (``_render_revision`` recibe un solo flag ``capture`` y lo
        aplica a los dos cuerpos), así que un ``rollback`` extrae y persiste datos igual que
        un ``apply``. ``direction`` solo cambia el texto del 409 para que el operador entienda
        qué operación está bloqueada.

        Si el kill switch global está apagado no se captura nada, así que tampoco hay nada
        que consentir.
        """
        if allow_result_capture or not MIGRATION_CAPTURE_ENABLED:
            return
        versions = [s.version for s in pending if s.capture_selects]
        if not versions:
            return
        what = (
            "las versiones pendientes" if direction == "up" else "las versiones a revertir"
        )
        operation = "esta aplicación" if direction == "up" else "este rollback"
        raise AppHttpException(
            message=(
                f"Confirmación requerida: {what} "
                f"{', '.join(versions)} tienen la captura de resultados activada, así que "
                f"{operation} va a guardar filas de esta base de datos (cifradas) en el "
                "gateway. Repetí la llamada con allow_result_capture=true si es lo que "
                "querés, o desactivá capture_selects en esas versiones."
            ),
            status_code=409,
            context={"capture_versions": versions, "required": "allow_result_capture=true"},
            public_context={"capture_versions": versions},
        )

    @staticmethod
    def _guard_quarantine(db_id: int, quarantined: bool, force: bool, dry_run: bool) -> None:
        if quarantined and not force and not dry_run:
            raise AppHttpException(
                message=(
                    "La BD está en cuarentena por un fallo de migración previo. "
                    "Inspeccione el estado real y reintente con force=true."
                ),
                status_code=409,
                context={"managed_database_id": db_id, "required": "force=true"},
            )

    def _dry_run_plan(
        self, db_id, db_name, server_id, target, slug, specs, up_to_version,
        *,
        env_policy: tuple[bool, str] | None = None,
        destructive: dict[str, tuple[str, ...]] | None = None,
    ) -> dict:
        """
        Calcula el plan (pendientes) SIN tocar el motor más que para leer la versión.

        El dry-run **informa** el bloqueo por entorno en ``blocked_by`` pero NO lo aplica: es
        la llamada con la que el operador descubre qué lo frena, así que hacerla fallar le
        quitaría el diagnóstico. Precedente exacto del criterio: ``_guard_quarantine`` recibe
        ``dry_run`` y se saltea. El guard real vive en ``_run_apply``, que este camino no
        ejecuta.
        """
        current = self.runner.get_current_version(target, db_name, slug)
        pending = self.runner.compute_pending(current, specs, up_to_version)
        pending_versions = [s.version for s in pending]
        blocked_by: list[str] = []
        if env_policy and env_policy[0] and destructive:
            blocked_by = [v for v in pending_versions if v in destructive]
        return {
            "managed_database_id": db_id,
            "database_name": db_name,
            "server_id": server_id,
            "dry_run": True,
            "from_version": current,
            "current_version": current,  # alias retrocompatible
            "to_version": pending_versions[-1] if pending_versions else current,
            "target_version": up_to_version,
            "no_op": len(pending) == 0,
            "pending_versions": pending_versions,
            "pending_count": len(pending),
            "environment_slug": env_policy[1] if env_policy else None,
            "blocked_by": blocked_by,
        }

    # Política ante un fallo a mitad de una migración (solo aplica cuando el motor NO es
    # transaccional — en PostgreSQL el propio motor deshace la migración y no hay nada que
    # reconciliar).
    #   'auto'      → reconcilia SOLO si puede deshacer TODO lo aplicado (default).
    #   'reconcile' → reconcilia igual, salteando lo que no tiene reverso (equivale a force).
    #   'leave'     → no toca nada: cuarentena + checkpoint, como antes.
    _ON_FAILURE_MODES = ("auto", "reconcile", "leave")

    def _auto_reconcile_after_failure(
        self, db_id, *, db_name, target, engine, specs, results, mode, admin,
    ) -> dict | None:
        """
        Deshace la aplicación parcial INMEDIATAMENTE después del fallo, en la misma llamada.

        Es la "sobreprotección" que faltaba: sin esto, un `apply` que falla a mitad devolvía
        200 con `failed=true` y dejaba al admin con una BD en estado desconocido, un
        checkpoint que tenía que entender, y la tentación de `stamp --force` + `rollback`
        (que es exactamente el camino que corrompe: stampear afirma que las 50 sentencias
        corrieron, y el rollback ejecuta 50 reversos contra 10 cambios reales).

        Devuelve el resultado de la reconciliación, o ``None`` si no había nada que hacer
        (motor transaccional, sin checkpoint, o ``mode='leave'``).
        """
        if mode == "leave":
            return None
        failed = next((r for r in results if r.status == "failed"), None)
        if failed is None:
            return None
        spec = next((s for s in specs if s.id == failed.migration_id), None)
        if spec is None:
            return None
        row = next(
            (
                r
                for r in migration_progress.incomplete_progress_for_database(db_id, "up")
                if r["model_migration_id"] == spec.id
            ),
            None,
        )
        if row is None:
            # Sin checkpoint incompleto no hay estado parcial que deshacer: o el motor es
            # transaccional (PostgreSQL ya lo revirtió), o falló la PRIMERA sentencia, o la
            # migración no era resumible y no se puede saber qué corrió.
            return None
        plan = self._reconcile_plan(spec, engine, row)
        if not plan["inverses"]:
            return None
        if mode == "auto" and not plan["reconcilable"]:
            # Hay sentencias aplicadas SIN reverso: deshacer solo una parte dejaría un
            # estado igual de raro pero MENOS documentado. Se deja como está y el admin
            # decide (reconcile-partial con force, o a mano).
            logger.info(
                "No se auto-reconcilia %s: %d sentencia(s) aplicada(s) sin reverso.",
                spec.version, len(plan["unreversible"]),
            )
            return None
        audit.record_intent(
            "migration.auto_reconcile",
            admin=admin, target_type="managed_database", target_id=db_id,
            server_id=None,
            detail=(
                f"auto-reconciliación tras fallo de {spec.version}: deshacer "
                f"{len(plan['inverses'])} sentencia(s)"
            ),
        )
        undo = self.runner.reconcile_partial(
            target, db_name=db_name, engine=engine, managed_db_id=db_id,
            spec=spec, inverses=plan["inverses"],
            total_statements=row["total_statements"],
        )
        undo_failed = any(r.status == "failed" for r in undo)
        remaining = migration_progress.get_progress(db_id, spec.id, "up")
        fully = not undo_failed and remaining is None
        audit.record(
            "migration.auto_reconcile",
            status="error" if undo_failed else "success",
            admin=admin, target_type="managed_database", target_id=db_id,
            touched_engine=True,
            detail=(
                f"{sum(1 for r in undo if r.status == 'applied')}/{len(plan['inverses'])} "
                f"reverso(s) de {spec.version}"
                + (" — estado reconciliado" if fully else " — INCOMPLETO")
            ),
        )
        return {
            "version": spec.version,
            "attempted": True,
            "undone_count": sum(1 for r in undo if r.status == "applied"),
            "statements_to_undo": len(plan["inverses"]),
            "fully_reconciled": fully,
            "unconfirmed_reverses": plan["unconfirmed"],
            "unreversible_statements": plan["unreversible"],
            "error": next((r.error for r in undo if r.status == "failed"), None),
        }

    @staticmethod
    def _guard_partial_down_before_apply(db_id: int, specs: list[MigrationSpec]) -> None:
        """
        Bloquea (409) el ``apply`` mientras haya un ROLLBACK parcialmente aplicado.

        Simétrico de ROB2 (que bloquea el rollback con un apply parcial): si el
        ``downgrade`` de la versión N falló a mitad, Alembic nunca movió el puntero — el
        ledger sigue en N — pero la BD ya tiene ALGUNOS reversos de N ejecutados. Un
        ``apply`` en ese estado lee "current=N", calcula pendientes desde ahí y deja la
        versión N a medio deshacer para siempre (ninguna re-aplicación la repara, porque
        para el ledger N "ya está aplicada"). La salida correcta es REINTENTAR el
        rollback, que retoma desde el checkpoint de dirección ``down``.
        """
        incomplete = migration_progress.incomplete_progress_for_database(db_id, direction="down")
        if not incomplete:
            return
        by_id = {s.id: s.version for s in specs}
        detail = ", ".join(
            f"versión {by_id.get(row['model_migration_id'], row['model_migration_id'])} "
            f"({row['last_statement_index']}/{row['total_statements']} reversos)"
            for row in incomplete
        )
        raise AppHttpException(
            message=(
                f"No se puede aplicar: hay un ROLLBACK parcialmente ejecutado ({detail}). "
                "La versión sigue registrada pero sus cambios están a medio deshacer; "
                "aplicar encima congelaría ese estado. Reintente el 'rollback' (retoma "
                "automáticamente desde el último reverso exitoso) antes de aplicar."
            ),
            status_code=409,
            context={"managed_database_id": db_id, "incomplete_progress": incomplete},
            public_context={"incomplete_progress": incomplete},
        )

    def _run_apply(
        self, db_id, *, db_name, server_id, target, engine, slug, specs, model_id,
        up_to_version, was_quarantined, admin, on_failure: str = "auto",
        allow_result_capture: bool = False,
        env_blocks_destructive: bool = False,
        env_slug: str | None = None,
        destructive_versions: dict[str, tuple[str, ...]] | None = None,
    ) -> dict:
        """Ejecuta el apply real sobre UNA BD ya cargada/validada (reutilizable por apply_all)."""
        # Simétrico de ROB2: con un rollback a medio ejecutar, aplicar encima opera a
        # ciegas. Va acá (no en apply()) para cubrir también apply_all, que captura la
        # excepción por BD sin abortar el lote.
        self._guard_partial_down_before_apply(db_id, specs)
        # Versión ANTES de aplicar (read-only) para reportar el salto from→to.
        from_version = self.runner.get_current_version(target, db_name, slug)
        # Las DOS llaves de la captura, sobre las versiones que REALMENTE se van a aplicar en
        # ESTA BD (no sobre todo el blueprint): una versión con captura ya aplicada hace meses
        # no debe exigir el flag para siempre, y una versión POSTERIOR a la objetivo no debe
        # bloquear una aplicación que no la incluye. Igual que en ``rollback``, y también acá
        # (no en ``apply``) para cubrir ``apply_all``, que reporta el 409 por BD.
        pending = self.runner.compute_pending(from_version, specs, up_to_version)
        capture_pending_ids = [s.id for s in pending if s.capture_selects]
        if capture_pending_ids:
            session = self._session()
            try:
                self._guard_reviewed_capture(
                    session, model_id, migration_ids=capture_pending_ids
                )
            finally:
                session.close()
        self._guard_capture_consent(pending, allow_result_capture)
        # Guard de entorno, sobre las pendientes REALES de ESTA BD. Va acá por el mismo
        # criterio que los dos guards de captura de arriba, y con el mismo beneficio: cubre el
        # ``apply`` por BD y ``apply_all`` de una sola vez, sin releer el motor.
        if env_blocks_destructive:
            self._guard_environment_destructive(
                db_id,
                pending,
                destructive_versions
                if destructive_versions is not None
                else self._destructive_versions(specs, engine),
                env_slug or "?",
            )
        audit.record(
            "migration.apply", status="attempt", admin=admin,
            target_type="managed_database", target_id=db_id, server_id=server_id,
            touched_engine=True, detail=f"apply hasta {up_to_version or 'head'}",
        )
        try:
            results = self.runner.apply(
                target, db_name=db_name, slug=slug, engine=engine,
                managed_db_id=db_id, specs=specs, up_to_version=up_to_version,
            )
        except AppHttpException as exc:
            # Fallo ANTES de aplicar ninguna migración (conexión/lock): no hay
            # resultado por-migración que registrar; dejamos traza en auditoría.
            audit.record(
                "migration.apply", status="error", admin=admin,
                target_type="managed_database", target_id=db_id, server_id=server_id,
                touched_engine=True,
                detail=f"fallo al aplicar (HTTP {getattr(exc, 'status_code', '?')})",
            )
            raise

        self._record_history(db_id, results)
        failed = any(r.status == "failed" for r in results)

        # Auto-protección: si quedó una aplicación PARCIAL (solo posible en MySQL/MariaDB,
        # donde el DDL hace commit implícito), se deshace acá mismo. Va ANTES de sincronizar
        # la versión y de marcar cuarentena para que ambos reflejen el estado YA reconciliado.
        reconciliation = None
        if failed:
            reconciliation = self._auto_reconcile_after_failure(
                db_id, db_name=db_name, target=target, engine=engine, specs=specs,
                results=results, mode=on_failure, admin=admin,
            )

        # model_version se SINCRONIZA releyendo la fuente de verdad (tabla de versión
        # que Alembic mantiene en la BD destino), no la contabilidad local.
        self._sync_model_version_from_engine(db_id, target, db_name, slug)

        # ROB1 — marcar/limpiar cuarentena según el desenlace. Si la auto-reconciliación
        # dejó la BD coincidiendo con su versión registrada, NO se pone en cuarentena: no
        # hay estado parcial que inspeccionar, solo una migración que hay que corregir.
        reconciled_clean = bool(reconciliation and reconciliation["fully_reconciled"])
        self._set_quarantine(db_id, failed and not reconciled_clean, results)

        audit.record(
            "migration.apply", status="error" if failed else "success", admin=admin,
            target_type="managed_database", target_id=db_id, server_id=server_id,
            touched_engine=True,
            detail=f"{sum(1 for r in results if r.status=='applied')} aplicadas"
                   + (" (con fallo)" if failed else ""),
        )
        applied = [r for r in results if r.status == "applied"]
        to_version = applied[-1].version if applied else from_version
        captured = self._capture_pointer(db_id, results, admin=admin, server_id=server_id)
        return {
            "managed_database_id": db_id,
            "captured_select_count": captured,
            "select_results_available": captured > 0,
            "database_name": db_name,
            "server_id": server_id,
            "from_version": from_version,
            "to_version": to_version,
            "target_version": up_to_version,
            "applied_count": len(applied),
            "failed": failed,
            "quarantined": failed and not reconciled_clean,
            "no_op": len(results) == 0 and not failed,
            "pending_versions": [r.version for r in results],
            "results": [self._result_dict(r) for r in results],
            # Qué hizo el sistema por su cuenta ante el fallo (None si no hizo falta).
            "reconciliation": reconciliation,
        }

    # ------------------------------------------------------------------ #
    # Captura de resultados de SELECT (informe, nunca maquinaria)          #
    # ------------------------------------------------------------------ #
    def _capture_pointer(
        self,
        db_id: int,
        results: list[MigrationResult],
        *,
        admin: dict | None,
        server_id: int | None,
    ) -> int:
        """
        Cuántas capturas escribió ESTA corrida, y auditoría de esa ESCRITURA (conteos, nunca
        valores).

        El número viene del propio runner (``MigrationResult.captured_results``, que devuelve
        ``migration_results.finalize``), NO de un ``COUNT`` sobre la tabla: la tabla acumula
        las capturas de corridas anteriores, así que contarla afirmaba escrituras que no
        habían ocurrido. Caso real: una versión aplicada con captura (1 fila ``up``) y después
        revertida con un ``down_sql`` sin lecturas → el ``rollback`` respondía
        ``captured_select_count: 1`` y auditaba una escritura inexistente. Las capturas
        viejas siguen leyéndose con ``GET …/select-results``; el puntero de la respuesta habla
        solo de lo que esta corrida produjo.
        """
        written = sum(r.captured_results for r in results)
        if not written:
            return 0
        touched = {r.migration_id for r in results if r.captured_results}
        audit.record(
            "migration.select_results.write",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=False,
            detail=(
                f"{written} resultado(s) de SELECT capturados en "
                f"{len(touched)} versión(es)"
            ),
        )
        return written

    def select_results(
        self, db_id: int, version: str, *, admin: dict | None = None
    ) -> dict:
        """
        Devuelve las capturas de una versión sobre esta BD, DESCIFRADAS.

        La auditoría es FAIL-CLOSED y va ANTES de descifrar (mismo criterio que
        ``reveal_password``): esto entrega datos de negocio, así que si el rastro no se puede
        persistir la lectura no ocurre.
        """
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            engine = EngineType(engine_value(server))
            db_name, server_id = md.name, md.server_id
        finally:
            session.close()

        spec = self._spec_or_404(specs, version, model.id)

        audit.record_intent(
            "migration.select_results.read",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=False,
            detail=f"lectura de resultados capturados de la versión {version}",
        )
        items = migration_results.read_results(db_id, spec.id)

        # Índices ESPERADOS: se derivan acá y no se persisten. Que la lista de sentencias
        # salga de ``statement_lists`` es lo que garantiza que estos índices signifiquen lo
        # mismo que los del checkpoint.
        up_statements = self.runner.statement_lists(spec, engine)[0]
        expected = [
            i
            for i, stmt in enumerate(up_statements, start=1)
            if migration_results.is_capturable(stmt, engine=engine.value)
        ]
        present_up = {it["statement_index"] for it in items if it["direction"] == "up"}
        rolled_back = [
            it["statement_index"]
            for it in items
            if it["durability"] == migration_results.DURABILITY_ROLLED_BACK
        ]
        audit.record(
            "migration.select_results.read",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=False,
            detail=f"{len(items)} captura(s) de la versión {version} entregadas",
        )
        return {
            "managed_database_id": db_id,
            "database_name": db_name,
            "server_id": server_id,
            "model_migration_id": spec.id,
            "version": spec.version,
            "capture_selects": spec.capture_selects,
            "stale": any(it["migration_checksum"] != spec.checksum for it in items),
            "expected_indexes": expected,
            "missing_indexes": [i for i in expected if i not in present_up],
            "durability_warning": (
                "PostgreSQL revirtió la transacción de la migración: las sentencias "
                f"{', '.join(str(i) for i in rolled_back)} muestran lo que se vio DURANTE el "
                "intento, no el estado final de la base."
                if rolled_back
                else None
            ),
            "items": items,
        }

    def purge_select_results(
        self, db_id: int, version: str, *, admin: dict | None = None
    ) -> int:
        """Borra las capturas de una versión sobre esta BD. Devuelve cuántas se borraron."""
        session = self._session()
        try:
            md, _server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            server_id = md.server_id
        finally:
            session.close()

        spec = self._spec_or_404(specs, version, model.id)
        audit.record_intent(
            "migration.select_results.purge",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=False,
            detail=f"purga de los resultados capturados de la versión {version}",
        )
        deleted = migration_results.purge(db_id, spec.id)
        audit.record(
            "migration.select_results.purge",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=False,
            detail=f"{deleted} captura(s) de la versión {version} eliminadas",
        )
        return deleted

    @staticmethod
    def _spec_or_404(
        specs: list[MigrationSpec], version: str, model_id: int
    ) -> MigrationSpec:
        spec = next(
            (s for s in specs if version_sort_key(s.version) == version_sort_key(version)),
            None,
        )
        if spec is None:
            raise AppHttpException(
                message=f"La versión {version} no existe en el blueprint de esta BD.",
                status_code=404,
                context={"model_id": model_id, "version": version},
            )
        return spec

    def rollback(
        self,
        db_id: int,
        *,
        confirm_version: str | None = None,
        target_version: str | None = None,
        allow_result_capture: bool = False,
        admin: dict | None = None,
    ) -> dict:
        """
        Revierte una BD a ``target_version`` de forma SECUENCIAL en una sola llamada
        (análogo a apply, hacia atrás): el sistema detecta qué downgrades hay que
        aplicar y los ejecuta en orden. Si ``target_version`` se omite, revierte solo
        la última migración (compatibilidad). Operación DESTRUCTIVA: exige
        ``confirm_version == versión actual`` (doble intención) y que TODO el camino
        tenga ``down_sql`` confirmado (409 si falta alguno).

        Si alguna versión del camino tiene ``capture_selects=true``, exige además las MISMAS
        dos llaves que ``apply``: la versión revisada (``reviewed=true``) y el consentimiento
        de la corrida (``allow_result_capture=true``). No es simetría decorativa: el codegen
        emite ``capture_statement`` para las sentencias del ``down_sql`` igual que para las
        del ``up_sql``, así que un rollback extrae y persiste datos de negocio exactamente
        como un apply.
        """
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            self._verify_integrity(specs)  # el rollback ejecuta DDL destructivo
            slug, engine = model.slug, EngineType(engine_value(server))
            db_name, server_id = md.name, md.server_id
            target = build_target(server)
        finally:
            session.close()

        # ROB2 — una aplicación PARCIAL bloquea el rollback. Ver el docstring del guard:
        # revertir N-1 mientras la BD tiene media versión N aplicada es el escenario de
        # corrupción que este módulo existe para evitar.
        self._guard_partial_before_rollback(db_id, specs)

        current = self.runner.get_current_version(target, db_name, slug)
        if current is None:
            raise AppHttpException(
                message="La BD no tiene ninguna migración aplicada para revertir.",
                status_code=409,
                context={"managed_database_id": db_id},
            )
        # Doble intención: el cliente repite la versión ACTUAL (de la que parte).
        if confirm_version != current:
            raise AppHttpException(
                message=(
                    "Confirmación requerida: 'confirm_version' debe coincidir con la "
                    f"versión actual de la BD ({current})."
                ),
                status_code=422,
                context={"managed_database_id": db_id, "required": "confirm_version == current"},
            )

        cur_key = version_sort_key(current)
        spec_keys = {version_sort_key(s.version) for s in specs}

        # Determinar el destino del rollback.
        if target_version is None:
            # Compat: revertir UNA migración (la actual). Destino = versión existente
            # inmediatamente inferior, o base (None) si la actual es la primera.
            below = sorted(
                (s.version for s in specs if version_sort_key(s.version) < cur_key),
                key=version_sort_key,
            )
            dest = below[-1] if below else None
        else:
            tkey = version_sort_key(target_version)
            if tkey >= cur_key:
                raise AppHttpException(
                    message=(
                        f"La versión objetivo ({target_version}) debe ser ANTERIOR a la "
                        f"actual ({current}). Para avanzar usa /migrations/apply."
                    ),
                    status_code=422,
                    context={"target_version": target_version, "current": current},
                )
            if tkey not in spec_keys:
                raise AppHttpException(
                    message=f"La versión objetivo {target_version} no existe en el blueprint.",
                    status_code=422,
                    context={"target_version": target_version, "model_id": model.id},
                )
            dest = target_version

        # Camino a revertir: versiones con dest < v <= current (las que se desharán).
        dest_key = version_sort_key(dest) if dest is not None else None
        path = sorted(
            (
                s for s in specs
                if version_sort_key(s.version) <= cur_key
                and (dest_key is None or version_sort_key(s.version) > dest_key)
            ),
            key=lambda s: version_sort_key(s.version),
            reverse=True,
        )
        # Fail-closed: TODO el camino debe tener down_sql confirmado ANTES de ejecutar
        # (evita un rollback que falle a mitad por un downgrade no definido).
        missing = [s.version for s in path if not s.down_sql]
        if missing:
            raise AppHttpException(
                message=(
                    "No se puede revertir: las versiones "
                    f"{', '.join(missing)} no tienen rollback (down_sql) confirmado. "
                    "Confírmalo con PATCH en cada migración."
                ),
                status_code=409,
                context={"managed_database_id": db_id, "missing_down_sql": missing},
                # El frontend necesita esta lista para guiar al admin (PATCH por versión),
                # no es solo debug info: viaja siempre, no solo en development.
                public_context={"missing_down_sql": missing},
            )

        # Captura de resultados: las MISMAS dos llaves que exige ``apply``, sobre el camino
        # que REALMENTE se va a ejecutar (no sobre todo el blueprint — ver el docstring de
        # ``_guard_reviewed_capture``). Van acá, después de calcular ``path`` y antes de la
        # auditoría de intento: se responde 409 sin tocar el motor.
        capture_path_ids = [s.id for s in path if s.capture_selects]
        if capture_path_ids:
            session = self._session()
            try:
                self._guard_reviewed_capture(
                    session, model.id, migration_ids=capture_path_ids
                )
            finally:
                session.close()
        self._guard_capture_consent(path, allow_result_capture, direction="down")

        audit.record(
            "migration.rollback", status="attempt", admin=admin,
            target_type="managed_database", target_id=db_id, server_id=server_id,
            touched_engine=True, detail=f"rollback {current} -> {dest or 'base'}",
        )
        results = self.runner.rollback_to(
            target, db_name=db_name, slug=slug, engine=engine,
            managed_db_id=db_id, specs=specs, to_version=dest,
        )
        self._record_history(db_id, results)
        # La versión tras el rollback se RE-LEE del motor (fuente de verdad) y se
        # sincroniza en el inventario del gateway.
        new_current = self.runner.get_current_version(target, db_name, slug)
        self._set_model_version(db_id, new_current)

        failed = any(r.status == "failed" for r in results)
        self._set_quarantine(db_id, failed, results)
        reverted = [r for r in results if r.status == "applied"]

        audit.record(
            "migration.rollback",
            status="error" if failed else "success",
            admin=admin, target_type="managed_database", target_id=db_id,
            server_id=server_id, touched_engine=True,
            detail=f"{len(reverted)} revertida(s): {current} -> {new_current or 'base'}"
                   + (" (con fallo)" if failed else ""),
        )
        captured = self._capture_pointer(
            db_id, results, admin=admin, server_id=server_id
        )
        return {
            "managed_database_id": db_id,
            "database_name": db_name,
            "server_id": server_id,
            "from_version": current,
            "to_version": new_current,
            "target_version": dest,
            "reverted_count": len(reverted),
            "captured_select_count": captured,
            "select_results_available": captured > 0,
            "reverted_versions": [r.version for r in reverted],
            "failed": failed,
            "quarantined": failed,
            "no_op": len(results) == 0,
            "results": [self._result_dict(r) for r in results],
        }

    def stamp(
        self, db_id: int, version: str, *, force: bool = False, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            self._verify_integrity(specs)
            slug, engine = model.slug, EngineType(engine_value(server))
            db_name, server_id = md.name, md.server_id
            target = build_target(server)
            self._guard_stamp_unreviewed_capture(session, model.id, version, force)
        finally:
            session.close()

        self._guard_partial_checkpoint(db_id, force)

        self.runner.stamp(
            target, db_name=db_name, slug=slug, engine=engine,
            managed_db_id=db_id, specs=specs, version=version,
        )
        self._set_model_version(db_id, version)
        # El stamp es una AFIRMACIÓN explícita del admin ("esta BD está en la versión X"):
        # reconcilia el estado, así que también saca a la BD de cuarentena si un apply
        # previo la dejó en 'error' (p. ej. reintentar CREATE TABLE de una tabla ya
        # existente tras adoptarla). No ejecuta SQL, solo marca la versión.
        self._set_quarantine(db_id, failed=False, results=[])
        if force:
            # El admin afirma haber reconciliado el estado físico a mano: cualquier
            # checkpoint de sentencia (de CUALQUIER versión y en CUALQUIER dirección)
            # que quedara pendiente para esta BD ya no es confiable — el stamp
            # reescribe la narrativa de versión por completo, así que el checkpoint
            # quedaría hablando de un estado que el admin acaba de invalidar.
            migration_progress.clear_progress_for_database(db_id)
        # ``stamp`` DECLARA una versión sin ejecutar una línea de DDL, y con eso reescribe
        # ``managed_databases.model_version``. O sea que es la puerta trasera de cualquier
        # política que se apoye en esa caché: un gate de promoción del tipo "no apliques en
        # producción si staging no está al día" se destraba stampeando las BDs de staging al
        # head. Ese gate NO existe todavía (ver TODO.md); cuando se construya, tiene que cerrar
        # este camino y no solo leer la caché. Mientras tanto el rastro es explícito: la
        # auditoría nombra la versión declarada y la que había antes, para que un stamp usado
        # como atajo se pueda reconstruir después.
        audit.record(
            "migration.stamp", admin=admin, target_type="managed_database",
            target_id=db_id, server_id=server_id, touched_engine=True,
            detail=f"stamp DECLARA version {version} (sin ejecutar DDL)"
            + (" (force: checkpoint parcial descartado)" if force else ""),
        )
        return self.status(db_id)

    @staticmethod
    def _guard_stamp_unreviewed_capture(
        session, model_id: int, version: str, force: bool
    ) -> None:
        """
        Bloquea (409) marcar (``stamp``) una versión que tiene la captura de resultados
        activada y AÚN NO fue revisada. Defensa en profundidad, no la barrera principal.

        ``stamp`` no ejecuta SQL, y por eso ``_guard_reviewed_baseline`` lo excluye a
        propósito. Pero el stamp es lo que HABILITA el ``rollback`` de esa versión: sin este
        gate el camino era crear la versión con ``capture_selects=true`` (nace
        ``reviewed=false``) → stampearla (sin control) → ``rollback`` sobre ella. Ese último
        paso ya está cubierto por su propio gate; esto lo corta un paso antes, en el punto
        donde se afirma un estado que el gateway no pudo haber producido: ``apply`` jamás
        aplicó esa versión (su gate de ``reviewed`` lo impide), así que no hay historia
        legítima en la que una BD esté genuinamente en ella.

        ``force=true`` lo omite, con el MISMO significado que ya tiene en este endpoint ("el
        admin afirma haber reconciliado el estado físico a mano"). Hace falta un escape real:
        una versión aplicada hace meses a la que después se le activó la captura queda
        ``reviewed=false``, y una BD que perdió su puntero de versión (ver el incidente de
        ``_gw_v_*``) necesita poder re-stampearla para salir de cuarentena.
        """
        if force:
            return
        row = (
            session.query(ModelMigration.id)
            .filter(
                ModelMigration.model_id == model_id,
                ModelMigration.version == version,
                ModelMigration.capture_selects.is_(True),
                ModelMigration.reviewed.is_(False),
            )
            .first()
        )
        if row is None:
            return
        raise AppHttpException(
            message=(
                f"La versión {version} tiene la captura de resultados activada y SIN revisar: "
                "marcarla habilitaría un rollback que extraería datos de esta base. Revisala "
                "y aprobala (PATCH reviewed=true) antes de marcarla. Si la BD realmente ya "
                "está en esa versión y estás reconciliando a mano, repetí con force=true."
            ),
            status_code=409,
            context={
                "model_id": model_id,
                "version": version,
                "required": "reviewed=true (o force=true)",
            },
            public_context={"unreviewed_capture": [version]},
        )

    @staticmethod
    def _guard_partial_before_rollback(db_id: int, specs: list[MigrationSpec]) -> None:
        """
        ROB2: bloquea (409) el ``rollback`` mientras haya una aplicación PARCIAL pendiente.

        Es el agujero más grave que tenía el flujo de rollback. Cuando el ``apply`` de la
        versión N falla a mitad, Alembic NO alcanzó a escribir N en ``_gw_v_{slug}`` (el
        stamp va al final del ``upgrade()``), así que el ledger sigue en N-1 mientras la BD
        tiene, físicamente, las primeras sentencias de N ya commiteadas (AUTOCOMMIT: el DDL
        de MySQL/MariaDB no es transaccional).

        En ese estado, ``rollback`` NO veía nada raro: leía "current = N-1" y se ponía a
        ejecutar el ``down_sql`` de N-1 contra una BD contaminada con parte de N. O falla a
        mitad —dejando un tercer estado inconsistente— o "funciona" por casualidad y deja
        objetos huérfanos de N que el gateway ya no sabe que existen.

        La salida no es forzar: es RECONCILIAR primero (``/migrations/reconcile-partial``,
        que deshace exactamente las sentencias que sí se aplicaron) o reintentar el
        ``apply`` para completar N. Recién entonces el rollback opera sobre un estado
        conocido.
        """
        incomplete = migration_progress.incomplete_progress_for_database(db_id, direction="up")
        if not incomplete:
            return
        by_id = {s.id: s.version for s in specs}
        detail = ", ".join(
            f"versión {by_id.get(row['model_migration_id'], row['model_migration_id'])} "
            f"({row['last_statement_index']}/{row['total_statements']} sentencias)"
            for row in incomplete
        )
        raise AppHttpException(
            message=(
                f"No se puede revertir: hay una aplicación PARCIAL sin resolver ({detail}). "
                "La versión no quedó registrada en la BD, así que el rollback operaría "
                "sobre un estado desconocido. Primero: reintente 'apply' para completarla, "
                "o use 'migrations/reconcile-partial' para deshacer exactamente lo que sí "
                "se aplicó."
            ),
            status_code=409,
            context={"managed_database_id": db_id, "incomplete_progress": incomplete},
            public_context={"incomplete_progress": incomplete},
        )

    @staticmethod
    def _guard_partial_checkpoint(db_id: int, force: bool) -> None:
        """
        ``stamp`` afirma "esta BD está en la versión X" SIN ejecutar SQL. Si hay una
        aplicación PARCIAL detectada (checkpoint de sentencia incompleto: algunas
        sentencias de alguna migración ya commitearon, pero no todas) para esta BD, un
        stamp ciego la enmascararía: un rollback posterior ejecutaría el ``down_sql``
        completo de esa versión contra una BD que solo tiene una FRACCIÓN de los cambios
        físicos — pudiendo fallar a mitad también, o "funcionar" solo por casualidad.

        Bloquea por defecto; ``force=true`` es la vía explícita para "ya reconcilié el
        estado físico a mano, proceda" (descarta el checkpoint, auditado). Sin este
        override, un fallo NO resumible (sentencia rota / DDL no atómico — ver
        ``migration_progress.py``) dejaría al admin sin ninguna salida.

        **ADVERTENCIA sobre el anti-patrón más común.** La reacción intuitiva ante un apply
        que falla a mitad es ``stamp --force`` a la versión que falló y después ``rollback``.
        Eso EMPEORA el problema: el stamp AFIRMA que las N sentencias de esa versión se
        aplicaron, así que el rollback ejecuta los N reversos contra una BD que solo tiene k
        cambios físicos — los N-k reversos de lo que nunca corrió fallan ("no existe") y el
        rollback muere a mitad, dejando un TERCER estado inconsistente. Además ``force``
        descarta el checkpoint, que era la única prueba de dónde había quedado. La vía
        correcta es ``/migrations/reconcile-partial`` (deshace exactamente las k que
        corrieron) o reintentar ``apply``, que retoma desde el checkpoint.
        """
        # AMBAS direcciones: un rollback a medio ejecutar (checkpoint 'down') deja la BD
        # tan desconocida como un apply a medias — stampear encima lo enmascara igual.
        incomplete = migration_progress.incomplete_progress_for_database(db_id, direction="up")
        incomplete += [
            {**row, "direction": "down"}
            for row in migration_progress.incomplete_progress_for_database(db_id, direction="down")
        ]
        if incomplete and not force:
            detail = ", ".join(
                f"migración {row['model_migration_id']} "
                f"({row['last_statement_index']}/{row['total_statements']} sentencias"
                + (" del rollback" if row.get("direction") == "down" else "")
                + ")"
                for row in incomplete
            )
            raise AppHttpException(
                message=(
                    f"No se puede stampear: hay una aplicación PARCIAL detectada ({detail}). "
                    "Vías correctas, en orden de preferencia: (1) "
                    "POST /migrations/reconcile-partial, que deshace EXACTAMENTE las "
                    "sentencias que sí se aplicaron y deja la BD en su versión anterior; "
                    "(2) reintentar 'apply', que retoma automáticamente desde la última "
                    "sentencia exitosa. NO stampees esta versión para después revertirla: "
                    "el stamp afirma que se aplicó COMPLETA, así que el rollback ejecutaría "
                    "todos los reversos contra una BD que solo tiene una fracción de los "
                    "cambios, y fallaría también. Si ya reconciliaste el estado físico a "
                    "mano, repetí el stamp con force=true."
                ),
                status_code=409,
                context={"managed_database_id": db_id, "incomplete_progress": incomplete},
                public_context={"incomplete_progress": incomplete},
            )

    # ------------------------------------------------------------------ #
    # Reconciliación de una aplicación PARCIAL                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _reconcile_plan(spec: MigrationSpec, engine: EngineType, progress_row: dict) -> dict:
        """
        Plan de reconciliación de UNA migración parcial: qué reversos hay que ejecutar.

        Requiere el MANIFIESTO (``model_migration_statements``): con solo los blobs
        ``up_sql``/``down_sql`` es imposible saber qué reverso corresponde a la sentencia
        ``k`` — el ``down_sql`` es una secuencia INDEPENDIENTE, con otra cantidad de
        sentencias (los cambios sin reverso simplemente no aparecen). Sin manifiesto se
        devuelve ``reconcilable=False`` con el motivo, en vez de adivinar.
        """
        runner = MigrationRunner()
        manifest = runner.usable_manifest(spec, engine)
        applied = int(progress_row["last_statement_index"])
        total = int(progress_row["total_statements"])
        if not manifest:
            return {
                "reconcilable": False,
                "reason": (
                    "esta versión no tiene manifiesto de sentencias para el motor destino "
                    "(migración escrita a mano o SQL editado): no se puede saber qué "
                    "reverso corresponde a cada sentencia aplicada"
                ),
                "count": 0, "unreversible": [], "inverses": [],
            }
        if len(manifest) != total:
            return {
                "reconcilable": False,
                "reason": (
                    f"el checkpoint habla de {total} sentencias y el manifiesto tiene "
                    f"{len(manifest)}: no coinciden, no se reconcilia a ciegas"
                ),
                "count": 0, "unreversible": [], "inverses": [],
            }
        # Sentencias efectivamente aplicadas (1..applied), en orden INVERSO.
        pending = [m for m in manifest if m.seq <= applied]
        pending.sort(key=lambda m: m.seq, reverse=True)
        unreversible = [
            {"seq": m.seq, "object_type": m.object_type, "object_name": m.object_name}
            for m in pending if not m.down_sql
        ]
        # Reversos que NO son demostrablemente seguros: existen, pero pueden fallar (una
        # UNIQUE/CHECK/FK que se re-crea VALIDA los datos actuales) o no restaurar los datos
        # (recrear una tabla borrada devuelve la estructura, no las filas). No bloquean —
        # son el mejor reverso disponible — pero el admin tiene que verlos en el dry-run.
        unconfirmed = [
            {
                "seq": m.seq,
                "object_type": m.object_type,
                "object_name": m.object_name,
                "destructive": m.destructive,
            }
            for m in pending if m.down_sql and not m.down_confirmed
        ]
        inverses: list[tuple[int, str]] = []
        for m in pending:
            if not m.down_sql:
                continue
            # Un reverso puede ser multi-sentencia (DROP nuevo; CREATE viejo): se parte,
            # porque cada exec_driver_sql admite una sola sentencia.
            for sql in split_sql_statements(m.down_sql):
                inverses.append((m.seq, sql))
        return {
            "reconcilable": not unreversible,
            "reason": None,
            "count": len(inverses),
            "unreversible": unreversible,
            "unconfirmed": unconfirmed,
            "inverses": inverses,
        }

    def reconcile_partial(
        self,
        db_id: int,
        *,
        confirm_version: str,
        dry_run: bool = False,
        force: bool = False,
        admin: dict | None = None,
    ) -> dict:
        """
        Deshace las sentencias que SÍ se aplicaron de una migración que falló a mitad.

        Deja la BD igual a lo que el ledger de Alembic ya afirma (la versión parcial nunca
        se registró), así que NO toca la tabla de versión: es una compensación, no un
        ``downgrade``. Después de esto la BD queda en un estado conocido y el ``rollback``
        normal vuelve a estar disponible.

        Doble intención: ``confirm_version`` debe ser la versión parcialmente aplicada (el
        admin tiene que haber mirado el estado antes). ``force=true`` procede aunque haya
        sentencias SIN reverso — las saltea y las reporta: sin eso, una sola sentencia
        irreversible dejaría al admin sin salida automática.
        """
        session = self._session()
        try:
            md, server, model = self._load_context(session, db_id)
            specs = self._load_specs(session, model.id)
            self._verify_integrity(specs)  # se va a ejecutar DDL destructivo
            engine = EngineType(engine_value(server))
            db_name, server_id = md.name, md.server_id
            target = build_target(server)
        finally:
            session.close()

        incomplete = migration_progress.incomplete_progress_for_database(db_id, direction="up")
        if not incomplete:
            raise AppHttpException(
                message="Esta BD no tiene ninguna aplicación parcial que reconciliar.",
                status_code=409,
                context={"managed_database_id": db_id},
            )
        by_id = {s.id: s for s in specs}
        # La versión MÁS ALTA primero: es la última que se intentó aplicar.
        rows = sorted(
            incomplete,
            key=lambda r: version_sort_key(
                by_id[r["model_migration_id"]].version
                if r["model_migration_id"] in by_id else "0"
            ),
            reverse=True,
        )
        row = rows[0]
        spec = by_id.get(row["model_migration_id"])
        if spec is None:
            raise AppHttpException(
                message=(
                    "La migración parcialmente aplicada ya no existe en el blueprint: no "
                    "hay reversos que ejecutar. Reconcilie el estado a mano y use "
                    "'stamp?force=true'."
                ),
                status_code=409,
                context={"model_migration_id": row["model_migration_id"]},
            )
        if confirm_version != spec.version:
            raise AppHttpException(
                message=(
                    "Confirmación requerida: 'confirm_version' debe coincidir con la "
                    f"versión parcialmente aplicada ({spec.version})."
                ),
                status_code=422,
                context={
                    "managed_database_id": db_id,
                    "partial_version": spec.version,
                    "required": "confirm_version == partial_version",
                },
            )

        plan = self._reconcile_plan(spec, engine, row)
        if not plan["inverses"] and not plan["reconcilable"]:
            raise AppHttpException(
                message=f"No se puede reconciliar automáticamente: {plan['reason']}.",
                status_code=409,
                context={"managed_database_id": db_id, "version": spec.version},
                public_context={"reason": plan["reason"]},
            )
        if plan["unreversible"] and not force:
            raise AppHttpException(
                message=(
                    f"{len(plan['unreversible'])} de las sentencias ya aplicadas no tienen "
                    "reverso: reconciliar dejaría esos cambios en la BD. Revíselos y "
                    "reintente con force=true para deshacer el resto, o reconcilie a mano."
                ),
                status_code=409,
                context={"managed_database_id": db_id, "version": spec.version},
                public_context={"unreversible_statements": plan["unreversible"]},
            )

        base = {
            "managed_database_id": db_id,
            "database_name": db_name,
            "server_id": server_id,
            "version": spec.version,
            "applied_statements": row["last_statement_index"],
            "total_statements": row["total_statements"],
            "statements_to_undo": len(plan["inverses"]),
            "unreversible_statements": plan["unreversible"],
            "unconfirmed_reverses": plan["unconfirmed"],
        }
        if dry_run:
            return {
                **base,
                "dry_run": True,
                "statements": [{"seq": seq, "sql": sql} for seq, sql in plan["inverses"]],
            }

        # Auditoría fail-closed ANTES de tocar el motor.
        audit.record_intent(
            "migration.reconcile_partial",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            detail=(
                f"reconciliar aplicación parcial de {spec.version}: deshacer "
                f"{len(plan['inverses'])} sentencia(s) de las "
                f"{row['last_statement_index']} aplicadas"
            ),
        )
        results = self.runner.reconcile_partial(
            target,
            db_name=db_name,
            engine=engine,
            managed_db_id=db_id,
            spec=spec,
            inverses=plan["inverses"],
            total_statements=row["total_statements"],
        )
        failed = any(r.status == "failed" for r in results)
        remaining = migration_progress.get_progress(db_id, spec.id, "up")
        fully_reconciled = not failed and remaining is None
        if fully_reconciled:
            # El plano físico volvió a coincidir con el ledger: la BD sale de cuarentena.
            self._set_quarantine(db_id, failed=False, results=[])
        audit.record(
            "migration.reconcile_partial",
            status="error" if failed else "success",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=True,
            detail=(
                f"{sum(1 for r in results if r.status == 'applied')}/"
                f"{len(plan['inverses'])} reverso(s) ejecutado(s) de {spec.version}"
                + (" (con fallo)" if failed else "")
                + (" — estado reconciliado" if fully_reconciled else "")
            ),
        )
        return {
            **base,
            "dry_run": False,
            "undone_count": sum(1 for r in results if r.status == "applied"),
            "failed": failed,
            "fully_reconciled": fully_reconciled,
            "remaining_applied_statements": (
                remaining.last_statement_index if remaining else 0
            ),
            "results": [
                {
                    "seq": r.index,
                    "status": r.status,
                    "error": r.error,
                    "execution_ms": r.execution_ms,
                }
                for r in results
            ],
        }

    def apply_all(
        self,
        model_id: int,
        *,
        max_databases: int,
        database_ids: list[int] | None = None,
        environment_id: int | None = None,
        force: bool = False,
        dry_run: bool = False,
        on_failure: str = "auto",
        allow_result_capture: bool = False,
        admin: dict | None = None,
    ) -> dict:
        """
        Aplica las pendientes a TODAS las BDs del blueprint (síncrono, acotado).
        Continúa con las demás BDs aunque una falle. El job asíncrono es del Plan 06.

        Optimización (evita trabajo N+1): carga y verifica ``specs`` UNA sola vez y
        cachea el ``ServerTarget`` por servidor (la credencial se descifra una vez por
        servidor, no por BD).
        """
        # Mismo guard que ``apply``: el parámetro llega ahora desde la ruta (antes se
        # aceptaba en la firma pero la ruta no lo exponía, así que siempre valía "auto" y el
        # selector del frontend no hacía nada). Se valida contra la MISMA tupla para que los
        # dos caminos no puedan divergir.
        if on_failure not in self._ON_FAILURE_MODES:
            raise AppHttpException(
                message="'on_failure' invalido.",
                status_code=422,
                context={"on_failure": on_failure, "allowed": list(self._ON_FAILURE_MODES)},
            )

        session = self._session()
        try:
            model = session.get(DatabaseModel, model_id)
            if model is None:
                raise AppHttpException(
                    message="Blueprint no encontrado.", status_code=404,
                    context={"model_id": model_id},
                )
            slug = model.slug
            specs = self._load_specs(session, model_id)
            self._guard_reviewed_baseline(session, model_id)
            # El gate de ``reviewed`` de la captura se evalúa por BD en ``_run_apply``, sobre
            # sus versiones pendientes reales: acá, a nivel de lote, una versión con captura
            # sin revisar que NINGUNA de estas BDs tiene pendiente frenaba el lote entero
            # (incluido el dry_run). El 409 sale igual, pero por BD y nombrando lo que esa BD
            # sí iba a ejecutar.
            total = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.model_id == model_id)
                .count()
            )
            scoped = session.query(
                ManagedDatabase.id, ManagedDatabase.name,
                ManagedDatabase.server_id, ManagedDatabase.status,
            ).filter(ManagedDatabase.model_id == model_id)
            if environment_id is not None:
                # El filtro va ANTES del ``limit`` de abajo, para que el tope no se consuma con
                # BDs de otros entornos y "aplicá a desarrollo" no quede recortado por
                # producción.
                scoped = scoped.filter(ManagedDatabase.environment_id == environment_id)
            if database_ids:
                # Los ids que NO pertenecen al blueprint se rechazan explícitamente en vez de
                # dejar que el `IN` los ignore en silencio. No es cosmética: es la frontera
                # que impide aplicar las migraciones de un blueprint a una BD ajena pasando
                # su id, y el resto del gateway es fail-closed en todas partes.
                valid = {
                    r[0]
                    for r in session.query(ManagedDatabase.id)
                    .filter(
                        ManagedDatabase.model_id == model_id,
                        ManagedDatabase.id.in_(database_ids),
                    )
                    .all()
                }
                unknown = sorted(set(database_ids) - valid)
                if unknown:
                    raise AppHttpException(
                        message=(
                            "Hay BDs que no pertenecen a este blueprint: "
                            f"{', '.join(str(i) for i in unknown)}."
                        ),
                        status_code=422,
                        context={"model_id": model_id, "unknown_database_ids": unknown},
                        public_context={"unknown_database_ids": unknown},
                    )
                # Mismo criterio fail-closed para el cruce con el filtro de entorno: un id que
                # SÍ pertenece al blueprint pero NO al entorno pedido desaparecería en silencio
                # del lote, que es exactamente el recorte callado que el bloque de arriba evita
                # a propósito.
                if environment_id is not None:
                    in_env = {
                        r[0]
                        for r in session.query(ManagedDatabase.id)
                        .filter(
                            ManagedDatabase.id.in_(database_ids),
                            ManagedDatabase.environment_id == environment_id,
                        )
                        .all()
                    }
                    outside = sorted(valid - in_env)
                    if outside:
                        raise AppHttpException(
                            message=(
                                "Hay BDs que no pertenecen al entorno indicado: "
                                f"{', '.join(str(i) for i in outside)}."
                            ),
                            status_code=422,
                            public_context={
                                "code": ecodes.CODE_DATABASES_OUTSIDE,
                                "database_ids_outside": outside,
                            },
                            context={"environment_id": environment_id},
                        )
                scoped = scoped.filter(ManagedDatabase.id.in_(database_ids))
            # Coincidencias ANTES del recorte. ``total_databases`` cuenta todas las BDs del
            # blueprint e ignora los filtros (contrato existente, no se toca porque la SPA lo
            # imprime literal), así que sin este número "3 de 40 procesadas" no dice si sobraron
            # 37 o si en ese entorno solo había 3.
            matched = scoped.count()
            db_rows = scoped.order_by(ManagedDatabase.id.asc()).limit(max_databases).all()
            dbs = [(r.id, r.name, r.server_id, r.status) for r in db_rows]
            # Política de entorno de TODO el lote en una query (anti-N+1), con valores planos:
            # la sesión se cierra unas líneas más abajo, antes del bucle.
            env_policies = self._env_policy_for(session, [d[0] for d in dbs])
            # ServerTarget + engine por servidor distinto (descifra credencial 1×/servidor).
            targets: dict[int, tuple] = {}
            for sid in {d[2] for d in dbs}:
                srv = get_server_or_404(session, sid)
                targets[sid] = (build_target(srv), EngineType(engine_value(srv)))
            # Guards por MOTOR presente en el lote. ``apply`` los corre para su única BD;
            # acá el lote puede ser HETEROGÉNEO (el mismo blueprint en servidores de motores
            # distintos), así que se validan una vez por motor distinto — antes se aplicaba
            # sin revisarlos y un blueprint atado a un motor solo se detectaba por el fallo
            # del propio motor, BD por BD.
            self._guard_gateway_internal_sql(specs)
            for _target, eng in {(None, e) for _t, e in targets.values()}:
                self._guard_cross_engine(session, model_id, eng)
                self._guard_untranslatable_sql(specs, eng)
        finally:
            session.close()

        if not specs:
            raise AppHttpException(
                message="El blueprint no tiene migraciones definidas.",
                status_code=422,
                context={"model_id": model_id},
            )
        self._verify_integrity(specs)  # una sola vez para todo el lote

        items: list[dict] = []
        denied: list[int] = []
        # El set de versiones destructivas depende del MOTOR (por los overrides por motor), y
        # un lote puede ser heterogéneo. Se memoiza por motor para no re-analizar el mismo SQL
        # por cada BD; ``migration_facts.analyze`` ya cachea, esto además evita rearmar el dict.
        _destructive_cache: dict[EngineType, dict[str, tuple[str, ...]]] = {}

        def destructive_by_engine(eng: EngineType) -> dict[str, tuple[str, ...]]:
            if eng not in _destructive_cache:
                _destructive_cache[eng] = self._destructive_versions(specs, eng)
            return _destructive_cache[eng]

        for db_id, name, server_id, status in dbs:
            target, engine = targets[server_id]
            item = {
                "managed_database_id": db_id, "database_name": name,
                "server_id": server_id, "applied": [], "ok": False,
            }
            policy = env_policies.get(db_id)
            item["environment_slug"] = policy[1] if policy else None
            try:
                quarantined = status == ProvisionStatus.error
                self._guard_quarantine(db_id, quarantined, force, dry_run)
                if dry_run:
                    plan = self._dry_run_plan(
                        db_id, name, server_id, target, slug, specs, None,
                        env_policy=policy,
                        destructive=destructive_by_engine(engine) if policy else None,
                    )
                    item["ok"] = True
                    item["pending_versions"] = plan["pending_versions"]
                    item["dry_run"] = True
                    item["blocked_by"] = plan["blocked_by"]
                else:
                    out = self._run_apply(
                        db_id, db_name=name, server_id=server_id, target=target,
                        engine=engine, slug=slug, specs=specs, model_id=model_id,
                        up_to_version=None,
                        was_quarantined=quarantined, admin=admin,
                        on_failure=on_failure,
                        allow_result_capture=allow_result_capture,
                        env_blocks_destructive=bool(policy and policy[0]),
                        env_slug=policy[1] if policy else None,
                        destructive_versions=(
                            destructive_by_engine(engine) if policy and policy[0] else None
                        ),
                    )
                    item["ok"] = not out["failed"]
                    item["applied"] = out["results"]
                    # Paridad con el apply por BD: sin esto, tras un apply masivo no había
                    # forma de saber en qué BDs quedaron capturas ni cómo llegar a ellas.
                    item["captured_select_count"] = out.get("captured_select_count", 0)
                    item["select_results_available"] = out.get(
                        "select_results_available", False
                    )
            except AppHttpException as exc:
                item["error"] = exc.message
                # El código estructurado se copia APARTE de la prosa. Este ``except`` se
                # quedaba solo con ``exc.message`` y tiraba el ``public_context`` a la basura,
                # así que para los guards del bucle el canal habitual (``public_context`` de la
                # respuesta HTTP) no existe: la ruta responde 200 con los ítems adentro. Sin
                # este campo el cliente vuelve a matchear prosa con expresiones regulares.
                item["error_code"] = (exc.public_context or {}).get("code")
                if item["error_code"] == ecodes.CODE_DESTRUCTIVE_BLOCKED:
                    item["blocked_by"] = list(
                        (exc.public_context or {}).get("blocked_versions") or []
                    )
                    denied.append(db_id)
                    audit.record(
                        "migration.environment_denied",
                        status="denied",
                        admin=admin,
                        target_type="managed_database",
                        target_id=db_id,
                        server_id=server_id,
                        touched_engine=False,
                        detail=(
                            f"guard=destructive environment={item['environment_slug']} "
                            f"versions={','.join(item['blocked_by'])}"
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 — una BD no debe abortar el lote
                logger.warning("apply_all: error inesperado en BD %s: %s", db_id, exc,
                               exc_info=True)
                item["error"] = f"error inesperado: {type(exc).__name__}"
            items.append(item)

        audit.record(
            "migration.apply_all", admin=admin, target_type="database_model",
            target_id=model_id, touched_engine=True,
            detail=f"{len(dbs)}/{total} BDs procesadas"
            + (f" (entorno {environment_id})" if environment_id is not None else "")
            + (f" — {len(denied)} denegadas por entorno" if denied else "")
            + (" (dry-run)" if dry_run else ""),
        )
        return {
            "model_id": model_id,
            "total_databases": total,
            "matched_databases": matched,
            "processed": len(dbs),
            "results": items,
        }

    # ------------------------------------------------------------------ #
    # Historial (lectura)                                                 #
    # ------------------------------------------------------------------ #
    def history(self, db_id: int, *, limit: int, offset: int) -> tuple[list[dict], int]:
        """Historial de aplicaciones de migraciones de una BD (más reciente primero)."""
        session = self._session()
        try:
            if session.get(ManagedDatabase, db_id) is None:
                raise AppHttpException(
                    message="Base de datos gestionada no encontrada.",
                    status_code=404,
                    context={"managed_database_id": db_id},
                )
            q = (
                session.query(DatabaseMigrationHistory, ModelMigration.version)
                .outerjoin(
                    ModelMigration,
                    ModelMigration.id == DatabaseMigrationHistory.model_migration_id,
                )
                .filter(DatabaseMigrationHistory.managed_database_id == db_id)
            )
            total = q.count()
            rows = (
                q.order_by(
                    DatabaseMigrationHistory.applied_at.desc(),
                    DatabaseMigrationHistory.id.desc(),
                )
                .limit(limit)
                .offset(offset)
                .all()
            )
            items = [
                {
                    "id": h.id,
                    "managed_database_id": h.managed_database_id,
                    "model_migration_id": h.model_migration_id,
                    "version": version,
                    "applied_at": h.applied_at,
                    "status": h.status.value if hasattr(h.status, "value") else h.status,
                    "error": h.error,
                    "execution_ms": h.execution_ms,
                }
                for h, version in rows
            ]
            return items, total
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Persistencia de resultados                                          #
    # ------------------------------------------------------------------ #
    def _record_history(self, db_id: int, results: list[MigrationResult]) -> None:
        if not results:
            return
        session = self._session()
        try:
            for r in results:
                session.add(
                    DatabaseMigrationHistory(
                        managed_database_id=db_id,
                        model_migration_id=r.migration_id,
                        applied_at=r.applied_at,
                        status=MigrationStatus(r.status),
                        error=r.error,
                        execution_ms=r.execution_ms,
                    )
                )
            session.commit()
        finally:
            session.close()

    def _sync_model_version_from_engine(
        self, db_id: int, target, db_name: str, slug: str
    ) -> None:
        """
        Sincroniza model_version releyendo la FUENTE DE VERDAD: la tabla de versión
        que Alembic mantiene dentro de la BD destino (no la contabilidad local).
        """
        current = self.runner.get_current_version(target, db_name, slug)
        self._set_model_version(db_id, current)

    def _set_model_version(self, db_id: int, version: str | None) -> None:
        session = self._session()
        try:
            md = session.get(ManagedDatabase, db_id)
            if md is not None:
                md.model_version = version
                session.commit()
        finally:
            session.close()

    def _set_quarantine(
        self, db_id: int, failed: bool, results: list[MigrationResult]
    ) -> None:
        """
        ROB1 — marca/limpia la cuarentena de la BD según el desenlace del apply:
        - failed → status=error + nota con la versión que falló (posible estado parcial).
        - éxito tras haber estado en error → vuelve a active y limpia la nota.
        """
        session = self._session()
        try:
            md = session.get(ManagedDatabase, db_id)
            if md is None:
                return
            if failed:
                bad = next((r for r in results if r.status == "failed"), None)
                md.status = ProvisionStatus.error
                md.notes = (
                    f"Migración {bad.version if bad else '?'} falló; posible estado "
                    f"parcial. Inspeccione y reintente con force=true."
                )
                session.commit()
            elif md.status == ProvisionStatus.error:
                md.status = ProvisionStatus.active
                md.notes = None
                session.commit()
        finally:
            session.close()

    @staticmethod
    def _result_dict(r: MigrationResult) -> dict:
        return {
            "migration_id": r.migration_id,
            "version": r.version,
            "status": r.status,
            "error": r.error,
            "execution_ms": r.execution_ms,
            # Checkpoint por sentencia: para que el frontend distinga un resume de un
            # intento desde cero, y sepa en qué sentencia murió (sin exponer SQL crudo).
            "resumed": r.resumed,
            "resumed_from_statement": r.resumed_from_statement,
            "statement_total": r.statement_total,
            "failed_at_statement_index": r.failed_at_statement_index,
        }
