"""
Controller de comparaciones estructurales entre dos BDs gestionadas (Plan diff).

Responsabilidades (Fases 4-6):
- **Crear** una comparación: snapshotea ambas BDs (motor), corre el diff PURO,
  renderiza el DDL para el motor del TARGET y persiste cabecera + ítems +
  fingerprints. Solo lectura del motor.
- **Leer** el resumen y los ítems paginados (dry-run/preview obligatorio: nunca se
  ejecuta nada sin haber mostrado el DDL exacto primero).
- **Adoptar** (Opción A): ensambla el DDL seleccionado como una NUEVA versión del
  blueprint del target (reusa ``ModelMigrationController.create_migration``) y,
  opcionalmente, la aplica por el camino normal (``ManagedMigrationController.apply``).
- **Ejecutar** (Opción B): corre el DDL seleccionado directamente sobre el target
  (``MigrationRunner.execute_adhoc``). BLOQUEADO si el target tiene blueprint.

Seguridad transversal:
- El servidor es la ÚNICA fuente de verdad del SQL (nunca se ejecuta SQL que
  reenvíe el cliente); el cliente solo confirma con ``confirm_target_name`` +
  ``confirm_token`` (hash del conjunto EXACTO a ejecutar, recomputado server-side).
- Anti-TOCTOU: antes de adoptar/ejecutar se re-snapshotea el target y se recompara
  el fingerprint (409, sin ``force``).
- Auditoría fail-closed (``record_intent``) ANTES de tocar el motor en toda
  ejecución (Opción A inmediata y Opción B).
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.core.database import Database
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    SCHEMA_COMPARISON_MAX_ITEMS,
    SCHEMA_COMPARISON_MAX_SQL_BYTES,
    SCHEMA_COMPARISON_TTL_HOURS,
)
from app.exceptions import AppHttpException
from app.models.enums import EngineType, ProvisionStatus
from app.models.managed_database import ManagedDatabase
from app.models.schema_comparison import SchemaComparison
from app.models.schema_comparison_item import SchemaComparisonItem
from app.services import audit
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.migrations import MigrationRunner
from app.services.db_admin.schema_diff import diff_snapshots

# Motores de la misma familia SQL (comparables entre sí). PostgreSQL solo consigo mismo.
_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})
# Tipos de objeto cuyo cuerpo/DDL NO es traducible cross-engine (atan el blueprint al
# motor de origen vía el guard cross-engine existente).
_NON_PORTABLE_TYPES = frozenset(
    {"routine", "view", "materialized_view", "trigger", "event"}
)


def _utcnow() -> datetime:
    """UTC naive (consistente con el almacenamiento DateTime sin tz en los 3 motores)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _snapshot_fingerprint(snapshot) -> str:
    """
    Hash estable del snapshot NORMALIZADO (anti-TOCTOU). Excluye lo cosmético:
    ``captured_at`` y la versión de extensión (no son estructura). Cualquier cambio
    estructural (una tabla/columna/índice) altera el hash y dispara el 409.
    """
    payload = snapshot.model_dump(mode="json")
    payload.pop("captured_at", None)
    for ext in payload.get("extensions") or []:
        ext.pop("version", None)
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class SchemaComparisonController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Helpers de carga                                                    #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _db_or_404(session, db_id: int) -> ManagedDatabase:
        md = session.get(ManagedDatabase, db_id)
        if md is None:
            raise AppHttpException(
                message="Base de datos gestionada no encontrada.",
                status_code=404,
                context={"managed_database_id": db_id},
            )
        return md

    @staticmethod
    def _comparison_or_404(session, comparison_id: int) -> SchemaComparison:
        comp = session.get(SchemaComparison, comparison_id)
        if comp is None:
            raise AppHttpException(
                message="Comparación de esquema no encontrada.",
                status_code=404,
                context={"comparison_id": comparison_id},
            )
        return comp

    @staticmethod
    def _assert_not_expired(comp: SchemaComparison) -> None:
        if comp.expires_at is not None and comp.expires_at < _utcnow():
            raise AppHttpException(
                message=(
                    "La comparación expiró; describe un estado del motor que ya no es "
                    "vigente. Recalcúlala (POST /schema-comparisons)."
                ),
                status_code=410,
                context={"comparison_id": comp.id, "expires_at": str(comp.expires_at)},
            )

    @staticmethod
    def _engines_compatible(a: str, b: str) -> bool:
        if a == b:
            return True
        return a in _MYSQL_FAMILY and b in _MYSQL_FAMILY

    # ------------------------------------------------------------------ #
    # Serialización                                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _counts(items) -> dict[str, dict[str, int]]:
        """
        Conteo por (object_type -> change_type -> nº de OBJETOS distintos).

        Se cuenta por ``object_name`` distinto (no por sentencia): un objeto que
        rinde N sentencias sigue contando como 1 cambio.
        """
        seen: dict[tuple[str, str], set[str]] = {}
        for it in items:
            seen.setdefault((it.object_type, it.change_type), set()).add(it.object_name)
        out: dict[str, dict[str, int]] = {}
        for (ot, ct), names in seen.items():
            out.setdefault(ot, {})[ct] = len(names)
        return out

    def _serialize_summary(
        self, comp: SchemaComparison, *, items
    ) -> dict:
        has_destructive = any(
            json.loads(it.risk_flags).get("destructive") for it in items
        )
        return {
            "id": comp.id,
            "source_database_id": comp.source_database_id,
            "target_database_id": comp.target_database_id,
            "source_engine": comp.source_engine,
            "target_engine": comp.target_engine,
            "cross_flavor_warning": comp.cross_flavor_warning,
            "scope_note": comp.scope_note,
            "item_count": len(items),
            "counts": self._counts(items),
            "has_destructive": has_destructive,
            "expired": bool(comp.expires_at is not None and comp.expires_at < _utcnow()),
            "created_at": comp.created_at,
            "expires_at": comp.expires_at,
        }

    @staticmethod
    def _serialize_item(it: SchemaComparisonItem) -> dict:
        return {
            "id": it.id,
            "comparison_id": it.comparison_id,
            "seq": it.seq,
            "object_type": it.object_type,
            "object_name": it.object_name,
            "change_type": it.change_type,
            "phase": it.phase,
            "sql": it.sql,
            "risk_flags": json.loads(it.risk_flags),
            "down_sql": it.down_sql,
            "down_confirmed": it.down_confirmed,
            "execution_status": it.execution_status,
            "execution_error": it.execution_error,
            "executed_at": it.executed_at,
        }

    # ------------------------------------------------------------------ #
    # Fase 4 — Crear comparación                                          #
    # ------------------------------------------------------------------ #
    def create_comparison(
        self, source_database_id: int, target_database_id: int, *, admin: dict | None = None
    ) -> dict:
        if source_database_id == target_database_id:
            raise AppHttpException(
                message="source y target no pueden ser la misma base de datos.",
                status_code=422,
                context={"database_id": source_database_id},
            )

        # 1) Resolver ambas BDs + servidores + motores DENTRO de la sesión (la credencial
        #    se descifra mientras la sesión sigue abierta), y validar compatibilidad.
        session = self._session()
        try:
            src_md = self._db_or_404(session, source_database_id)
            tgt_md = self._db_or_404(session, target_database_id)
            src_server = get_server_or_404(session, src_md.server_id)
            tgt_server = get_server_or_404(session, tgt_md.server_id)
            src_engine = engine_value(src_server)
            tgt_engine = engine_value(tgt_server)
            if not self._engines_compatible(src_engine, tgt_engine):
                raise AppHttpException(
                    message=(
                        f"Motores incompatibles: no se puede comparar '{src_engine}' con "
                        f"'{tgt_engine}'. Se permite MySQL↔MariaDB; PostgreSQL solo con PostgreSQL."
                    ),
                    status_code=422,
                    context={"source_engine": src_engine, "target_engine": tgt_engine},
                )
            src_target = build_target(src_server)
            tgt_target = build_target(tgt_server)
            src_name, tgt_name = src_md.name, tgt_md.name
            tgt_server_id = tgt_md.server_id
        finally:
            session.close()

        # 2) Snapshots (motor, solo lectura) + diff PURO + render para el TARGET.
        source_snap = get_adapter(src_target).structural_snapshot(src_name)
        target_snap = get_adapter(tgt_target).structural_snapshot(tgt_name)
        diff = diff_snapshots(source_snap, target_snap)
        rendered = get_adapter(tgt_target).render_diff(diff)

        # 3) Guardrails de tamaño (fail temprano; no materializar payloads enormes).
        if len(rendered) > SCHEMA_COMPARISON_MAX_ITEMS:
            raise AppHttpException(
                message=(
                    f"La comparación produjo {len(rendered)} sentencias (máx. "
                    f"{SCHEMA_COMPARISON_MAX_ITEMS}). ¿Son BDs realmente comparables?"
                ),
                status_code=422,
                context={"items": len(rendered), "max": SCHEMA_COMPARISON_MAX_ITEMS},
            )
        total_bytes = sum(len(r.sql.encode("utf-8")) for r in rendered)
        if total_bytes > SCHEMA_COMPARISON_MAX_SQL_BYTES:
            raise AppHttpException(
                message=(
                    f"El DDL total ({total_bytes} bytes) supera el máximo "
                    f"({SCHEMA_COMPARISON_MAX_SQL_BYTES})."
                ),
                status_code=422,
                context={"bytes": total_bytes, "max": SCHEMA_COMPARISON_MAX_SQL_BYTES},
            )

        # 4) Persistir cabecera + ítems + fingerprints.
        src_fp = _snapshot_fingerprint(source_snap)
        tgt_fp = _snapshot_fingerprint(target_snap)
        expires = _utcnow() + timedelta(hours=SCHEMA_COMPARISON_TTL_HOURS)
        session = self._session()
        try:
            comp = SchemaComparison(
                source_database_id=source_database_id,
                target_database_id=target_database_id,
                source_engine=src_engine,
                target_engine=tgt_engine,
                source_fingerprint=src_fp,
                target_fingerprint=tgt_fp,
                cross_flavor_warning=diff.cross_flavor_warning,
                scope_note=diff.scope_note,
                expires_at=expires,
            )
            session.add(comp)
            session.flush()  # asigna comp.id
            for i, r in enumerate(rendered):
                session.add(
                    SchemaComparisonItem(
                        comparison_id=comp.id,
                        seq=i,
                        object_type=r.object_type,
                        object_name=r.object_name,
                        change_type=r.change_type,
                        phase=r.phase,
                        sql=r.sql,
                        risk_flags=json.dumps(r.risk.model_dump(), sort_keys=True),
                        down_sql=r.down_sql,
                        down_confirmed=r.down_confirmed,
                    )
                )
            session.commit()
            session.refresh(comp)
            items = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comp.id)
                .all()
            )
            comp_id = comp.id
            result = self._serialize_summary(comp, items=items)
        finally:
            session.close()

        audit.record(
            "schema_comparison.create",
            admin=admin,
            target_type="managed_database",
            target_id=target_database_id,
            server_id=tgt_server_id,
            touched_engine=True,  # se snapshotearon ambos motores (solo lectura)
            detail=(
                f"comparación {comp_id}: source={source_database_id} vs "
                f"target={target_database_id}, {len(rendered)} sentencia(s)"
            ),
        )
        return result

    # ------------------------------------------------------------------ #
    # Fase 4 — Lectura                                                    #
    # ------------------------------------------------------------------ #
    def get_comparison(self, comparison_id: int) -> dict:
        session = self._session()
        try:
            comp = self._comparison_or_404(session, comparison_id)
            items = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comparison_id)
                .all()
            )
            return self._serialize_summary(comp, items=items)
        finally:
            session.close()

    def list_items(
        self,
        comparison_id: int,
        *,
        object_type: str | None = None,
        change_type: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        session = self._session()
        try:
            self._comparison_or_404(session, comparison_id)
            q = session.query(SchemaComparisonItem).filter(
                SchemaComparisonItem.comparison_id == comparison_id
            )
            if object_type is not None:
                q = q.filter(SchemaComparisonItem.object_type == object_type)
            if change_type is not None:
                q = q.filter(SchemaComparisonItem.change_type == change_type)
            total = q.count()
            rows = (
                q.order_by(SchemaComparisonItem.seq.asc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [self._serialize_item(r) for r in rows], total
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Anti-TOCTOU                                                         #
    # ------------------------------------------------------------------ #
    def _assert_fingerprint(self, managed_db_id: int, stored_fp: str, *, label: str) -> None:
        """Re-snapshotea la BD y compara el fingerprint contra el guardado (409 si difiere)."""
        session = self._session()
        try:
            md = self._db_or_404(session, managed_db_id)
            server = get_server_or_404(session, md.server_id)
            target = build_target(server)
            db_name = md.name
        finally:
            session.close()

        current_fp = _snapshot_fingerprint(get_adapter(target).structural_snapshot(db_name))
        if current_fp != stored_fp:
            raise AppHttpException(
                message=(
                    f"El esquema de {label} cambió desde que se calculó esta comparación; "
                    "recalcúlala (POST /schema-comparisons) antes de continuar."
                ),
                status_code=409,
                context={"managed_database_id": managed_db_id, "side": label},
            )

    def _verify_no_drift(self, comp: SchemaComparison, *, side: str = "target") -> None:
        """Recompara fingerprints del target (y opcionalmente el source) — anti-TOCTOU."""
        self._assert_fingerprint(
            comp.target_database_id, comp.target_fingerprint, label="target"
        )
        if side == "both":
            self._assert_fingerprint(
                comp.source_database_id, comp.source_fingerprint, label="source"
            )

    # ------------------------------------------------------------------ #
    # Fase 5 — Opción A: adoptar como versión de blueprint                #
    # ------------------------------------------------------------------ #
    def adopt_comparison(
        self,
        comparison_id: int,
        *,
        selected_item_ids: list[int],
        name: str,
        description: str | None = None,
        execute_immediately: bool = False,
        admin: dict | None = None,
    ) -> dict:
        session = self._session()
        try:
            comp = self._comparison_or_404(session, comparison_id)
            self._assert_not_expired(comp)
            tgt_md = self._db_or_404(session, comp.target_database_id)
            if tgt_md.model_id is None:
                raise AppHttpException(
                    message=(
                        "El target no tiene blueprint asignado (model_id): la adopción como "
                        "versión de blueprint (Opción A) no está disponible. Usa /execute."
                    ),
                    status_code=422,
                    context={"target_database_id": tgt_md.id},
                )
            model_id = tgt_md.model_id
            target_db_id = tgt_md.id
            target_server_id = tgt_md.server_id
            target_engine = comp.target_engine
            target_fp = comp.target_fingerprint
            # Extraer los ítems seleccionados YA como datos planos (la sesión se cierra).
            selected = self._load_selected(session, comparison_id, selected_item_ids)
        finally:
            session.close()

        # Anti-TOCTOU antes de derivar/aplicar nada.
        self._assert_fingerprint(target_db_id, target_fp, label="target")

        # Ensamblar up_sql (orden de fase) y down_sql (orden inverso).
        selected.sort(key=lambda d: d["seq"])
        up_sql = ";\n".join(d["sql"] for d in selected)
        down_parts = [d["down_sql"] for d in reversed(selected) if d["down_sql"]]
        down_suggested = ";\n".join(down_parts) if down_parts else None
        # Auto-confirmar el rollback SOLO si TODO el conjunto es claramente reversible
        # (cada ítem con down_sql confirmado). Si no, queda como sugerencia (fail-closed).
        all_confirmed = bool(selected) and all(
            d["down_confirmed"] and d["down_sql"] for d in selected
        )
        down_confirmed = down_suggested if all_confirmed else None
        has_non_portable = any(d["object_type"] in _NON_PORTABLE_TYPES for d in selected)

        # Pin del SQL al motor del target (evita que el traductor cross-engine lo mangle:
        # es DDL específico de dialecto). Igual que el baseline de snapshot (Plan 09).
        up_mysql = up_sql if target_engine in _MYSQL_FAMILY else None
        up_pg = up_sql if target_engine == "postgresql" else None

        # Auditoría fail-closed ANTES de tocar el motor SOLO si se aplica de inmediato
        # (crear la versión no toca ningún motor; aplicarla sí).
        if execute_immediately:
            audit.record_intent(
                "schema_comparison.adopt",
                admin=admin,
                target_type="managed_database",
                target_id=target_db_id,
                server_id=target_server_id,
                detail=(
                    f"adopt+apply de {len(selected)} sentencia(s) desde comparación "
                    f"{comparison_id} al blueprint {model_id}"
                ),
            )

        # REUSO: create_migration resuelve checksum, autoasignación de versión con
        # reintento por concurrencia y bump de current_version en una transacción.
        # is_baseline=True + reviewed según execute_immediately: DDL derivado del motor
        # es "capturado" (como un baseline de snapshot). Si se difiere la aplicación,
        # nace reviewed=False y el gate R1 lo protege hasta que un admin lo apruebe;
        # si se aplica de inmediato, la selección explícita del admin ES la revisión.
        from app.controllers.model_migration_controller import ModelMigrationController

        migration = ModelMigrationController().create_migration(
            model_id,
            {
                "name": name,
                "up_sql": up_sql,
                "up_sql_mysql": up_mysql,
                "up_sql_postgresql": up_pg,
                "down_sql": down_confirmed,
                "down_sql_suggested": down_suggested,
                "source_engine": target_engine,
                "has_non_portable": has_non_portable,
                "kind": "schema",
                "is_baseline": True,
                "reviewed": execute_immediately,
            },
            admin=admin,
        )
        version = migration["version"]

        apply_result = None
        if execute_immediately:
            from app.controllers.managed_migration_controller import (
                ManagedMigrationController,
            )

            # Camino normal, con TODOS sus guards (integridad, cross-engine, cuarentena,
            # R1 — que aquí pasa porque reviewed=True). up_to_version acota a la versión
            # recién creada (no re-aplica versiones anteriores del blueprint más allá de
            # lo pendiente hasta ella).
            apply_result = ManagedMigrationController().apply(
                target_db_id, up_to_version=version, admin=admin
            )

        audit.record(
            "schema_comparison.adopt",
            status="success",
            admin=admin,
            target_type="managed_database",
            target_id=target_db_id,
            server_id=target_server_id,
            touched_engine=execute_immediately,
            detail=(
                f"versión {version} creada en blueprint {model_id} desde comparación "
                f"{comparison_id} ({len(selected)} sentencia(s))"
                + (" y aplicada" if execute_immediately else "")
            ),
        )
        return {
            "comparison_id": comparison_id,
            "model_id": model_id,
            "version": version,
            "statements": len(selected),
            "executed": execute_immediately,
            "migration": migration,
            "apply_result": apply_result,
        }

    @staticmethod
    def _load_selected(
        session, comparison_id: int, selected_item_ids: list[int]
    ) -> list[dict]:
        """Carga los ítems seleccionados validando pertenencia; devuelve datos planos."""
        ids = list(dict.fromkeys(selected_item_ids))  # dedup preservando orden
        rows = (
            session.query(SchemaComparisonItem)
            .filter(
                SchemaComparisonItem.comparison_id == comparison_id,
                SchemaComparisonItem.id.in_(ids),
            )
            .all()
        )
        found = {r.id for r in rows}
        missing = [i for i in ids if i not in found]
        if missing:
            raise AppHttpException(
                message="Algunos ítems seleccionados no pertenecen a esta comparación.",
                status_code=422,
                context={"comparison_id": comparison_id, "missing_item_ids": missing},
            )
        return [
            {
                "id": r.id,
                "seq": r.seq,
                "sql": r.sql,
                "down_sql": r.down_sql,
                "down_confirmed": r.down_confirmed,
                "object_type": r.object_type,
                "object_name": r.object_name,
                "risk": json.loads(r.risk_flags),
            }
            for r in rows
        ]

    # ------------------------------------------------------------------ #
    # Fase 6 — Opción B: ejecución directa ad-hoc                          #
    # ------------------------------------------------------------------ #
    @staticmethod
    def execution_token(target_database_id: int, target_engine: str, resolved: list[dict]) -> str:
        """
        Token de confirmación verificable: SHA256 del conjunto EXACTO a ejecutar
        (``target_id`` + ``engine`` + lista ORDENADA de ``(sql, risk_flags)``).

        Es un checksum de integridad, no un secreto: liga la confirmación del cliente
        al SQL que realmente vio. Si la selección cambia (o alguien reordena), el token
        no coincide. Se recomputa server-side y solo se usa para COMPARAR; nunca se
        confía en el valor del cliente para otra cosa. El frontend reproduce este mismo
        algoritmo sobre el DDL que muestra.
        """
        parts: list[str] = [str(target_database_id), str(target_engine)]
        for d in resolved:
            parts.append(d["sql"])
            parts.append(json.dumps(d["risk"], sort_keys=True))
        blob = "\x1f".join(parts)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    @staticmethod
    def _resolve_mode(all_items: list, mode: str, selected_item_ids: list[int] | None) -> list[dict]:
        """
        Resuelve el conjunto de sentencias a ejecutar según el modo (ya ordenadas por seq).

        - ``all``: todo lo aditivo/seguro y destructivo, EXCEPTO objetos procedurales
          con cuerpo no revisable (``requires_individual_review``: solo vía custom).
        - ``all_except_destructive``: excluye además cualquier ítem ``destructive``.
        - ``custom``: exactamente los ``selected_item_ids`` (el admin los eligió a mano;
          sin exclusiones automáticas).
        """
        def to_dict(it) -> dict:
            return {
                "id": it.id,
                "seq": it.seq,
                "sql": it.sql,
                "object_type": it.object_type,
                "object_name": it.object_name,
                "risk": json.loads(it.risk_flags),
            }

        dicts = [to_dict(it) for it in all_items]  # ya vienen ordenados por seq
        if mode == "all":
            return [d for d in dicts if not d["risk"].get("requires_individual_review")]
        if mode == "all_except_destructive":
            return [
                d
                for d in dicts
                if not d["risk"].get("destructive")
                and not d["risk"].get("requires_individual_review")
            ]
        if mode == "custom":
            if not selected_item_ids:
                raise AppHttpException(
                    message="mode=custom requiere 'selected_item_ids'.",
                    status_code=422,
                    context={"mode": mode},
                )
            idset = set(selected_item_ids)
            chosen = [d for d in dicts if d["id"] in idset]
            missing = idset - {d["id"] for d in chosen}
            if missing:
                raise AppHttpException(
                    message="Algunos 'selected_item_ids' no pertenecen a esta comparación.",
                    status_code=422,
                    context={"missing_item_ids": sorted(missing)},
                )
            return chosen
        raise AppHttpException(
            message="Modo de ejecución inválido.",
            status_code=422,
            context={"mode": mode},
        )

    def preview_execution(
        self,
        comparison_id: int,
        *,
        mode: str,
        selected_item_ids: list[int] | None,
    ) -> dict:
        """
        Resuelve el conjunto de sentencias de un modo/selección de Opción B SIN
        ejecutar nada, y devuelve el ``confirm_token`` correspondiente.

        Existe porque el frontend NO puede reproducir de forma confiable ``_resolve_mode``
        (requeriría paginar TODOS los ítems y replicar el filtro por ``risk_flags``) ni el
        formato EXACTO de serialización de ``execution_token`` (orden de claves JSON,
        separadores) — ambos son detalles de implementación del servidor. El servidor sigue
        siendo la única fuente de verdad del token: esta es la única vía soportada para
        obtenerlo antes de llamar a ``/execute``.
        """
        session = self._session()
        try:
            comp = self._comparison_or_404(session, comparison_id)
            self._assert_not_expired(comp)
            target_db_id = comp.target_database_id
            target_engine = comp.target_engine
            all_items = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comparison_id)
                .order_by(SchemaComparisonItem.seq.asc())
                .all()
            )
            resolved = self._resolve_mode(all_items, mode, selected_item_ids)
        finally:
            session.close()

        if not resolved:
            raise AppHttpException(
                message="No hay sentencias que ejecutar para el modo/selección indicados.",
                status_code=422,
                context={"comparison_id": comparison_id, "mode": mode},
            )

        token = self.execution_token(target_db_id, target_engine, resolved)
        return {
            "comparison_id": comparison_id,
            "target_database_id": target_db_id,
            "mode": mode,
            "statements": [
                {
                    "item_id": d["id"],
                    "object_type": d["object_type"],
                    "object_name": d["object_name"],
                    "sql": d["sql"],
                    "risk_flags": d["risk"],
                }
                for d in resolved
            ],
            "confirm_token": token,
        }

    def execute_comparison(
        self,
        comparison_id: int,
        *,
        mode: str,
        selected_item_ids: list[int] | None,
        confirm_target_name: str,
        confirm_token: str,
        force: bool = False,
        admin: dict | None = None,
    ) -> dict:
        session = self._session()
        try:
            comp = self._comparison_or_404(session, comparison_id)
            self._assert_not_expired(comp)
            tgt_md = self._db_or_404(session, comp.target_database_id)
            # Decisión #3: la ejecución directa está BLOQUEADA si el target tiene blueprint
            # (dejaría la BD desincronizada de su propio blueprint sin que el sistema se entere).
            if tgt_md.model_id is not None:
                raise AppHttpException(
                    message=(
                        "El target tiene un blueprint asignado: la ejecución directa (Opción B) "
                        "está bloqueada. Usa /adopt (Opción A) para agregar una versión al blueprint."
                    ),
                    status_code=409,
                    context={"target_database_id": tgt_md.id, "model_id": tgt_md.model_id},
                )
            # Confirmación por nombre (doble intención, human-meaningful).
            if confirm_target_name != tgt_md.name:
                raise AppHttpException(
                    message=(
                        "Confirmación requerida: 'confirm_target_name' debe coincidir "
                        "exactamente con el nombre de la BD target."
                    ),
                    status_code=422,
                    context={"target_database_id": tgt_md.id, "required": "confirm_target_name == name"},
                )
            target_db_id = tgt_md.id
            target_server_id = tgt_md.server_id
            target_engine = comp.target_engine
            target_fp = comp.target_fingerprint
            db_name = tgt_md.name
            quarantined = tgt_md.status == ProvisionStatus.error
            server = get_server_or_404(session, tgt_md.server_id)
            engine = EngineType(engine_value(server))
            target = build_target(server)
            all_items = (
                session.query(SchemaComparisonItem)
                .filter(SchemaComparisonItem.comparison_id == comparison_id)
                .order_by(SchemaComparisonItem.seq.asc())
                .all()
            )
            resolved = self._resolve_mode(all_items, mode, selected_item_ids)
        finally:
            session.close()

        if not resolved:
            raise AppHttpException(
                message="No hay sentencias que ejecutar para el modo/selección indicados.",
                status_code=422,
                context={"comparison_id": comparison_id, "mode": mode},
            )

        # Confirmación verificable (hash), recomputada server-side sobre el conjunto EXACTO.
        expected = self.execution_token(target_db_id, target_engine, resolved)
        if confirm_token != expected:
            raise AppHttpException(
                message=(
                    "'confirm_token' no coincide con el conjunto de sentencias a ejecutar "
                    "(¿cambió la selección desde que se calculó?). Reobtén el token."
                ),
                status_code=422,
                context={"comparison_id": comparison_id, "mode": mode},
            )

        # Cuarentena: un fallo previo pudo dejar la BD en estado parcial (DDL no
        # transaccional). Se exige inspección + force=true (igual que apply).
        if quarantined and not force:
            raise AppHttpException(
                message=(
                    "La BD está en cuarentena por un fallo previo. Inspecciónala y "
                    "reintenta con force=true."
                ),
                status_code=409,
                context={"target_database_id": target_db_id, "required": "force=true"},
            )

        # Anti-TOCTOU JUSTO antes de ejecutar.
        self._assert_fingerprint(target_db_id, target_fp, label="target")

        # Auditoría fail-closed ANTES de tocar el motor (persiste la intención completa).
        audit.record_intent(
            "schema_comparison.execute",
            admin=admin,
            target_type="managed_database",
            target_id=target_db_id,
            server_id=target_server_id,
            detail=(
                f"execute mode={mode}: {len(resolved)} sentencia(s) de la comparación "
                f"{comparison_id} sobre '{db_name}' (target sin blueprint)"
            ),
        )

        statements = [d["sql"] for d in resolved]
        results = MigrationRunner().execute_adhoc(
            target,
            db_name=db_name,
            engine=engine,
            managed_db_id=target_db_id,
            statements=statements,
        )
        self._record_item_results(resolved, results)

        failed = any(r.status == "failed" for r in results)
        applied_count = sum(1 for r in results if r.status == "applied")
        audit.record(
            "schema_comparison.execute",
            status="error" if failed else "success",
            admin=admin,
            target_type="managed_database",
            target_id=target_db_id,
            server_id=target_server_id,
            touched_engine=True,
            detail=(
                f"{applied_count}/{len(resolved)} sentencia(s) aplicada(s)"
                + (" (con fallo)" if failed else "")
            ),
        )
        return {
            "comparison_id": comparison_id,
            "target_database_id": target_db_id,
            "mode": mode,
            "total": len(resolved),
            "applied_count": applied_count,
            "failed": failed,
            "statements": self._result_items(resolved, results),
        }

    @staticmethod
    def _result_items(resolved: list[dict], results: list) -> list[dict]:
        out: list[dict] = []
        for k, d in enumerate(resolved):
            if k < len(results):
                r = results[k]
                out.append(
                    {
                        "item_id": d["id"],
                        "object_type": d["object_type"],
                        "object_name": d["object_name"],
                        "status": r.status,
                        "error": r.error,
                        "execution_ms": r.execution_ms,
                    }
                )
            else:
                # Sentencia no ejecutada (se cortó en un fallo anterior).
                out.append(
                    {
                        "item_id": d["id"],
                        "object_type": d["object_type"],
                        "object_name": d["object_name"],
                        "status": "skipped",
                        "error": None,
                        "execution_ms": None,
                    }
                )
        return out

    def _record_item_results(self, resolved: list[dict], results: list) -> None:
        """Persiste el resultado por sentencia en ``schema_comparison_items``."""
        session = self._session()
        try:
            for k, d in enumerate(resolved):
                item = session.get(SchemaComparisonItem, d["id"])
                if item is None:
                    continue
                if k < len(results):
                    r = results[k]
                    item.execution_status = r.status
                    item.execution_error = r.error
                    item.executed_at = r.executed_at
                else:
                    item.execution_status = "skipped"
                    item.execution_error = None
                    item.executed_at = None
            session.commit()
        finally:
            session.close()
