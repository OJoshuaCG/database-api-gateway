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
from app.exceptions import AppHttpException
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.database_model import DatabaseModel
from app.models.enums import EngineType, MigrationStatus
from app.models.model_migration import ModelMigration
from app.services import audit
from app.services.db_admin.migration_integrity import compute_checksum, version_sort_key
from app.services.db_admin.sql_dialect import RollbackGenerator, SqlTranslator

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

    def _serialize(self, m: ModelMigration) -> dict:
        return {
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
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }

    @staticmethod
    def _serialize_summary(m: ModelMigration) -> dict:
        return {
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
            "created_at": m.created_at,
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

    @staticmethod
    def _has_history(session, migration_id: int) -> bool:
        return (
            session.query(DatabaseMigrationHistory)
            .filter(DatabaseMigrationHistory.model_migration_id == migration_id)
            .first()
            is not None
        )

    @staticmethod
    def _has_successful_application(session, migration_id: int) -> bool:
        """
        True si la migración se aplicó EXITOSAMENTE en al menos una BD (status=applied).

        Distinto de ``_has_history``: un intento que solo FALLÓ deja historial pero no
        cambió ninguna BD, así que su SQL todavía puede corregirse. El SQL solo se
        congela cuando existe una aplicación exitosa (alguna BD ya depende de él).
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
            return [self._serialize_summary(r) for r in rows], total
        finally:
            session.close()

    def get_migration(self, model_id: int, version: str) -> dict:
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            return self._serialize(self._migration_or_404(session, model_id, version))
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
            # Sugerir rollback solo si el admin no proporcionó uno explícito.
            suggested = self._rollback.generate(up_sql)

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
                )
                session.add(migration)
                try:
                    # flush hace visible la migración a _bump_model_version y detecta el
                    # conflicto de versión (UNIQUE) en la MISMA transacción.
                    session.flush()
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
            result = self._serialize(migration)
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
                selected, seed_by_table, data.get("manual_layout") or []
            )
            if violations:
                raise AppHttpException(
                    message=(
                        "El layout manual no es aplicable (dependencias/orden). "
                        "Corrige la asignación de objetos a versiones."
                    ),
                    status_code=422,
                    context={"violations": violations},
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
        session = self._session()
        try:
            self._model_or_404(session, model_id)
            m = self._migration_or_404(session, model_id, version)
            applied_successfully = self._has_successful_application(session, m.id)

            # El SQL efectivo (base u overrides) NO puede cambiar si ya se aplicó
            # EXITOSAMENTE en alguna BD: editarlo aquí no re-ejecuta nada en el motor, así
            # que la metadata divergiría de lo que realmente corrió. Un intento que solo
            # falló no congela el SQL (ninguna BD depende de él). Fix-forward si ya se aplicó.
            sql_fields_changing = any(
                f in data and data[f] is not None
                for f in ("up_sql", "up_sql_mysql", "up_sql_postgresql")
            )
            if applied_successfully and sql_fields_changing:
                raise AppHttpException(
                    message=(
                        "La migración ya fue aplicada exitosamente en alguna BD: no se "
                        "puede modificar su SQL. Cree una nueva migración para corregir "
                        "(fix-forward)."
                    ),
                    status_code=409,
                    context={"model_id": model_id, "version": version},
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

            # Recalcular checksum si cambió alguna variante de SQL o el rollback.
            m.checksum = compute_checksum(
                m.up_sql, m.up_sql_mysql, m.up_sql_postgresql, m.down_sql, m.version
            )
            session.commit()
            session.refresh(m)
            result = self._serialize(m)
        finally:
            session.close()
        audit.record(
            "migration.update",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            detail=f"migración {version} actualizada",
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
            if self._has_history(session, m.id):
                raise AppHttpException(
                    message=(
                        "No se puede eliminar una migración con historial de aplicación. "
                        "Revierta y/o cree una migración compensatoria."
                    ),
                    status_code=409,
                    context={"model_id": model_id, "version": version},
                )
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
        audit.record(
            "migration.delete",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            detail=f"migración {version} eliminada",
        )

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
