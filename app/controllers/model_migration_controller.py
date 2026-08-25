"""
Controller de ModelMigration — migraciones versionadas de un blueprint.

CRUD puro sobre la BD de metadatos del gateway (NO toca ningún motor destino). Al
crear una migración:
- calcula el ``checksum`` de integridad,
- auto-traduce el ``up_sql`` a cada motor (campo calculado ``translated``),
- sugiere un ``down_sql`` (rollback) si la operación es aditiva.

La aplicación sobre BDs gestionadas vive en ``ManagedDatabaseController`` (toca el
motor) usando ``MigrationRunner``.
"""

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.core.context import current_http_identifier
from app.core.database import Database
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    SNAPSHOT_DATA_BATCH_ROWS,
    SNAPSHOT_DATA_MAX_BYTES_PER_TABLE,
    SNAPSHOT_DATA_MAX_ROWS_PER_TABLE,
    SNAPSHOT_DATA_MAX_TABLES,
    SNAPSHOT_MAX_SQL_PER_VERSION,
)
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.database_model import DatabaseModel
from app.models.enums import EngineType, MigrationStatus
from app.models.managed_database import ManagedDatabase
from app.models.model_migration import ModelMigration
from app.models.model_migration_statement import ModelMigrationStatement
from app.services import audit
from app.services import confirm_token as confirm_token_service
from app.services import migration_freeze_catalog as freeze_codes
from app.services.db_admin import migration_facts, migration_progress, migration_results
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.identifiers import references_gateway_internal_table
from app.services.db_admin.migration_integrity import compute_checksum, version_sort_key
from app.services.db_admin.migrations import MigrationRunner
from app.services.db_admin.sql_dialect import RollbackGenerator, SqlTranslator

logger = get_logger(__name__)

# Orden NUMÉRICO de versión en SQL: (longitud, valor) equivale al orden entero para
# strings de solo dígitos (incl. con ceros a la izquierda), evitando el bug del orden
# lexicográfico ("9999" > "10000"). Cross-engine (length() existe en los 4 motores).
_VERSION_ORDER_ASC = (func.length(ModelMigration.version), ModelMigration.version)
_VERSION_ORDER_DESC = (
    func.length(ModelMigration.version).desc(),
    ModelMigration.version.desc(),
)


class ModelMigrationController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)
        self._translator = SqlTranslator()
        self._rollback = RollbackGenerator()

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Serialización                                                       #
    # ------------------------------------------------------------------ #
    def _translated(self, m: ModelMigration) -> dict[str, str]:
        """SQL efectivo por motor (override si existe; si no, traducción).

        Las migraciones de DATOS (``kind='data'``) NO se traducen: la sintaxis upsert
        difiere por motor. Solo se reporta el SQL del motor con override presente.
        """
        out: dict[str, str] = {}
        if m.kind == "data":
            if m.up_sql_mysql:
                out["mysql"] = m.up_sql_mysql
            if m.up_sql_postgresql:
                out["postgresql"] = m.up_sql_postgresql
            return out
        out["mysql"] = m.up_sql_mysql or m.up_sql
        if m.up_sql_postgresql:
            out["postgresql"] = m.up_sql_postgresql
        else:
            pg = self._translator.translate(m.up_sql, EngineType.postgresql)
            if pg is not None:
                out["postgresql"] = pg
        return out

    def _serialize(self, m: ModelMigration, policy: dict) -> dict:
        """
        ``policy`` son las banderas de ``_policy_flags``. Es un parámetro OBLIGATORIO a
        propósito: con un default, un PATCH de solo el nombre sobre una versión congelada
        respondería ``sql_frozen: false`` y la UI la creería editable — exactamente el fallo
        que estos campos vienen a eliminar. Si falta, el error salta en el serializador.
        """
        return {
            **policy,
            "id": m.id,
            "model_id": m.model_id,
            "version": m.version,
            "name": m.name,
            "up_sql": m.up_sql,
            "up_sql_mysql": m.up_sql_mysql,
            "up_sql_postgresql": m.up_sql_postgresql,
            "down_sql": m.down_sql,
            "down_sql_suggested": m.down_sql_suggested,
            "translated": self._translated(m),
            "checksum": m.checksum,
            "kind": m.kind,
            "source_engine": m.source_engine,
            "is_baseline": m.is_baseline,
            "has_non_portable": m.has_non_portable,
            "reviewed": m.reviewed,
            "capture_selects": m.capture_selects,
            **self._sql_facts(m),
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }

    @staticmethod
    def _serialize_summary(m: ModelMigration, policy: dict) -> dict:
        return {
            **policy,
            "id": m.id,
            "model_id": m.model_id,
            "version": m.version,
            "name": m.name,
            "has_mysql_override": m.up_sql_mysql is not None,
            "has_postgresql_override": m.up_sql_postgresql is not None,
            "has_rollback": m.down_sql is not None,
            "checksum": m.checksum,
            "kind": m.kind,
            "is_baseline": m.is_baseline,
            "reviewed": m.reviewed,
            "capture_selects": m.capture_selects,
            **ModelMigrationController._sql_facts(m),
            "created_at": m.created_at,
        }

    @staticmethod
    def _sql_facts(m: ModelMigration) -> dict:
        """
        Hechos derivados del SQL para las insignias: siembra, COLLATE forzado, destructiva.

        Usa ``quick_facts`` (regex sobre el SQL enmascarado), NO el análisis con AST: esto se
        calcula por cada fila de cada página de versiones, y parsear hasta 256 KB por fila para
        decidir si se dibuja una plantita no se sostiene. El veredicto fino lo da el endpoint
        de validación cuando el usuario lo pide.

        No se persiste en columnas a propósito: haría falta recalcular en los dos puntos de
        escritura más un backfill, y la alternativa "calcular al leer y guardar" sería escribir
        dentro de un GET. La memoización por SQL —que es inmutable salvo PATCH, y el PATCH
        recalcula el checksum— da el mismo efecto sin tocar el esquema.
        """
        facts = migration_facts.quick_facts(m.up_sql)
        return {
            "has_seed": facts.has_seed,
            "forced_collations": list(facts.forced_collations),
            "destructive": facts.destructive,
        }

    # ------------------------------------------------------------------ #
    # Helpers internos                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _model_or_404(session, model_id: int) -> DatabaseModel:
        model = session.get(DatabaseModel, model_id)
        if not model:
            raise AppHttpException(
                message="Blueprint no encontrado.",
                status_code=404,
                context={"model_id": model_id},
            )
        return model

    @staticmethod
    def _migration_or_404(session, model_id: int, version: str) -> ModelMigration:
        m = (
            session.query(ModelMigration)
            .filter(
                ModelMigration.model_id == model_id,
                ModelMigration.version == version,
            )
            .first()
        )
        if not m:
            raise AppHttpException(
                message="Migración no encontrada para este blueprint.",
                status_code=404,
                context={"model_id": model_id, "version": version},
            )
        return m

    # ------------------------------------------------------------------ #
    # Vigencia de una versión: ¿alguna BD la tiene aplicada HOY?           #
    # ------------------------------------------------------------------ #
    # ``database_migration_history`` es un LOG DE EVENTOS, no un estado. Una fila
    # ``status='applied'`` dice "esta versión corrió con éxito sobre esta BD alguna vez", y
    # eso no se revoca nunca: ``_record_history`` se llama tanto desde el camino ``apply``
    # como desde el ``rollback``, y ninguno de los dos deja rastro de la DIRECCIÓN (ni
    # ``MigrationResult`` ni la tabla tienen columna ``direction``). Consecuencia: con solo
    # el historial, una versión revertida CORRECTAMENTE en todas las BDs quedaba congelada
    # de por vida, sin ninguna forma de editarla ni eliminarla — no existe purga de
    # historial, y el ``CASCADE`` que borraría esas filas cuelga del borrado de la migración,
    # que es justo lo que el historial bloquea.
    #
    # Por eso el historial pasa a ser el PRIMER filtro (barato, sin abrir conexiones) y la
    # decisión final la da la VERSIÓN ACTUAL de esas BDs. Las migraciones son forward-only
    # encadenadas: una BD en la versión N tiene aplicadas todas las <= N. Así que "la
    # versión V sigue vigente" es "alguna de las BDs que la aplicaron está hoy en V o
    # posterior".
    #
    # LÍMITE CONOCIDO Y ACEPTADO: ``stamp`` mueve el puntero sin ejecutar ni deshacer DDL,
    # así que una BD stampeada hacia atrás reporta una versión anterior conservando
    # físicamente los cambios de V. Para esa BD, desbloquear V permite borrar la DESCRIPCIÓN
    # de cambios que siguen en el motor. No es un descuido: ``stamp`` es la puerta trasera
    # declarada de cualquier gate basado en la versión, y el módulo ya lo documenta así.

    @staticmethod
    def _describe_blocking(blocking: list[dict]) -> str:
        """Frase legible con las BDs que bloquean, para el ``message`` del 409.

        El dato estructurado va igual en ``public_context['blocking_databases']``; esto es
        para que el mensaje diga CUÁL BD y POR QUÉ sin obligar a inspeccionar el payload. No
        se vuelca ningún mensaje del motor (criterio R4): solo el id y un motivo del
        vocabulario cerrado.
        """
        partes = []
        for row in blocking:
            db_id = row["managed_database_id"]
            reason = row.get("reason")
            if reason == freeze_codes.REASON_STILL_APPLIED:
                actual = row.get("current_version")
                partes.append(f"BD {db_id} está en la versión {actual}")
            elif reason == freeze_codes.REASON_UNREADABLE:
                partes.append(f"no se pudo leer la versión de la BD {db_id}")
            elif reason == freeze_codes.REASON_UNKNOWN_DATABASE:
                partes.append(f"la BD {db_id} tiene historial pero no está en el inventario")
            else:
                partes.append(f"no se pudo resolver el blueprint de la BD {db_id}")
        return ", ".join(partes) + "."

    @staticmethod
    def _applied_history_targets(session, migration_ids: list[int]) -> dict[int, set[int]]:
        """``{migration_id: {managed_database_id, ...}}`` con aplicación EXITOSA histórica.

        Una sola query para todo el lote. Las BDs dadas de baja del inventario no aparecen:
        el ``ondelete='CASCADE'`` de ``managed_database_id`` ya se llevó sus filas.
        """
        if not migration_ids:
            return {}
        rows = (
            session.query(
                DatabaseMigrationHistory.model_migration_id,
                DatabaseMigrationHistory.managed_database_id,
            )
            .filter(
                DatabaseMigrationHistory.model_migration_id.in_(migration_ids),
                DatabaseMigrationHistory.status == MigrationStatus.applied,
            )
            .distinct()
            .all()
        )
        out: dict[int, set[int]] = {}
        for migration_id, db_id in rows:
            out.setdefault(migration_id, set()).add(db_id)
        return out

    @staticmethod
    def _reaches_version(current: str | None, version: str) -> bool:
        """¿``current`` es la versión ``version`` o una posterior? Fail-closed.

        ``version_sort_key`` hace ``int(version)`` y nada garantiza que la versión CACHEADA
        en el inventario sea numérica (se escribió releyendo el motor, que puede traer
        cualquier cosa si alguien stampeó a mano). Un valor ilegible se trata como "sí la
        alcanza": ante la duda, la versión sigue congelada. Lo contrario permitiría borrar
        por un error de parseo.
        """
        if current is None:
            return False
        try:
            return version_sort_key(current) >= version_sort_key(version)
        except (TypeError, ValueError):
            return True

    @classmethod
    def _still_applied_cached(
        cls, session, migrations: list[ModelMigration]
    ) -> set[int]:
        """IDs de migración que HOY siguen aplicadas en alguna BD, según la CACHÉ.

        Usa ``ManagedDatabase.model_version`` (la caché del inventario) y no el motor: esto
        corre por cada fila de cada página del listado de versiones, y abrir una conexión por
        BD para pintar un botón no se sostiene. El veredicto autoritativo —el que ejecuta la
        mutación— lee el motor en vivo (``_still_applied_live``).

        La divergencia posible es en la dirección segura: si la caché quedó atrasada, el
        listado puede ofrecer un botón que después el guard rechaza con 409. Al revés no
        puede pasar sin que la caché sobreestime la versión, y eso solo congela de más.
        """
        targets = cls._applied_history_targets(session, [m.id for m in migrations])
        db_ids = {db_id for ids in targets.values() for db_id in ids}
        if not db_ids:
            return set()
        versions = dict(
            session.query(ManagedDatabase.id, ManagedDatabase.model_version)
            .filter(ManagedDatabase.id.in_(db_ids))
            .all()
        )
        still: set[int] = set()
        for m in migrations:
            for db_id in targets.get(m.id, ()):
                # Una BD con historial cuya fila ya no está en el inventario no debería
                # existir (CASCADE), pero si aparece se cuenta como vigente: no se puede
                # probar lo contrario.
                if db_id not in versions or cls._reaches_version(versions[db_id], m.version):
                    still.add(m.id)
                    break
        return still

    @classmethod
    def _still_applied_live(cls, session, m: ModelMigration) -> list[dict]:
        """BDs que HOY tienen aplicada ``m``, leyendo la versión DEL MOTOR.

        Es el veredicto autoritativo, y por eso abre conexiones: la caché del inventario la
        escriben ``apply``/``rollback``/``stamp`` releyendo el motor, pero nada garantiza que
        esté fresca, y acá se está por autorizar algo irreversible (borrar una versión, o
        cambiar el SQL de una que ya corrió).

        Fail-closed: una BD que no se puede leer —motor caído, base sin aprovisionar,
        credenciales rotas— cuenta como VIGENTE y aparece en la lista con ``reason``. Tratar
        un fallo de lectura como "ya no la tiene" convertiría una caída de red en permiso
        para borrar.

        Se devuelve la lista de BDs bloqueantes (vacía ⇒ la versión está libre), no un
        booleano, porque el 409 tiene que poder nombrar cuáles y por qué.
        """
        db_ids = cls._applied_history_targets(session, [m.id]).get(m.id, set())
        if not db_ids:
            return []
        model = session.query(DatabaseModel).filter(DatabaseModel.id == m.model_id).first()
        if model is None:  # sin slug no hay tabla de versión que leer
            return [{"managed_database_id": db_id, "reason": freeze_codes.REASON_UNKNOWN_BLUEPRINT}
                    for db_id in sorted(db_ids)]

        runner = MigrationRunner()
        blocking: list[dict] = []
        for db_id in sorted(db_ids):
            md = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.id == db_id)
                .first()
            )
            if md is None:
                blocking.append({"managed_database_id": db_id, "reason": freeze_codes.REASON_UNKNOWN_DATABASE})
                continue
            try:
                server = get_server_or_404(session, md.server_id)
                current = runner.get_current_version(
                    build_target(server), md.name, model.slug
                )
            except Exception:
                # No se distingue el motivo a propósito: el mensaje del motor puede llevar
                # host, usuario o fragmentos de sentencia (criterio R4 del módulo). El
                # detalle va al log con el Request ID.
                logger.exception(
                    "%s | no se pudo leer la versión de la BD %s al evaluar la vigencia "
                    "de la migración %s",
                    current_http_identifier.get(),
                    db_id,
                    m.version,
                )
                blocking.append(
                    {"managed_database_id": db_id, "reason": freeze_codes.REASON_UNREADABLE}
                )
                continue
            if cls._reaches_version(current, m.version):
                blocking.append(
                    {
                        "managed_database_id": db_id,
                        "reason": freeze_codes.REASON_STILL_APPLIED,
                        "current_version": current,
                    }
                )
        return blocking

    @staticmethod
    def _policy_flags(
        session, migrations: list[ModelMigration], *, latest_version: str | None
    ) -> dict[int, dict]:
        """
        Banderas de política por migración: ``sql_frozen``, ``deletable`` y ``block_reason``.

        Se devuelve la DECISIÓN, no sus insumos (conteos de historial). Si el cliente
        recibiera "cuántas BDs la aplicaron" y dedujera la regla por su cuenta, tendríamos la
        misma política escrita a los dos lados del contrato: el día que se afine aquí, la UI
        seguiría con el criterio viejo y volvería el problema que esto viene a resolver —
        descubrir al guardar que la operación no estaba permitida.

        Son las mismas condiciones que evalúan ``update_migration`` y ``delete_migration``:
          - ``sql_frozen``: alguna BD ya depende del SQL (aplicación exitosa) o hay una
            aplicación parcial a medias, que haría que un resume interpretara los índices del
            checkpoint contra un SQL distinto del que corrió.
          - ``deletable``: lo anterior, y además ser la punta de la secuencia.

        Dos queries en lote para toda la página (nada de N+1). No se usa ``func.count(...)
        .filter(...)``: ``FILTER`` es exclusivo de PostgreSQL y la BD de metadatos puede ser
        MySQL/MariaDB, además de SQLite en los tests.
        """
        ids = [m.id for m in migrations]
        if not ids:
            return {}
        # No alcanza con "tiene historial de aplicación exitosa": eso es un evento pasado que
        # nunca se revoca (el rollback escribe el MISMO status). Lo que congela es que alguna
        # BD dependa de la versión HOY. Ver el bloque de ``_still_applied_cached``.
        applied_ids = ModelMigrationController._still_applied_cached(session, migrations)
        partial_ids = migration_progress.migrations_with_incomplete_progress(ids, "up")
        # Una versión editada DESPUÉS de haberse aplicado ya no describe lo que esas BDs
        # tienen físicamente. No restringe nada (el SQL nuevo es el que corre de acá en
        # más): es la señal para que la UI no muestre la versión como si fuera fiel al
        # plano de todas sus BDs.
        diverged_ids = ModelMigrationController._divergent_migration_ids(session, ids)

        flags: dict[int, dict] = {}
        for m in migrations:
            applied = m.id in applied_ids
            partial = m.id in partial_ids
            is_tip = latest_version is None or m.version == latest_version
            if applied:
                reason = "applied"
            elif partial:
                reason = "partial"
            elif not is_tip:
                reason = "not_tip"
            else:
                reason = None
            flags[m.id] = {
                "sql_frozen": applied or partial,
                "deletable": reason is None,
                # 'not_tip' solo afecta al borrado: esa versión sí se puede editar.
                "block_reason": reason,
                "sql_diverged": m.id in diverged_ids,
            }
        return flags

    def _policy_for(self, session, m: ModelMigration) -> dict:
        """Banderas de política de UNA migración (atajo sobre ``_policy_flags``)."""
        latest = self._latest_version(session, m.model_id)
        return self._policy_flags(session, [m], latest_version=latest)[m.id]

    @staticmethod
    def _latest_version(session, model_id: int) -> str | None:
        """Versión punta del blueprint (orden numérico), para la bandera ``deletable``."""
        row = (
            session.query(ModelMigration.version)
            .filter(ModelMigration.model_id == model_id)
            .order_by(*_VERSION_ORDER_DESC)
            .first()
        )
        return row[0] if row else None

    @staticmethod
    def _failed_attempt_targets(session, migration_id: int) -> list[int]:
        """
        BDs distintas donde esta migración se intentó y FALLÓ (sin ninguna aplicación exitosa).

        Se usa para dejar constancia en auditoría de lo que el borrado va a descartar: al
        eliminar la migración, el ``ondelete="CASCADE"`` se lleva sus filas de
        ``database_migration_history``, que son el único registro estructurado de que esta
        versión rompió en esas BDs. La entrada de auditoría sobrevive al borrado; el historial no.
        """
        rows = (
            session.query(DatabaseMigrationHistory.managed_database_id)
            .filter(
                DatabaseMigrationHistory.model_migration_id == migration_id,
                DatabaseMigrationHistory.status == MigrationStatus.failed,
            )
            .distinct()
            .all()
        )
        return sorted(r[0] for r in rows)

    @staticmethod
    def _has_successful_application(session, migration_id: int) -> bool:
        """
        True si la migración se aplicó EXITOSAMENTE en al menos una BD (status=applied).

        Se filtra por ``applied`` y no por "tiene historial": un intento que solo FALLÓ deja
        fila en ``database_migration_history`` pero no cambió ninguna BD, así que su SQL
        todavía puede corregirse y la versión todavía puede borrarse. Lo que congela una
        versión es que alguna BD ya dependa de ella, no que se haya intentado aplicar.

        Es el predicado que gobierna tanto el 409 de edición del SQL como el de borrado.
        """
        return (
            session.query(DatabaseMigrationHistory)
            .filter(
                DatabaseMigrationHistory.model_migration_id == migration_id,
                DatabaseMigrationHistory.status == MigrationStatus.applied,
            )
            .first()
            is not None
        )

    # ------------------------------------------------------------------ #
    # Edición de una versión que YA está aplicada                          #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _resulting_checksum(m: ModelMigration, data: dict) -> str:
        """Checksum que la versión TENDRÁ si se aplica ``data``.

        Es el ``subject`` del ``confirm_token``, y por eso se calcula con la MISMA función
        que el checksum persistido: el token queda atado al contenido exacto que se
        previsualizó. Sin eso, ``(operación, model_id, version)`` es igual para cualquier
        edición de esa versión, así que se podría pedir el preview de una corrección inocua
        y mandar otra en el PATCH — que es justo lo que la confirmación debe impedir (mismo
        razonamiento que el ``subject`` de la consola SQL).

        Las reglas de asignación replican las de ``update_migration``, y tienen que seguir
        haciéndolo: se mira la PRESENCIA de la clave (el PATCH llega con
        ``exclude_unset=True``), salvo ``up_sql``, que con ``None`` no se asigna porque el
        SQL base no puede quedar vacío. Si divergen, el token deja de coincidir y el PATCH
        se rechaza con 422 — falla en la dirección segura, pero es ruido evitable.
        """
        up = data["up_sql"] if data.get("up_sql") is not None else m.up_sql
        # ``.get(k, default)`` y no ``if k in data``: con la clave PRESENTE y valor ``None``
        # (limpiar un override) devuelve None, que es justo el SQL efectivo resultante.
        my = data.get("up_sql_mysql", m.up_sql_mysql)
        pg = data.get("up_sql_postgresql", m.up_sql_postgresql)
        down = data.get("down_sql", m.down_sql)
        return compute_checksum(up, my, pg, down, m.version)

    @classmethod
    def _authorize_applied_edit(
        cls,
        m: ModelMigration,
        *,
        data: dict,
        confirm_version: str | None,
        confirm_token: str | None,
        blocking: list[dict],
        model_id: int,
        version: str,
    ) -> None:
        """Deja pasar la edición de una versión VIGENTE, o lanza el 409/422 que corresponda.

        El freeze NO se abre solo: sin los dos factores, la respuesta es el mismo 409
        ``model_migration.sql_frozen`` de siempre, con el mismo ``public_context``. Lo único
        que cambia es que el mensaje ahora NOMBRA la vía de excepción, porque un 409 que
        oculta la única salida obliga a leer el código para encontrarla.

        Los dos factores son los del resto del repo y cubren cosas distintas:
        ``confirm_version`` obliga a identificar CONSCIENTEMENTE qué versión se toca (molde
        de ``confirm_target_name``), y ``confirm_token`` da frescura, anti-replay y —vía
        ``subject``— ata la autorización al SQL exacto que se previsualizó.

        Se reusa ``confirm_token`` con ``server_id=model_id`` y ``db_name=version``. No es
        una identidad física de BD como en el resto de sus usos, pero el HMAC no interpreta
        esos campos: solo los firma. Se documenta acá para que nadie lea el token de una
        edición de blueprint como si apuntara a un servidor.
        """
        if not confirm_token:
            raise AppHttpException(
                message=(
                    "No se puede modificar el SQL: "
                    f"{cls._describe_blocking(blocking)} Cree una nueva versión para "
                    "corregir (fix-forward), o revierta esas BDs primero. Si la corrección "
                    "tiene que quedar en ESTA versión para que las BDs nuevas no repitan el "
                    "defecto, pida el preview de edición y reenvíe el PATCH con "
                    "'confirm_version' y 'confirm_token': la edición NO re-ejecuta nada, así "
                    "que esas BDs quedarán divergentes y el gateway lo registrará."
                ),
                status_code=409,
                public_context={
                    "code": freeze_codes.CODE_SQL_FROZEN,
                    "version": version,
                    "blocking_databases": blocking,
                    # La UI necesita distinguir "no se puede" de "se puede confirmando".
                    "override_available": True,
                },
                context={"model_id": model_id, "version": version},
            )
        if confirm_version != version:
            raise AppHttpException(
                message=(
                    "La confirmación no coincide: para editar una versión ya aplicada, "
                    f"'confirm_version' debe ser exactamente '{version}'."
                ),
                status_code=422,
                public_context={
                    "code": freeze_codes.CODE_EDIT_CONFIRM_MISMATCH,
                    "version": version,
                },
                context={"model_id": model_id, "version": version},
            )
        confirm_token_service.verify(
            confirm_token,
            freeze_codes.CONFIRM_OPERATION,
            model_id,
            version,
            subject=cls._resulting_checksum(m, data),
        )

    @staticmethod
    def _divergent_migration_ids(session, migration_ids: list[int]) -> set[int]:
        """IDs de versión cuyo SQL se editó DESPUÉS de haberse aplicado en alguna BD.

        Una sola query en lote para toda la página (mismo criterio que
        ``_applied_history_targets``: por fila serían N consultas para pintar una insignia).

        **No se filtra por ``status``** a propósito. La vía de edición escribe una entrada
        ``attempt`` fail-closed ANTES de commitear y una ``success`` después; si el proceso
        muere entre las dos, queda solo el ``attempt`` — y esa versión igual puede haber
        quedado divergente. Contar únicamente los ``success`` haría desaparecer justo el caso
        que peor se diagnostica. Fail-closed: marcar de más es ruido, marcar de menos es la
        mentira silenciosa que toda esta vía existe para evitar.
        """
        if not migration_ids:
            return set()
        rows = (
            session.query(AuditLog.target_id)
            .filter(
                AuditLog.action == freeze_codes.AUDIT_ACTION_EDITED_AFTER_APPLY,
                AuditLog.target_type == freeze_codes.AUDIT_TARGET_TYPE,
                AuditLog.target_id.in_(migration_ids),
            )
            .distinct()
            .all()
        )
        return {r[0] for r in rows if r[0] is not None}

    def preview_sql_edit(
        self, model_id: int, version: str, data: dict, *, admin: dict | None = None
    ) -> dict:
        """Emite el ``confirm_token`` para editar una versión, y dice a QUIÉN va a divergir.

        Es de solo lectura sobre el inventario, pero **sí lee la versión del motor** de cada
        BD con historial (``_still_applied_live``): el conjunto de BDs que van a quedar
        divergentes es la información por la que existe esta llamada, y una caché atrasada
        la volvería una promesa. Por eso también se audita.

        Si la versión NO está vigente en ninguna BD, se devuelve ``requires_confirmation``
        en False y **sin token**: ese caso ya lo permite el PATCH común, y emitir un token
        que no hace falta entrena a mandarlo siempre.
        """
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            m = self._migration_or_404(session, model_id, version)
            # ``touched_engine`` tiene que reflejar si se CONTACTÓ el motor, no si el
            # resultado fue bloqueante: con historial se abre conexión aunque después todas
            # las BDs estén por debajo de la versión y ``blocking`` salga vacío.
            consulted_engine = self._has_successful_application(session, m.id)
            blocking = self._still_applied_live(session, m) if consulted_engine else []
            resulting = self._resulting_checksum(m, data)
            # Se copia el id ANTES de cerrar: fuera del ``with`` la instancia queda
            # DETACHED y tocar un atributo puede intentar recargarlo sobre una sesión
            # cerrada. La auditoría va después del cierre para no sostener la conexión.
            migration_id = m.id
            token = expires = None
            if blocking:
                token, expires = confirm_token_service.issue(
                    freeze_codes.CONFIRM_OPERATION,
                    model_id,
                    version,
                    subject=resulting,
                )
        finally:
            session.close()
        audit.record(
            "migration.edit_preview",
            admin=admin,
            target_type=freeze_codes.AUDIT_TARGET_TYPE,
            target_id=migration_id,
            touched_engine=consulted_engine,
            detail=(
                f"preview de edición de la versión {version} del blueprint {model_id}: "
                f"{len(blocking)} BD(s) quedarían divergentes"
            ),
        )
        return {
            "model_id": model_id,
            "version": version,
            "requires_confirmation": bool(blocking),
            "blocking_databases": blocking,
            "resulting_checksum": resulting,
            "confirm_version": version,
            "confirm_token": token,
            "expires_at": expires,
        }

    # ------------------------------------------------------------------ #
    # Lectura                                                             #
    # ------------------------------------------------------------------ #
    def list_migrations(
        self, model_id: int, *, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            q = session.query(ModelMigration).filter(ModelMigration.model_id == model_id)
            total = q.count()
            rows = q.order_by(*_VERSION_ORDER_ASC).limit(limit).offset(offset).all()
            # La punta se busca sobre TODAS las versiones, no sobre la página: con paginación,
            # la última fila de la página no tiene por qué ser la última del blueprint.
            flags = self._policy_flags(
                session, rows, latest_version=self._latest_version(session, model_id)
            )
            return [self._serialize_summary(r, flags[r.id]) for r in rows], total
        finally:
            session.close()

    def get_migration(self, model_id: int, version: str) -> dict:
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            m = self._migration_or_404(session, model_id, version)
            return self._serialize(m, self._policy_for(session, m))
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Escritura                                                           #
    # ------------------------------------------------------------------ #
    # Reintentos al autoasignar versión: ante colisión por concurrencia (varios
    # colaboradores creando a la vez), se recalcula el siguiente número y se reintenta.
    _AUTO_VERSION_RETRIES = 5

    @staticmethod
    def _next_version(session, model_id: int) -> str:
        """
        Siguiente versión secuencial del blueprint = (máximo numérico actual) + 1, con
        padding a 4 dígitos ('0001', '0002'…). Usa el orden NUMÉRICO, no lexicográfico.
        """
        latest = (
            session.query(ModelMigration.version)
            .filter(ModelMigration.model_id == model_id)
            .order_by(*_VERSION_ORDER_DESC)
            .first()
        )
        next_n = (int(latest[0]) + 1) if latest else 1
        return f"{next_n:04d}"

    def create_migration(self, model_id: int, data: dict, *, admin: dict | None = None) -> dict:
        session = self._session()
        try:
            self._model_or_404(session, model_id)

            up_sql = data["up_sql"]
            up_mysql = data.get("up_sql_mysql")
            up_pg = data.get("up_sql_postgresql")
            down_sql = data.get("down_sql")
            self._reject_gateway_internal_sql(up_sql, up_mysql, up_pg, down_sql)
            # Sugerir rollback solo si el admin no proporcionó uno explícito. Un llamador
            # interno (p. ej. adopción de un diff) puede pasar un ``down_sql_suggested`` de
            # mejor calidad (derivado del estado "antes" exacto): se respeta si viene.
            suggested_override = data.get("down_sql_suggested")
            suggested = (
                suggested_override
                if suggested_override is not None
                else self._rollback.generate(up_sql)
            )
            # Parámetros OPCIONALES nuevos: preservan el comportamiento actual con sus
            # defaults (una migración escrita a mano nace portable, schema, revisada). Se
            # leen del ``data`` para no romper los call-sites existentes (la ruta pasa el
            # ``model_dump()`` del schema, que no incluye estas claves). ``is_baseline`` se
            # incluye además de los del plan porque el gate R1 (que protege el apply de DDL
            # capturado sin revisar) se activa por ``is_baseline=True``.
            source_engine = data.get("source_engine")
            has_non_portable = bool(data.get("has_non_portable", False))
            kind = data.get("kind") or "schema"
            reviewed = data.get("reviewed")
            reviewed = True if reviewed is None else bool(reviewed)
            is_baseline = bool(data.get("is_baseline", False))
            # Captura de resultados de SELECT: opt-in explícito. Una versión que extrae datos
            # de negocio nace SIEMPRE sin revisar, aunque el request diga lo contrario — la
            # aprobación tiene que ser un acto separado, sobre el SQL ya guardado (mismo
            # criterio que el gate R1 de los baselines de snapshot).
            capture_selects = bool(data.get("capture_selects", False))
            if capture_selects:
                reviewed = False

            # Versión: explícita si el admin la pasó; si no, autoasignada (secuencial).
            explicit_version = data.get("version")
            attempts = 1 if explicit_version else self._AUTO_VERSION_RETRIES
            migration = None
            last_exc: IntegrityError | None = None
            for _ in range(attempts):
                version = explicit_version or self._next_version(session, model_id)
                migration = ModelMigration(
                    model_id=model_id,
                    version=version,
                    name=data["name"],
                    up_sql=up_sql,
                    up_sql_mysql=up_mysql,
                    up_sql_postgresql=up_pg,
                    down_sql=down_sql,
                    down_sql_suggested=suggested,
                    # El checksum cubre la versión: se recalcula en cada intento.
                    checksum=compute_checksum(up_sql, up_mysql, up_pg, down_sql, version),
                    kind=kind,
                    source_engine=source_engine,
                    is_baseline=is_baseline,
                    has_non_portable=has_non_portable,
                    reviewed=reviewed,
                    capture_selects=capture_selects,
                )
                session.add(migration)
                try:
                    # flush hace visible la migración a _bump_model_version y detecta el
                    # conflicto de versión (UNIQUE) en la MISMA transacción.
                    session.flush()
                    self._write_statement_manifest(
                        session, migration.id, data.get("statements"), source_engine
                    )
                    self._bump_model_version(session, model_id)
                    session.commit()  # inserción + current_version en un único commit
                    break
                except IntegrityError as exc:
                    session.rollback()
                    last_exc = exc
                    if explicit_version:
                        # Versión EXPLÍCITA duplicada → 409 (no se reintenta).
                        raise AppHttpException(
                            message="Ya existe una migración con esa versión en el blueprint.",
                            status_code=409,
                            context={"model_id": model_id, "version": explicit_version},
                        ) from exc
                    # Autoasignada: colisión por concurrencia → recomputar y reintentar.
                    continue
            else:
                # Se agotaron los reintentos en modo autónomo (alta concurrencia).
                raise AppHttpException(
                    message=(
                        "No se pudo asignar una versión secuencial por concurrencia alta. "
                        "Reintenta la operación."
                    ),
                    status_code=409,
                    context={"model_id": model_id},
                ) from last_exc

            session.refresh(migration)
            result = self._serialize(migration, self._policy_for(session, migration))
            migration_id = migration.id
            assigned_version = migration.version
        finally:
            session.close()
        audit.record(
            "migration.create",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            detail=f"migración {assigned_version} creada (id={migration_id})",
        )
        return result

    @staticmethod
    def _reject_gateway_internal_sql(*sql_variants: str | None) -> None:
        """
        Rechaza (422) SQL de migración que nombre la contabilidad INTERNA del gateway.

        Ninguna migración de blueprint tiene un motivo legítimo para tocar
        ``_gw_v_{slug}`` (la tabla de versión de Alembic) ni ``_gw_stg_*`` (staging de la
        copia de datos): el gateway las administra en exclusiva.

        Fallo REAL que esto previene (2026-07-27): un diff estructural incluía la tabla de
        versión del destino y generaba ``DROP TABLE _gw_v_{slug}``. Al aplicarse, Alembic
        ejecutaba todo el DDL —incluido el DROP de su propia contabilidad— y moría al
        registrar la versión con ``(1146, "Table '..._gw_v_...' doesn't exist")``, dejando
        la BD con los cambios aplicados pero sin puntero de versión. La causa raíz se
        arregló excluyendo estas tablas del snapshot
        (``identifiers.GATEWAY_TABLE_PREFIXES``); esto es la barrera de creación, para que
        ni una migración escrita a mano pueda reintroducir el problema.
        """
        for sql in sql_variants:
            hits = references_gateway_internal_table(sql or "")
            if hits:
                raise AppHttpException(
                    message=(
                        "El SQL de la migración hace referencia a tablas internas del "
                        f"gateway ({', '.join(hits)}*), que administra el propio sistema "
                        "(la tabla de versión de Alembic y el staging de copia de datos). "
                        "Una migración nunca debe tocarlas: aplicarla dejaría la base de "
                        "datos sin puntero de versión. Quitá esas sentencias del SQL."
                    ),
                    status_code=422,
                    context={"gateway_internal_prefixes": hits},
                    public_context={"gateway_internal_prefixes": hits},
                )

    @staticmethod
    def _write_statement_manifest(
        session, migration_id: int, statements: list[dict] | None, source_engine: str | None
    ) -> None:
        """
        Persiste el MANIFIESTO de sentencias de la versión (una fila por sentencia, con su
        reverso emparejado). Ver ``app/models/model_migration_statement.py``.

        Se escribe en la MISMA transacción que la migración: o existen las dos cosas o
        ninguna (un manifiesto huérfano describiría un ``up_sql`` que no se guardó).

        Es OPCIONAL: solo lo provee el flujo que conoce el emparejamiento sentencia↔reverso
        (la adopción de un diff estructural). Requiere ``source_engine``: el manifiesto está
        renderizado para UN motor y el runner solo lo usa si coincide con el destino — sin
        saber para cuál es, no sirve, así que se ignora en silencio en vez de guardar filas
        que nunca se podrán validar.
        """
        if not statements or not source_engine:
            return
        for i, st in enumerate(statements, start=1):
            session.add(
                ModelMigrationStatement(
                    model_migration_id=migration_id,
                    seq=i,
                    engine=source_engine,
                    up_sql=st["up_sql"],
                    down_sql=st.get("down_sql"),
                    down_confirmed=bool(st.get("down_confirmed")),
                    object_type=st.get("object_type"),
                    object_name=(st.get("object_name") or "")[:512] or None,
                    op_group=(st.get("op_group") or "")[:600] or None,
                    destructive=bool(st.get("destructive")),
                )
            )

    # Motivos de omisión de datos-semilla que honran on_oversize="error".
    _OVERSIZE_REASONS = ("oversize_rows", "oversize_bytes")

    def create_from_snapshot(self, data: dict, *, admin: dict | None = None) -> dict:
        """
        Crea un blueprint NUEVO desde el snapshot de una BD existente (snapshot selectivo).

        Permite ELEGIR qué migrar: por tipo/nombre de objeto (include/exclude), y
        opcionalmente DATOS-semilla de tablas de catálogo (INSERT idempotente + rollback
        por PK). El resultado puede quedar en una sola migración (``single``), dividido
        por clase de objeto (``by_class``) o en buckets definidos por el usuario
        (``manual``, con validación topológica). Los datos van SIEMPRE en la(s) última(s)
        versión(es). Toda migración generada nace ``reviewed=False`` (R1) y atada al motor
        de origen si trae objetos no portables o datos.
        """
        from app.controllers.server_controller import ServerController
        from app.services.db_admin import snapshot_layout as layout_mod

        sc = ServerController()
        server_id, database = data["server_id"], data["database"]

        # 1) Dump EN VIVO (solo estructura) + filtros include/exclude.
        dump = sc.snapshot(server_id, database)
        if not dump.statements:
            raise AppHttpException(
                message="La base de datos no tiene objetos estructurales que fotografiar.",
                status_code=422,
                context={"database": database},
            )
        source_engine = dump.source_engine
        selected = layout_mod.filter_statements(
            dump.statements,
            include_types=data.get("include_object_types"),
            exclude_types=data.get("exclude_object_types"),
            include_objects=data.get("include_objects"),
            exclude_objects=data.get("exclude_objects"),
        )

        # 2) Datos-semilla (opt-in, con guardrails).
        seeds, skipped = self._extract_seeds(sc, server_id, database, selected, data)

        # 3) Distribución en versiones según el layout.
        layout = data.get("layout") or "single"
        seed_by_table = {s.table: s for s in seeds}
        if layout == "manual":
            violations = layout_mod.validate_manual_layout(
                selected, seed_by_table, data.get("manual_layout") or [],
                skipped_data_tables={s["table"] for s in skipped},
            )
            if violations:
                raise AppHttpException(
                    message=self._manual_layout_error_message(violations, skipped, data),
                    status_code=422,
                    context={"violations": violations, "skipped_tables": skipped},
                )
        version_plans = layout_mod.build_versions(
            layout=layout,
            selected=selected,
            seeds=seeds,
            baseline_name=data.get("baseline_name") or "Snapshot baseline",
            source_engine=source_engine,
            manual_buckets=data.get("manual_layout"),
        )
        if not version_plans:
            raise AppHttpException(
                message="No hay nada que capturar: los filtros excluyeron todos los objetos.",
                status_code=422,
                context={"database": database},
            )

        # 4) Tope de tamaño por versión (distinto del cap de creación manual).
        for i, vp in enumerate(version_plans, start=1):
            if len(vp.up_sql) > SNAPSHOT_MAX_SQL_PER_VERSION:
                raise AppHttpException(
                    message=(
                        f"La versión {i:04d} ('{vp.name}') supera el tope de SQL por versión "
                        f"({SNAPSHOT_MAX_SQL_PER_VERSION} bytes). Reduce la selección o los datos."
                    ),
                    status_code=422,
                    context={"version": f"{i:04d}", "kind": vp.kind},
                )

        confirm_data_rollback = bool(data.get("confirm_data_rollback"))
        model_id, model_result, version_summaries = self._persist_snapshot_versions(
            data, source_engine, version_plans, confirm_data_rollback
        )

        total = len(version_plans)
        audit.record(
            "database_model.from_snapshot",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            server_id=server_id,
            touched_engine=True,  # se leyó estructura (y datos si se pidieron)
            detail=(
                f"blueprint desde snapshot de '{database}' ({source_engine}, layout={layout}, "
                f"{total} versión(es), {len(seeds)} tabla(s) con datos)"
            ),
        )
        selected_counts: dict[str, int] = {}
        for s in selected:
            selected_counts[s.object_type] = selected_counts.get(s.object_type, 0) + 1
        return {
            "model": model_result,
            "baseline_version": "0001",
            "source_engine": source_engine,
            "has_non_portable": any(vp.has_non_portable for vp in version_plans),
            "object_counts": selected_counts,
            "statements_captured": len(selected),
            "total_versions": total,
            "data_tables_captured": len(seeds),
            "skipped_tables": skipped,
            "versions": version_summaries,
        }

    # Techo de violaciones detalladas en el mensaje (evita un `msg` gigante con layouts
    # muy rotos; el resto sigue disponible en context.violations en desarrollo).
    _MAX_VIOLATIONS_IN_MESSAGE = 5

    @classmethod
    def _manual_layout_error_message(
        cls, violations: list[dict], skipped: list[dict], data: dict
    ) -> str:
        """
        Arma un ``message`` AUTOCONTENIDO y accionable para el 422 de layout manual.

        A diferencia de ``context`` (que el handler global solo devuelve en
        ``APP_ENV=="development"``), ``message`` se incluye en TODAS las respuestas de
        error — por eso el detalle legible va aquí, no solo en ``context.violations``.
        """
        lines = [v["hint"] for v in violations[: cls._MAX_VIOLATIONS_IN_MESSAGE]]
        extra = len(violations) - len(lines)
        if extra > 0:
            lines.append(f"(+{extra} problema(s) más; corrige estos primero y reintenta.)")
        msg = "El layout manual no es aplicable:\n- " + "\n- ".join(lines)

        # Una tabla pedida en 'data_tables' que se omitió ANTES de validar el layout (sin
        # PK, sin filas, tipo no soportado, etc.) nunca genera una violación de layout —
        # simplemente no existe para la validación. Sin este aviso, esa tabla "desaparece"
        # de la respuesta sin explicación.
        requested = {d["table"] for d in (data.get("data_tables") or [])}
        silently_skipped = [s for s in skipped if s["table"] in requested]
        if silently_skipped:
            detail = ", ".join(f"{s['table']} ({s['reason']})" for s in silently_skipped)
            msg += (
                "\nAdemás, estas tablas de 'data_tables' no se pudieron extraer y por eso "
                f"no aparecen en la lista anterior: {detail}."
            )
        return msg

    def _extract_seeds(self, sc, server_id, database, selected, data):
        """Extrae los datos-semilla pedidos, aplicando guardrails y on_oversize."""
        data_tables = data.get("data_tables") or []
        if not data_tables:
            return [], []
        if len(data_tables) > SNAPSHOT_DATA_MAX_TABLES:
            raise AppHttpException(
                message=(
                    f"Se pidieron datos de {len(data_tables)} tablas; el máximo es "
                    f"{SNAPSHOT_DATA_MAX_TABLES}. Los blueprints siembran catálogos, no datos masivos."
                ),
                status_code=422,
                context={"requested": len(data_tables), "max": SNAPSHOT_DATA_MAX_TABLES},
            )
        # La estructura de cada tabla sembrada DEBE estar incluida (el INSERT se aplica
        # después del CREATE TABLE de la misma migración/versión anterior).
        selected_tables = {s.name for s in selected if s.object_type == "table"}
        table_names = [d["table"] for d in data_tables]
        missing = [t for t in table_names if t not in selected_tables]
        if missing:
            raise AppHttpException(
                message=(
                    "No se puede sembrar datos de tablas cuya estructura no está incluida "
                    f"en el blueprint: {', '.join(missing)}."
                ),
                status_code=422,
                context={"tables": missing},
            )
        modes = {d["table"]: d.get("mode") or "upsert" for d in data_tables}
        results = sc.snapshot_data(
            server_id, database, table_names, modes=modes,
            max_rows=SNAPSHOT_DATA_MAX_ROWS_PER_TABLE,
            max_bytes=SNAPSHOT_DATA_MAX_BYTES_PER_TABLE,
            batch_rows=SNAPSHOT_DATA_BATCH_ROWS,
        )
        on_oversize = data.get("on_oversize") or "skip"
        seeds, skipped = [], []
        for res in results:
            if res.included:
                seeds.append(res)
                continue
            skipped.append({"table": res.table, "reason": res.reason})
            if on_oversize == "error" and (res.reason or "") in self._OVERSIZE_REASONS:
                raise AppHttpException(
                    message=(
                        f"La tabla '{res.table}' supera el guardrail de datos ({res.reason}). "
                        "Reduce el volumen o usa on_oversize='skip'."
                    ),
                    status_code=422,
                    context={"table": res.table, "reason": res.reason},
                )
        return seeds, skipped

    def _persist_snapshot_versions(
        self, data, source_engine, version_plans, confirm_data_rollback
    ) -> tuple[int, dict, list[dict]]:
        """Crea el blueprint + las N migraciones en una sola transacción."""
        last_version = f"{len(version_plans):04d}"
        session = self._session()
        try:
            model = DatabaseModel(
                name=data["name"],
                slug=data["slug"],
                description=data.get("description"),
                current_version=last_version,
                is_active=True,
            )
            session.add(model)
            try:
                session.flush()  # asigna id y detecta conflicto de name/slug
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un blueprint con ese nombre o slug.",
                    status_code=409,
                    context={"slug": data.get("slug")},
                ) from exc

            summaries: list[dict] = []
            for i, vp in enumerate(version_plans, start=1):
                version = f"{i:04d}"
                up_mysql = vp.up_sql if source_engine in ("mysql", "mariadb") else None
                up_pg = vp.up_sql if source_engine == "postgresql" else None
                # Datos: down_sql confirmado solo si el admin lo pidió (fail-closed).
                # Estructura: nunca se confirma automáticamente (solo sugerido).
                down_sql = (
                    vp.down_sql_suggested
                    if (vp.kind == "data" and confirm_data_rollback)
                    else None
                )
                session.add(
                    ModelMigration(
                        model_id=model.id,
                        version=version,
                        name=vp.name[:200],
                        up_sql=vp.up_sql,
                        up_sql_mysql=up_mysql,
                        up_sql_postgresql=up_pg,
                        down_sql=down_sql,
                        down_sql_suggested=vp.down_sql_suggested,
                        checksum=compute_checksum(vp.up_sql, up_mysql, up_pg, down_sql, version),
                        kind=vp.kind,
                        source_engine=source_engine,
                        is_baseline=True,
                        has_non_portable=vp.has_non_portable,
                        reviewed=False,
                    )
                )
                summaries.append(
                    {
                        "version": version,
                        "kind": vp.kind,
                        "name": vp.name[:200],
                        "object_counts": vp.object_counts,
                        "has_non_portable": vp.has_non_portable,
                    }
                )
            session.commit()
            session.refresh(model)
            return model.id, self._serialize_model(model), summaries
        finally:
            session.close()

    @staticmethod
    def _serialize_model(m: DatabaseModel) -> dict:
        return {
            "id": m.id,
            "name": m.name,
            "slug": m.slug,
            "description": m.description,
            "current_version": m.current_version,
            "is_active": m.is_active,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }

    def update_migration(
        self, model_id: int, version: str, data: dict, *, admin: dict | None = None
    ) -> dict:
        # Se SACAN de ``data`` antes de cualquier otra cosa: son factores de
        # autorización, no campos de la migración, y dejarlos adentro los expondría a
        # los bucles de asignación de más abajo.
        confirm_version = data.pop("confirm_version", None)
        confirm_token = data.pop("confirm_token", None)
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            m = self._migration_or_404(session, model_id, version)
            applied_successfully = self._has_successful_application(session, m.id)

            # El SQL efectivo (base u overrides) NO puede cambiar si ya se aplicó
            # EXITOSAMENTE en alguna BD: editarlo aquí no re-ejecuta nada en el motor, así
            # que la metadata divergiría de lo que realmente corrió. Un intento que solo
            # falló no congela el SQL (ninguna BD depende de él). Fix-forward si ya se aplicó.
            # Se mira la PRESENCIA de la clave, no que traiga valor (el PATCH llega con
            # ``exclude_unset=True``): limpiar un override con ``null`` CAMBIA el SQL efectivo
            # de ese motor —pasa a usarse la traducción del ``up_sql`` base— y por lo tanto
            # cuenta como cambio de SQL. Con la comparación ``is not None`` había un camino
            # real para saltear la aprobación de una versión con captura: ``up_sql`` que extrae
            # datos + ``up_sql_postgresql='SELECT 1'`` → PATCH ``reviewed=true`` (se aprueba lo
            # inocuo) → PATCH ``{"up_sql_postgresql": null}`` → la aprobación sobrevivía y el
            # SQL efectivo para PostgreSQL pasaba a ser el que extrae datos.
            # ``up_sql: null`` es la excepción: la asignación de abajo lo ignora (el SQL base no
            # puede quedar vacío), así que no cambia nada y no debe congelar ni purgar.
            sql_fields_changing = ("up_sql" in data and data["up_sql"] is not None) or any(
                f in data for f in ("up_sql_mysql", "up_sql_postgresql")
            )
            # El ``down_sql`` va APARTE de ``sql_fields_changing`` a propósito: confirmar el
            # rollback DESPUÉS de aplicar es un flujo documentado y soportado (el 409 de
            # ``rollback`` pide exactamente eso), así que no puede caer bajo la barrera de
            # "ya aplicado ⇒ no se toca el SQL" ni purgar el manifiesto. Pero SÍ cuenta para
            # invalidar la aprobación de una versión con captura: el ``down_sql`` también se
            # ejecuta y también captura (ver ``capture_review_reset`` más abajo). Se usa
            # ``in data`` (el PATCH llega con ``exclude_unset=True``) para cubrir también el
            # caso de LIMPIAR el rollback con ``null``.
            down_sql_changing = "down_sql" in data
            # El historial es solo el PRIMER filtro (barato). Que la versión haya corrido
            # alguna vez no la congela: lo que la congela es que alguna BD dependa de ella
            # HOY, y eso se confirma leyendo el motor. La lectura en vivo se hace únicamente
            # cuando de verdad se está por cambiar el SQL, para no abrir conexiones en un
            # PATCH que solo toca el nombre.
            # ``diverging`` queda con las BDs que este PATCH vuelve divergentes: ya no
            # van a coincidir con el SQL que la versión declara. Es lo que se audita.
            diverging: list[dict] = []
            if applied_successfully and sql_fields_changing:
                blocking = self._still_applied_live(session, m)
                if blocking:
                    self._authorize_applied_edit(
                        m,
                        data=data,
                        confirm_version=confirm_version,
                        confirm_token=confirm_token,
                        blocking=blocking,
                        model_id=model_id,
                        version=version,
                    )
                    diverging = blocking

            # Una aplicación PARCIAL (checkpoint de sentencia incompleto: algunas
            # sentencias del up_sql ACTUAL ya commitearon en alguna BD, pero no todas)
            # tampoco es segura de editar, aunque `_has_successful_application` diga
            # False (solo mira aplicaciones EXITOSAS). Si se permitiera, un resume
            # posterior interpretaría los índices del checkpoint contra un SQL distinto
            # del que efectivamente corrió — corrupción silenciosa de esquema.
            if sql_fields_changing:
                incomplete = migration_progress.incomplete_progress_for_migration(
                    m.id, direction="up"
                )
                if incomplete:
                    detail = ", ".join(
                        f"BD {row['managed_database_id']} "
                        f"({row['last_statement_index']}/{row['total_statements']} sentencias)"
                        for row in incomplete
                    )
                    raise AppHttpException(
                        message=(
                            "No se puede modificar el SQL: hay una aplicación PARCIAL en "
                            f"curso ({detail}). Reintente 'apply' sobre esa BD (retoma "
                            "automáticamente) hasta que complete, o límpielo con "
                            "'stamp?force=true' antes de editar."
                        ),
                        status_code=409,
                        context={
                            "model_id": model_id,
                            "version": version,
                            "incomplete_progress": incomplete,
                        },
                    )

            # Misma barrera que en la creación: una corrección manual tampoco puede
            # introducir SQL contra la contabilidad interna del gateway.
            self._reject_gateway_internal_sql(
                data.get("up_sql"), data.get("up_sql_mysql"),
                data.get("up_sql_postgresql"), data.get("down_sql"),
            )

            # Rastro de la divergencia: FAIL-CLOSED (si no se puede persistir,
            # ``record_intent`` lanza 500 y la edición NO ocurre) y en ESTE punto exacto,
            # que no es casual. Va DESPUÉS de todos los guards —si alguno rechazara luego,
            # quedaría un 'attempt' marcando como divergente una versión que nadie llegó a
            # tocar— y ANTES de la primera mutación, porque a partir de la próxima línea la
            # transacción toma el lock de escritura sobre la BD de metadatos y la sesión
            # aparte que abre ``record_intent`` no podría commitear (en SQLite es un
            # 'database is locked' duro; en MySQL/PG funcionaría, y esa diferencia es
            # justamente la que haría que el fallo apareciera recién en los tests).
            #
            # Que sea fail-closed es deliberado: el valor entero de esta vía está en que la
            # divergencia quede registrada. Una edición de una versión vigente SIN rastro es
            # exactamente el daño que el freeze impedía, así que perder el registro tiene
            # que costar la operación. Mismo criterio que ``reveal_password`` y la descarga
            # de un export.
            if diverging:
                previous_checksum = m.checksum
                divergent_ids = ", ".join(
                    str(row["managed_database_id"]) for row in diverging
                )
                audit.record_intent(
                    freeze_codes.AUDIT_ACTION_EDITED_AFTER_APPLY,
                    admin=admin,
                    target_type=freeze_codes.AUDIT_TARGET_TYPE,
                    target_id=m.id,
                    touched_engine=False,
                    detail=(
                        f"blueprint {model_id}: se edita el SQL de la versión {version}, "
                        f"vigente en {len(diverging)} BD(s). La edición NO re-ejecuta "
                        f"nada: esas BDs conservan FÍSICAMENTE lo que ya corrió y la "
                        f"versión deja de describirlas. BDs divergentes: {divergent_ids}. "
                        f"checksum previo {previous_checksum}"
                    ),
                )

            if "name" in data and data["name"] is not None:
                m.name = data["name"]
            if "up_sql" in data and data["up_sql"] is not None:
                # Al cambiar el SQL base, un override por-motor que NO se re-envíe en este
                # mismo PATCH quedaría obsoleto (gana en _translated sobre el nuevo up_sql).
                # Exigir intención explícita: reenviar el override corregido o limpiarlo
                # (null) en la misma llamada. Evita que quede SQL viejo aplicándose en silencio.
                stale = [
                    f
                    for f in ("up_sql_mysql", "up_sql_postgresql")
                    if getattr(m, f) is not None and f not in data
                ]
                if stale:
                    raise AppHttpException(
                        message=(
                            "Al cambiar 'up_sql' debes reenviar (corregido) o limpiar "
                            f"(null) los overrides que quedarían obsoletos: {', '.join(stale)}."
                        ),
                        status_code=409,
                        context={"model_id": model_id, "version": version, "stale_overrides": stale},
                    )
                # Cascade: al corregir el SQL base se regenera el rollback SUGERIDO
                # (la traducción cross-engine se recalcula al vuelo en _translated, no
                # hay campo persistido que actualizar). El down_sql CONFIRMADO no se toca.
                m.up_sql = data["up_sql"]
                m.down_sql_suggested = self._rollback.generate(m.up_sql)
            if "down_sql" in data:
                m.down_sql = data["down_sql"]
            if "up_sql_mysql" in data:
                m.up_sql_mysql = data["up_sql_mysql"]
            if "up_sql_postgresql" in data:
                m.up_sql_postgresql = data["up_sql_postgresql"]
            # R1: aprobación del baseline (revisión del DDL capturado). No es un campo
            # de SQL, así que se permite aunque la migración ya esté aplicada en alguna BD.
            reviewed_approved = False
            if data.get("reviewed") is not None:
                reviewed_approved = bool(data["reviewed"]) and not m.reviewed
                m.reviewed = bool(data["reviewed"])

            # ACTIVAR la captura de resultados cambia lo que la migración hace con los datos
            # del cliente, así que invalida cualquier aprobación previa: hay que re-aprobarla
            # sabiendo que va a extraer filas. Se aplica DESPUÉS del bloque de `reviewed`
            # para que gane sobre un `reviewed=true` enviado en la misma llamada.
            capture_enabled_now = False
            if data.get("capture_selects") is not None:
                capture_enabled_now = bool(data["capture_selects"]) and not m.capture_selects
                m.capture_selects = bool(data["capture_selects"])
                if capture_enabled_now:
                    m.reviewed = False
                    reviewed_approved = False

            # ...y CAMBIAR EL SQL de una versión con captura invalida la aprobación por el
            # mismo motivo: lo que se aprobó fue una CONSULTA CONCRETA, no la versión como
            # entidad. Sin esto había un camino real: crear con capture_selects=true y
            # up_sql='SELECT 1' → PATCH reviewed=true (aprobación legítima de algo inocuo) →
            # PATCH up_sql='SELECT * FROM clientes' (permitido mientras no haya una
            # aplicación EXITOSA) → apply pasaba el gate de reviewed sin que nadie hubiera
            # visto la consulta que realmente se iba a ejecutar y capturar.
            # Cuenta AMBAS direcciones (up_sql/overrides y down_sql): el codegen emite la
            # captura también para las sentencias del down_sql.
            # Se evalúa DESPUÉS del bloque de capture_selects para leer el valor final de
            # ``m.capture_selects`` (el PATCH puede activarla en esta misma llamada) y para
            # que el reset gane sobre un ``reviewed=true`` enviado en la MISMA llamada —
            # mismo criterio que ``capture_enabled_now``: aprobar y reescribir en un solo
            # request no es una revisión verificable.
            capture_review_reset = (
                (sql_fields_changing or down_sql_changing)
                and m.capture_selects
                and m.reviewed
            )
            if capture_review_reset:
                m.reviewed = False
                reviewed_approved = False

            # El MANIFIESTO de sentencias (``model_migration_statements``) describe el
            # ``up_sql`` que se guardó al crear la versión: si el SQL cambia, el
            # emparejamiento sentencia↔reverso y los índices ``seq`` ya no corresponden.
            # Se BORRA en vez de intentar re-derivarlo: un manifiesto desalineado haría
            # que una reconciliación parcial deshiciera la sentencia equivocada
            # (corrupción silenciosa). La migración vuelve al modo todo-o-nada.
            purged_captures = 0
            if sql_fields_changing:
                session.query(ModelMigrationStatement).filter(
                    ModelMigrationStatement.model_migration_id == m.id
                ).delete(synchronize_session=False)
            # Mismo razonamiento para las capturas de resultados de SELECT: describen el
            # resultado de la sentencia número k de un SQL que dejó de existir, y su
            # ``statement_index`` ya no apunta a lo mismo. Se purgan en ESTA transacción
            # (no quedan colgadas de un SQL inexistente) — y de paso dejan de retener
            # datos de negocio que ya no explican nada.
            #
            # Incluye el ``down_sql``, que NO entra en ``sql_fields_changing`` (ese flag
            # gobierna el freeze de "ya aplicada" y el borrado del manifiesto del ``up``):
            # cambiar el rollback reordena ``down_statements``, así que las capturas de
            # dirección ``down`` quedarían con su ``statement_index`` apuntando a otra
            # sentencia. El checksum las marcaba ``stale=true`` en la lectura, pero una fila
            # incoherente que hay que interpretar es peor que ninguna.
            if sql_fields_changing or down_sql_changing:
                purged_captures = migration_results.purge_for_migration(m.id, session=session)

            # Recalcular checksum si cambió alguna variante de SQL o el rollback.
            m.checksum = compute_checksum(
                m.up_sql, m.up_sql_mysql, m.up_sql_postgresql, m.down_sql, m.version
            )
            session.commit()
            session.refresh(m)
            result = self._serialize(m, self._policy_for(session, m))
        finally:
            session.close()
        audit.record(
            "migration.update",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            detail=(
                f"migración {version} actualizada"
                + (
                    f" (purgadas {purged_captures} captura(s) de resultados de SELECT)"
                    if purged_captures
                    else ""
                )
                + (" — captura de resultados ACTIVADA (requiere re-aprobación)"
                   if capture_enabled_now else "")
                # El motivo del reset se audita igual que el de la activación: sin esto, la
                # traza mostraría una versión que pasó de reviewed=true a false sin explicar
                # por qué, y es justo la transición que protege datos de negocio.
                + (" — SQL modificado en una versión con captura: aprobación (reviewed) "
                   "REVOCADA, hay que re-aprobar la consulta nueva"
                   if capture_review_reset else "")
            ),
        )
        if diverging:
            # Segunda entrada, ya con el checksum nuevo: cierra el par attempt/success.
            # ``_divergent_migration_ids`` NO filtra por status justamente para que un
            # proceso que muera entre las dos siga marcando la versión como divergente.
            audit.record(
                freeze_codes.AUDIT_ACTION_EDITED_AFTER_APPLY,
                admin=admin,
                target_type=freeze_codes.AUDIT_TARGET_TYPE,
                target_id=result["id"],
                touched_engine=False,
                detail=(
                    f"blueprint {model_id}: versión {version} editada estando vigente en "
                    f"{len(diverging)} BD(s); checksum nuevo {result['checksum']}"
                ),
            )
        if reviewed_approved:
            audit.record(
                "migration.review",
                admin=admin,
                target_type="database_model",
                target_id=model_id,
                detail=f"baseline {version} revisado y aprobado para aplicar",
            )
        return result

    def delete_migration(self, model_id: int, version: str, *, admin: dict | None = None) -> None:
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            m = self._migration_or_404(session, model_id, version)
            # Mismo criterio que `update_migration`: lo que congela una versión es que
            # ALGUNA BD ya dependa de ella, no que se haya intentado aplicar. Antes se usaba
            # `_has_history`, que cuenta también los intentos fallidos: una versión que
            # reventó en la primera sentencia (sin tocar ninguna BD) quedaba imborrable para
            # siempre, porque no existe purga de historial. La única salida era editarla y
            # dejarla inerte con un `SELECT ''`.
            if self._has_successful_application(session, m.id):
                # Mismo criterio que ``update_migration``: el historial es un log de eventos
                # que nunca se revoca, así que solo sirve de primer filtro. El veredicto lo da
                # la versión ACTUAL leída del motor.
                blocking = self._still_applied_live(session, m)
                if blocking:
                    raise AppHttpException(
                        message=(
                            "No se puede eliminar la versión: "
                            f"{self._describe_blocking(blocking)} Eliminarla no revierte "
                            "nada en el motor: esas BDs quedarían con objetos que ninguna "
                            "versión del blueprint describe. Revierta primero, o cree una "
                            "migración compensatoria."
                        ),
                        status_code=409,
                        public_context={
                            "code": freeze_codes.CODE_STILL_APPLIED,
                            "version": version,
                            "blocking_databases": blocking,
                        },
                        context={"model_id": model_id, "version": version},
                    )
            # Un intento fallido puede haber dejado sentencias commiteadas a medias. El
            # checkpoint es la única evidencia de cuáles, y el CASCADE se lo llevaría en
            # silencio: sin él, esa BD queda sucia y sin forma de reconciliarla. Es la misma
            # barrera que `update_migration` ya impone antes de tocar el SQL.
            incomplete = migration_progress.incomplete_progress_for_migration(
                m.id, direction="up"
            )
            if incomplete:
                detail = ", ".join(
                    f"BD {row['managed_database_id']} "
                    f"({row['last_statement_index']}/{row['total_statements']} sentencias)"
                    for row in incomplete
                )
                raise AppHttpException(
                    message=(
                        "No se puede eliminar: hay una aplicación PARCIAL de esta versión "
                        f"sin resolver ({detail}). Reconcilie esa BD "
                        "('reconcile-partial') o complete el 'apply' antes de eliminarla."
                    ),
                    status_code=409,
                    context={
                        "model_id": model_id,
                        "version": version,
                        "incomplete_progress": incomplete,
                    },
                )
            discarded = self._failed_attempt_targets(session, m.id)
            # Solo se puede eliminar la ÚLTIMA versión (la punta de la secuencia). Borrar
            # una intermedia dejaría un hueco y una versión posterior podría depender de
            # ella (forward-only encadenado). Para tocar una intermedia: edita, o revierte
            # hasta ahí y recréala.
            latest = (
                session.query(ModelMigration.version)
                .filter(ModelMigration.model_id == model_id)
                .order_by(*_VERSION_ORDER_DESC)
                .first()
            )
            if latest and version_sort_key(latest[0]) > version_sort_key(m.version):
                raise AppHttpException(
                    message=(
                        f"Solo se puede eliminar la última versión del blueprint "
                        f"(actual: {latest[0]}). Existen versiones posteriores a {version} "
                        "que podrían depender de ella."
                    ),
                    status_code=409,
                    context={"model_id": model_id, "version": version, "latest": latest[0]},
                )
            session.delete(m)
            session.flush()  # el borrado debe verse antes de recalcular current_version
            self._bump_model_version(session, model_id)
            session.commit()  # borrado + current_version en un único commit
        finally:
            session.close()
        # El detalle lleva las BDs afectadas porque el CASCADE ya borró sus filas de
        # historial: esta entrada es lo único que queda de que la versión falló ahí.
        detail = f"migración {version} eliminada"
        if discarded:
            detail += (
                f" (descartadas {len(discarded)} tentativa(s) fallida(s) en BDs "
                f"{', '.join(str(db_id) for db_id in discarded)})"
            )
        audit.record(
            "migration.delete",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            detail=detail,
        )

    # ------------------------------------------------------------------ #
    # Validación estática del SQL (sin aplicar)                           #
    # ------------------------------------------------------------------ #
    def validate_migration(self, model_id: int, data: dict, *, admin: dict | None = None) -> dict:
        """
        Analiza el SQL de una migración ANTES de aplicarla.

        Existe porque hasta ahora la única forma de saber si un delta era válido era
        aplicarlo: el 422 de traducción solo salta en un apply contra PostgreSQL, y un
        error de sintaxis no salta hasta que el motor lo rechaza, con la BD ya en
        cuarentena.

        Dos niveles. El estático es puro y no toca ningún motor. Con
        ``managed_database_id`` se añade el que de verdad importa: comprobar contra el
        catálogo que las tablas referenciadas existen — un ``ALTER TABLE`` sobre una tabla
        inexistente es sintácticamente impecable y NINGÚN análisis estático lo detecta.
        """
        session = self._session()
        try:
            model = self._model_or_404(session, model_id)
            up_sql = data.get("up_sql")
            version = data.get("version")
            migration = None
            if version:
                migration = self._migration_or_404(session, model_id, version)
            if not up_sql and migration:
                up_sql = migration.up_sql
            if not up_sql:
                raise AppHttpException(
                    message="Indica 'up_sql' (borrador) o 'version' (una ya guardada).",
                    status_code=422,
                    context={"model_id": model_id},
                )

            # `kind` y `has_non_portable` solo salen si se valida una versión ya guardada.
            # Importan: una migración `kind='data'` NUNCA es reanudable, y con los defaults
            # el panel informaba lo contrario.
            kind = migration.kind if migration else "schema"
            has_non_portable = bool(migration.has_non_portable) if migration else False

            target = None
            db_name = None
            # Motor del DESTINO: solo se usa para decidir si la comparación de collation
            # aplica (es semántica MySQL). El dialecto de análisis lo fija migration_facts.
            target_engine = EngineType.mysql
            pending_before: list[str] = []
            db_id = data.get("managed_database_id")
            if db_id is not None:
                md = session.get(ManagedDatabase, db_id)
                # Frontera de autorización, no cosmética: sin esta comprobación se podría
                # sondear el catálogo de CUALQUIER BD del gateway pasando su id a un
                # blueprint que no la contiene.
                if md is None or md.model_id != model_id:
                    raise AppHttpException(
                        message="La BD indicada no pertenece a este blueprint.",
                        status_code=422,
                        context={"model_id": model_id, "managed_database_id": db_id},
                    )
                server = get_server_or_404(session, md.server_id)
                target = build_target(server)
                target_engine = EngineType(engine_value(server))
                db_name = md.name
                # Versiones que esa BD tiene pendientes ANTES de la que se valida. Si hay
                # alguna, sus tablas todavía no existen y no tiene sentido presentarlas como
                # un error: es la pregunta la que está mal planteada, no el SQL.
                if version:
                    pending_before = self._versions_before(
                        session, model_id, md.model_version, version
                    )

            blueprint_collation = model.collation
        finally:
            session.close()

        # El dialecto de análisis lo fija el módulo (MySQL, el de autoría del `up_sql`); el
        # motor del destino solo decide contra qué catálogo se contrasta.
        facts = migration_facts.analyze(up_sql, kind, has_non_portable)

        missing: list[str] = []
        catalog_error: str | None = None
        if target is not None and db_name and facts.requires_existing_tables:
            # Una sola llamada trae el catálogo entero; comparar contra un set en memoria
            # evita una consulta por tabla. La comparación es INSENSIBLE a mayúsculas a
            # propósito: MySQL sobre Linux distingue y PostgreSQL pliega a minúsculas, así
            # que ser estricto produciría avisos falsos constantes — y un validador que
            # grita en falso deja de leerse, que es peor que no tenerlo.
            try:
                existing = {t.lower() for t in get_adapter(target).list_tables(db_name)}
                missing = [
                    t for t in facts.requires_existing_tables if t.lower() not in existing
                ]
            except AppHttpException as exc:
                # Motor inalcanzable o credencial inválida: se reporta y se devuelve igual el
                # análisis estático. Tumbar toda la validación por no poder leer el catálogo
                # sería el peor desenlace — el usuario perdería también lo que SÍ se pudo
                # comprobar sin conexión.
                catalog_error = exc.message
            audit.record(
                "migration.validate",
                admin=admin,
                target_type="managed_database",
                target_id=db_id,
                detail=(
                    f"validación de SQL contra {db_name}: "
                    + (
                        f"catálogo no consultable ({catalog_error})"
                        if catalog_error
                        else f"{len(missing)} tabla(s) referenciada(s) inexistente(s)"
                    )
                ),
            )

        # Solo se comparan collations si el blueprint declaró uno. Y solo tiene sentido en
        # la familia MySQL: PostgreSQL usa encoding + lc_collate, que no son equivalentes.
        conflicts: list[str] = []
        if blueprint_collation and target_engine != EngineType.postgresql:
            conflicts = [
                c for c in facts.forced_collations
                if c.lower() != blueprint_collation.lower()
            ]

        return {
            "statements": [
                {
                    "seq": f.seq,
                    "sql": f.sql,
                    "kind": f.kind,
                    "danger": f.danger,
                    "reasons": [{"code": r.code, "message": r.message} for r in f.reasons],
                    "seeds": f.seeds,
                    "destructive": f.destructive,
                    "collations": list(f.collations),
                    "parse_error": f.parse_error,
                }
                for f in facts.statements
            ],
            "has_seed": facts.has_seed,
            "forced_collations": list(facts.forced_collations),
            "forced_charsets": list(facts.forced_charsets),
            "destructive_statements": list(facts.destructive_statements),
            "parse_errors": [{"seq": seq, "message": msg} for seq, msg in facts.parse_errors],
            "gateway_internal_tables": list(facts.gateway_internal_tables),
            "postgresql_blockers": list(facts.postgresql_blockers),
            "resumable": facts.resumable,
            "referenced_tables": list(facts.requires_existing_tables),
            "pending_before": pending_before,
            "checked_database": db_name,
            "missing_tables": missing,
            "catalog_error": catalog_error,
            "blueprint_collation": blueprint_collation,
            "collation_conflicts": conflicts,
        }

    @staticmethod
    def _versions_before(session, model_id: int, current: str | None, version: str) -> list[str]:
        """
        Versiones del blueprint que esa BD tiene pendientes ANTES de la que se está validando.

        Si no está vacío, las tablas que crean esas versiones todavía no existen en el
        destino y aparecerían como "inexistentes" sin serlo. El aviso deja claro que lo que
        falla es la premisa de la comprobación, no el SQL.
        """
        cur = version_sort_key(current) if current else None
        target = version_sort_key(version)
        rows = (
            session.query(ModelMigration.version)
            .filter(ModelMigration.model_id == model_id)
            .all()
        )
        pending = [
            r[0]
            for r in rows
            if version_sort_key(r[0]) < target and (cur is None or version_sort_key(r[0]) > cur)
        ]
        return sorted(pending, key=version_sort_key)

    # ------------------------------------------------------------------ #
    # Mantenimiento de current_version del blueprint                      #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _bump_model_version(session, model_id: int) -> None:
        """
        Fija current_version del blueprint a la migración más reciente (o 0.0.0).

        NO commitea: el llamador lo hace en la misma transacción que la
        inserción/borrado de la migración (atomicidad).
        """
        latest = (
            session.query(ModelMigration.version)
            .filter(ModelMigration.model_id == model_id)
            .order_by(*_VERSION_ORDER_DESC)
            .first()
        )
        model = session.get(DatabaseModel, model_id)
        if model is not None:
            model.current_version = latest[0] if latest else "0.0.0"
