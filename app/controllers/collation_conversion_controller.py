"""
Controller de conversión de charset/collation de una base de datos COMPLETA (MySQL/MariaDB).

EL PROBLEMA (y por qué existe este módulo): cambiar el charset/collation de una BD y de sus
tablas NO alcanza. La documentación oficial de MySQL lo dice en la página de ``ALTER
DATABASE``:

    "If you change the default character set or collation for a database, any stored
     routines that are to use the new defaults must be dropped and recreated."

El motor CONGELA en cada PROCEDURE/FUNCTION/TRIGGER/EVENT/VIEW la ``collation_connection``
de la sesión que lo creó — es la que heredaron los parámetros ``VARCHAR``/``CHAR`` de una
rutina, las variables ``DECLARE`` de un trigger/evento y los literales de texto del cuerpo
de una vista. Si se cambia la collation de la BD/tablas sin recrear esos objetos, en
producción aparecen ``Illegal mix of collations``. Y no hay ``ALTER PROCEDURE``/``ALTER
TRIGGER`` que cambie el cuerpo ni la collation de sus parámetros: la ÚNICA vía es ``DROP`` +
``CREATE`` con el MISMO cuerpo, ejecutado en una sesión que ya tenga la collation objetivo.
Herramientas como HeidiSQL ofrecen "cambiar la collation de toda la BD" pero NO recrean esos
objetos; ese es exactamente el bug que este feature corrige.

FLUJO (mismo patrón seguro que el clon): crear PLAN (snapshotea el inventario, persiste
cabecera + fingerprint) → inspeccionar el inventario (tablas por collation + los 5 tipos de
objeto) → PREVIEW (resuelve el plan final + ``confirm_token``, sin ejecutar) → EXECUTE
(valida token/nombre/fingerprint, audita fail-closed, encola el job asíncrono) → polling.

POR QUÉ NO HAY ORDEN DE DEPENDENCIAS (simplificación deliberada respecto del clon y de
schema-comparisons, que sí necesitan un orden topológico y un endpoint
``resolve-selection``): acá los 5 tipos de objeto YA EXISTEN y cada uno se procesa de forma
INDEPENDIENTE y COMPLETA (capturar DDL → drop → recrear → reaplicar grants) antes de pasar
al siguiente. Por eso el orden es irrelevante:

- MySQL/MariaDB NO validan el cuerpo de una PROCEDURE/FUNCTION/TRIGGER/EVENT contra otros
  objetos al crearlos (el cuerpo es opaco hasta que se ejecuta), así que da lo mismo si otra
  rutina que citan está en medio de su propio drop+create.
- ``CREATE VIEW`` SÍ valida que existan las tablas/vistas referenciadas, pero como el
  drop+create de CADA objeto es inmediato (no se dropea todo el lote primero), en todo
  momento el resto de los objetos existe en su forma vieja o nueva — nunca ausente por más
  de un instante, y nunca mientras se crea OTRO objeto.

Toda la lógica de motor vive en el adapter; este controller solo orquesta.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.controllers.schema_comparison_controller import _synthetic_lock_key
from app.core.database import Database
from app.core.environments import (
    COLLATION_CONVERSION_TTL_HOURS,
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
)
from app.core.logger import get_logger
from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.models.collation_conversion_job import (
    COLLATION_ITEM_ERROR,
    COLLATION_ITEM_OK,
    COLLATION_ITEM_SKIPPED,
    COLLATION_MODE_COLUMNS,
    COLLATION_MODE_UNIVERSAL,
    COLLATION_OBJ_DATABASE,
    COLLATION_OBJ_FUNCTION,
    COLLATION_OBJ_PROCEDURE,
    COLLATION_OBJ_TABLE,
    COLLATION_PHASE_DATABASE,
    COLLATION_PHASE_DONE,
    COLLATION_PHASE_OBJECTS,
    COLLATION_PHASE_TABLES,
    COLLATION_STATUS_CANCELED,
    COLLATION_STATUS_FAILED,
    COLLATION_STATUS_INTERRUPTED,
    COLLATION_STATUS_PENDING,
    COLLATION_STATUS_RUNNING,
    COLLATION_STATUS_SUCCEEDED,
    CollationConversionJob,
    CollationConversionJobItem,
)
from app.models.enums import EngineType, ProvisionStatus
from app.models.managed_database import ManagedDatabase
from app.services import audit, charset_catalog
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.identifiers import (
    ensure_not_reserved_database,
    quote_identifier,
    validate_identifier,
)
from app.services.db_admin.migrations import MigrationRunner

logger = get_logger(__name__)

_MYSQL_FAMILY = frozenset({"mysql", "mariadb"})

# Los dos tipos de objeto que tienen privilegios PROPIOS a nivel de objeto y que el motor
# BORRA al dropearlos (``mysql.procs_priv``). Verificado en la doc de MySQL ("if you drop a
# routine, any routine-level privileges granted for that routine are revoked") y de MariaDB.
# TRIGGER y EVENT NO están: no tienen grants propios — el permiso de un trigger viaja en el
# privilegio ``TRIGGER`` de su TABLA y el de un evento en el privilegio ``EVENT`` de la BD,
# así que recrearlos no pierde nada. Las TABLAS tampoco (y además no se dropean acá).
_GRANTED_OBJECT_TYPES = frozenset({COLLATION_OBJ_PROCEDURE, COLLATION_OBJ_FUNCTION})

# object_type → palabra clave del DROP. Los 5 aceptan ``IF EXISTS`` en MySQL y MariaDB.
_DROP_KEYWORDS: dict[str, str] = {
    "procedure": "PROCEDURE",
    "function": "FUNCTION",
    "trigger": "TRIGGER",
    "event": "EVENT",
    "view": "VIEW",
}

# Acciones que SÍ convierten una tabla, por modo: ``convert_table`` es el
# ``CONVERT TO CHARACTER SET`` de MySQL/MariaDB y ``convert_columns`` el ``ALTER TABLE`` con
# una acción ``ALTER COLUMN ... COLLATE`` por columna de PostgreSQL. Ambas se cuentan y se
# ejecutan en la MISMA fase (``tables``) para que el polling del frontend no cambie.
_CONVERT_ACTIONS = ("convert_table", "convert_columns")

# Intervalo mínimo entre persistencias del progreso a la BD del gateway (throttle).
_PROGRESS_PERSIST_SECONDS = 3.0


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True)
class _Step:
    """Un paso del plan resuelto. ``action='skip'`` se registra pero no toca el motor."""

    object_type: str
    object_name: str
    # universal: alter_database | convert_table | recreate | skip
    # columns:   convert_columns | skip
    action: str
    sql: str | None = None
    reason: str | None = None
    previous_charset: str | None = None
    previous_collation: str | None = None
    # Modo ``columns``: qué columnas de la tabla altera este paso (todas en UNA sentencia).
    columns: tuple[str, ...] = ()


@dataclass
class _Plan:
    steps: list[_Step] = field(default_factory=list)
    missing: list[dict] = field(default_factory=list)
    missing_tables: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    include_database_default: bool = True


class CollationConversionController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Carga / helpers                                                     #
    # ------------------------------------------------------------------ #
    def _job_or_404(self, session, job_id: int) -> CollationConversionJob:
        job = session.get(CollationConversionJob, job_id)
        if job is None:
            raise AppHttpException(
                message="Job de conversión de collation no encontrado.",
                status_code=404,
                context={"collation_conversion_job_id": job_id},
            )
        return job

    def _load_context(self, server_id: int, database: str):
        """``(dialect, target, managed_id)`` — cierra la sesión antes de tocar el motor."""
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            dialect = engine_value(server)
            target = build_target(server)
            managed = (
                session.query(ManagedDatabase)
                .filter(
                    ManagedDatabase.server_id == server_id,
                    ManagedDatabase.name == database,
                )
                .first()
            )
            return dialect, target, (managed.id if managed else None)
        finally:
            session.close()

    @staticmethod
    def _mode_for_dialect(dialect: str) -> str:
        """
        Modo de conversión según el MOTOR. No lo elige el usuario: es una consecuencia de
        cómo cada motor trata la collation, y los dos modos son operaciones DISTINTAS.

        - MySQL/MariaDB → ``universal``: ``ALTER DATABASE`` + ``CONVERT TO CHARACTER SET``
          por tabla + DROP/CREATE de los 5 tipos de objeto que CONGELAN la collation de la
          sesión que los creó.
        - PostgreSQL → ``columns``: SOLO ``ALTER TABLE ... ALTER COLUMN ... TYPE ...
          COLLATE ...``. No hay objetos que recrear (resuelve la collation dinámicamente en
          cada llamada) ni ``ALTER DATABASE`` posible (el ``ENCODING``/``LC_COLLATE`` es
          inmutable tras el ``CREATE DATABASE``; cambiarlo exige volcar y recargar, que es
          el módulo de clonado).

        Un dialecto desconocido es un bug del inventario, no entrada del usuario: 422.
        """
        if dialect in _MYSQL_FAMILY:
            return COLLATION_MODE_UNIVERSAL
        if dialect == EngineType.postgresql.value:
            return COLLATION_MODE_COLUMNS
        raise AppHttpException(
            message="La conversión de charset/collation no aplica a este motor.",
            status_code=422,
            context={"dialect": dialect},
        )

    @staticmethod
    def _resolve_pg_collation(
        adapter, database: str, target_charset: str | None, target_collation: str
    ) -> str:
        """
        Valida el objetivo del modo ``columns`` contra ``pg_collation`` LEÍDO EN VIVO y
        devuelve el nombre EXACTO del catálogo (lo que viaja al DDL sale de ahí, nunca del
        texto crudo del request — mismo criterio que la forma canónica del catálogo global).

        NO se usa ``charset_catalog`` a propósito: describe otra cosa. Ahí viven los
        ``ENCODING``/``LC_COLLATE`` con los que se CREA una base (locales del SO, p. ej.
        ``en_US.UTF-8``); un ``COLLATE`` de COLUMNA nombra un OBJETO de ``pg_collation``
        (``en_US``, ``C``, ``es-ES-x-icu``). Son dos espacios de valores distintos, y el de
        PostgreSQL depende de los locales instalados en el SO de CADA servidor (y de si el
        binario trae ICU): una lista global sería directamente falsa. Los nombres son
        CASE-SENSITIVE, así que la comparación es exacta.
        """
        if target_charset:
            raise AppHttpException(
                message=(
                    "PostgreSQL no tiene charset por columna ni por tabla, y el ENCODING de "
                    "la base es inmutable tras el CREATE DATABASE: no envíes target_charset, "
                    "solo target_collation."
                ),
                status_code=422,
                context={"charset": target_charset},
            )
        available = {c.name: c for c in adapter.list_collations(database)}
        hit = available.get(target_collation)
        if hit is None:
            # No se listan las 800+ collations típicas de un servidor con todos los locales:
            # el inventario del job ya las expone para armar el selector.
            raise AppHttpException(
                message=(
                    "La collation pedida no existe en este servidor PostgreSQL (o no es "
                    "usable con el encoding de esta base). El catálogo de collations depende "
                    "de los locales instalados en el SO de cada servidor: consultá las "
                    "disponibles en el inventario del plan (available_collations)."
                ),
                status_code=422,
                context={"collation": target_collation, "available": len(available)},
                public_context={"available_count": len(available)},
            )
        return hit.name

    @staticmethod
    def _inventory_fingerprint(inv) -> str:
        """
        Hash estable del ESTADO DE LA BD (anti-TOCTOU). Incluye solo lo que el motor
        reporta, NUNCA lo que depende del objetivo elegido (``needs_conversion``,
        ``is_outdated``, ``mismatched_columns``): si no, cambiar de collation objetivo
        invalidaría el fingerprint sin que la BD haya cambiado.
        """
        payload = {
            "db": [inv.db_charset, inv.db_collation],
            "tables": sorted(
                # La 4ª posición SOLO existe en el modo ``columns`` (PostgreSQL), donde la
                # collation vive en la COLUMNA: sin ella, agregar/cambiar una columna de
                # texto entre el preview y la ejecución no invalidaría el plan. Se agrega
                # CONDICIONALMENTE para que la huella del modo ``universal`` (donde
                # ``columns`` es None) siga siendo byte a byte la misma de antes y los
                # planes ya persistidos no se invaliden al desplegar.
                [t.name, t.charset, t.collation]
                + (
                    [[[c.name, c.data_type, c.current_collation] for c in t.columns]]
                    if t.columns is not None
                    else []
                )
                for t in inv.tables
            ),
            "objects": sorted(
                [o.object_type, o.name, o.collation_connection] for o in inv.objects
            ),
        }
        blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def _serialize_summary(self, job: CollationConversionJob) -> dict:
        return {
            "id": job.id,
            "server_id": job.server_id,
            "database_name": job.database_name,
            "database_id": job.database_id,
            "engine": job.engine,
            "mode": job.mode,
            "target_charset": job.target_charset,
            "target_collation": job.target_collation,
            "previous_db_charset": job.previous_db_charset,
            "previous_db_collation": job.previous_db_collation,
            "status": job.status,
            "phase": job.phase,
            "progress": json.loads(job.progress) if job.progress else None,
            "error": job.error,
            "expired": job.expires_at < _utcnow(),
            "created_at": job.created_at,
            "expires_at": job.expires_at,
            "started_at": job.started_at,
            "finished_at": job.finished_at,
        }

    @staticmethod
    def _serialize_item(it: CollationConversionJobItem) -> dict:
        return {
            "id": it.id, "job_id": it.job_id, "seq": it.seq,
            "object_type": it.object_type, "object_name": it.object_name,
            "previous_charset": it.previous_charset,
            "previous_collation": it.previous_collation,
            "status": it.status, "error": it.error,
            "grants_captured": it.grants_captured,
            "grants_reapplied": it.grants_reapplied,
            "grants_error": it.grants_error,
            "columns_affected": it.columns_affected,
            "execution_ms": it.execution_ms, "executed_at": it.executed_at,
        }

    def _assert_not_expired(self, job: CollationConversionJob) -> None:
        if job.expires_at < _utcnow():
            raise AppHttpException(
                message="El plan de conversión expiró; vuelve a crearlo.",
                status_code=410,
                context={"collation_conversion_job_id": job.id},
            )

    # ------------------------------------------------------------------ #
    # Crear plan                                                          #
    # ------------------------------------------------------------------ #
    def create_plan(
        self,
        server_id: int,
        database: str,
        *,
        target_charset: str | None,
        target_collation: str,
        admin: dict | None = None,
    ) -> dict:
        dialect, target, managed_id = self._load_context(server_id, database)
        mode = self._mode_for_dialect(dialect)

        # La BD ya existe (no la crea el gateway) → whitelist permisiva + guard de sistema:
        # convertir la collation de `mysql`/`information_schema` no tiene sentido y podría
        # dejar el servidor inconsistente.
        validate_identifier(database, dialect, "base de datos", allow_existing=True)
        ensure_not_reserved_database(database, dialect)

        charset: str | None = None
        if mode == COLLATION_MODE_UNIVERSAL:
            # target_charset es OBLIGATORIO acá (a diferencia de create_database, donde
            # omitirlo es válido): el ALTER DATABASE/ALTER TABLE de este pipeline SIEMPRE
            # emite ``CHARACTER SET {cs} COLLATE {co}`` juntos (ver _build_plan), así que no
            # hay DDL válido posible con charset ausente. Validarlo ACÁ, antes de llamar a
            # resolve_enabled_combination, evita un bug real: pedir "solo collation" hace que
            # esa función tome legítimamente la rama "solo collation" y devuelva
            # ``(None, <collation habilitada>)`` — un ÉXITO real que un chequeo posterior de
            # "¿charset es truthy?" interpretaría como fallo, con un mensaje que no explica
            # nada (a diferencia del 422 que resolve_enabled_combination ya sabe emitir, con
            # ``allowed`` en public_context).
            if not target_charset:
                raise AppHttpException(
                    message=(
                        "target_charset es obligatorio para MySQL/MariaDB: esta operación "
                        "siempre fija charset y collation juntos (ALTER DATABASE/ALTER TABLE "
                        "... CHARACTER SET ... COLLATE ... exige ambos)."
                    ),
                    status_code=422,
                    context={"target_charset": target_charset},
                )
            # Catálogo GLOBAL de charsets/collations: el PAR debe estar HABILITADO antes de
            # tocar el motor (422 si no — resolve_enabled_combination ya lo levanta con
            # ``allowed`` en public_context), y se reemplazan los valores por la forma
            # CANÓNICA del catálogo — lo que viaja al DDL sale siempre de la tabla, nunca del
            # texto crudo del request. Mismo criterio que
            # ``ServerDatabaseController.create_database``.
            charset, collation = charset_catalog.resolve_enabled_combination(
                dialect, target_charset, target_collation
            )
            adapter = get_adapter(target)
            if database not in adapter.list_databases():
                raise AppHttpException(
                    message="La base de datos no existe en el servidor.",
                    status_code=404,
                    context={"database": database},
                )
        else:
            adapter = get_adapter(target)
            if database not in adapter.list_databases():
                raise AppHttpException(
                    message="La base de datos no existe en el servidor.",
                    status_code=404,
                    context={"database": database},
                )
            # El objetivo se valida contra el catálogo VIVO del servidor ANTES de calcular el
            # inventario, para que ``needs_conversion`` se compute contra el nombre exacto
            # del catálogo y no contra el texto del request.
            collation = self._resolve_pg_collation(
                adapter, database, target_charset, target_collation
            )

        inv = adapter.collation_inventory(database, target_collation=collation)

        expires = _utcnow() + timedelta(hours=COLLATION_CONVERSION_TTL_HOURS)
        session = self._session()
        try:
            job = CollationConversionJob(
                server_id=server_id,
                database_name=database,
                database_id=managed_id,
                engine=dialect,
                mode=mode,
                target_charset=charset,
                target_collation=collation,
                previous_db_charset=inv.db_charset,
                previous_db_collation=inv.db_collation,
                source_fingerprint=self._inventory_fingerprint(inv),
                expires_at=expires,
                status=COLLATION_STATUS_PENDING,
            )
            session.add(job)
            session.commit()
            session.refresh(job)
            result = self._serialize_summary(job)
            job_id = job.id
        finally:
            session.close()

        audit.record(
            "collation_conversion.plan",
            admin=admin,
            target_type="managed_database" if managed_id is not None else "server_database",
            target_id=managed_id,
            server_id=server_id,
            touched_engine=True,  # se leyó el inventario del motor (solo lectura)
            detail=(
                f"plan de conversión {job_id} ({mode}): {server_id}/{database} → "
                f"{charset or '-'}/{collation}"
            ),
        )
        return result

    # ------------------------------------------------------------------ #
    # Lectura                                                             #
    # ------------------------------------------------------------------ #
    def get_plan(self, job_id: int) -> dict:
        session = self._session()
        try:
            return self._serialize_summary(self._job_or_404(session, job_id))
        finally:
            session.close()

    def get_objects(self, job_id: int) -> dict:
        """
        Inventario EN VIVO de la BD para que el frontend arme la selección: tablas con su
        charset/collation actual, resumen agrupado por par (charset, collation) y los 5 tipos
        de objeto con la collation congelada que arrastran.
        """
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            fields = (
                job.server_id, job.database_name, job.target_charset,
                job.target_collation, job.engine, job.mode,
            )
        finally:
            session.close()
        server_id, database, charset, collation, dialect, mode = fields

        target = self._job_target_by_server(server_id)
        adapter = get_adapter(target)
        inv = adapter.collation_inventory(database, target_collation=collation)
        warnings = (
            self._external_fk_warnings(adapter, database)
            if mode == COLLATION_MODE_UNIVERSAL
            else self._nondeterministic_warnings(inv, collation)
        )
        return {
            "job_id": job_id,
            "database": database,
            "engine": dialect,
            "mode": mode,
            "db_charset": inv.db_charset,
            "db_collation": inv.db_collation,
            "target_charset": charset,
            "target_collation": collation,
            "tables": [t.model_dump() for t in inv.tables],
            "summary": [g.model_dump() for g in inv.summary],
            "objects": [o.model_dump() for o in inv.objects],
            "available_collations": [c.model_dump() for c in inv.available_collations],
            "notes": inv.notes,
            "warnings": warnings,
        }

    @staticmethod
    def _nondeterministic_warnings(inv, collation: str) -> list[str]:
        """
        Aviso si la collation objetivo es NO DETERMINISTA (PostgreSQL 12+, solo ICU).

        No es un detalle académico: en PostgreSQL 12–17 una collation no determinista NO
        admite comparación por patrón (``LIKE``, expresiones regulares) ni deduplicación de
        índices B-tree, así que convertir una columna que hoy se filtra con ``LIKE`` la
        rompe. Además el gateway no puede saber si la aplicación usa esos operadores: solo
        puede avisar.
        """
        hit = next(
            (c for c in inv.available_collations if c.name == collation), None
        )
        if hit is None or hit.deterministic:
            return []
        return [
            f"La collation objetivo `{collation}` es NO DETERMINISTA (dos cadenas con bytes "
            "distintos pueden considerarse iguales). En PostgreSQL 12–17 eso IMPIDE la "
            "comparación por patrón sobre esas columnas (LIKE, expresiones regulares, "
            "operadores *_pattern_ops) y la deduplicación de índices B-tree: cualquier "
            "consulta que hoy use LIKE sobre una columna convertida va a fallar."
        ]

    def _job_target_by_server(self, server_id: int) -> ServerTarget:
        session = self._session()
        try:
            return build_target(get_server_or_404(session, server_id))
        finally:
            session.close()

    @staticmethod
    def _external_fk_warnings(adapter, database: str) -> list[str]:
        """
        Advierte si alguna tabla de OTRA base de datos del mismo servidor tiene una FK hacia
        ``database``. Reusa ``ServerAdapter.external_fk_dependents`` TAL CUAL (mismo criterio
        que ``CloneController._external_fk_warnings``).

        Importa acá por una razón propia de esta operación: MySQL/MariaDB exigen que las
        columnas de los dos lados de una FK tengan el MISMO charset/collation. Convertir la
        tabla referida y no la que la referencia (imposible si vive en otra BD: este job
        opera sobre una sola) rompe con ``(3780, 'Referencing column ... are incompatible')``
        o ``(1832, 'Cannot change column ...: used in a foreign key constraint')``.
        Best-effort: si la consulta falla, no bloquea la lectura. PostgreSQL devuelve ``[]``
        (no soporta FKs cross-database).
        """
        try:
            deps = adapter.external_fk_dependents(database)
        except AppHttpException:
            return []
        except Exception:  # noqa: BLE001 — un aviso nunca debe tumbar el inventario
            return []
        if not deps:
            return []
        examples = ", ".join(
            f"`{d.schema_name}`.`{d.table}`.`{d.column}` → `{d.referenced_table}`"
            for d in deps[:5]
        )
        more = f" (+{len(deps) - 5} más)" if len(deps) > 5 else ""
        return [
            f"Hay {len(deps)} columna(s) en OTRA(S) base(s) de datos del servidor con una FK "
            f"hacia `{database}`: {examples}{more}. MySQL/MariaDB exigen el MISMO "
            "charset/collation en ambos lados de una FK, así que convertir esta BD puede "
            "hacer fallar la conversión de esas tablas (3780/1832) o dejar las referencias "
            "externas incompatibles. Esas otras bases NO se convierten con este job: "
            "planificá una conversión para cada una."
        ]

    def list_items(self, job_id: int, *, limit: int, offset: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            self._job_or_404(session, job_id)
            q = session.query(CollationConversionJobItem).filter(
                CollationConversionJobItem.job_id == job_id
            )
            total = q.count()
            rows = (
                q.order_by(CollationConversionJobItem.seq.asc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [self._serialize_item(r) for r in rows], total
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Construcción del plan                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _session_collation_sql(charset: str, collation: str, dialect: str) -> str:
        """
        ``SET NAMES`` con la collation objetivo: la pieza CENTRAL del feature.

        Recrear un objeto NO alcanza si la sesión que lo recrea tiene la collation vieja: el
        objeto volvería a congelar exactamente la misma collation que se quería cambiar. Por
        eso el ``DROP``+``CREATE`` de cada objeto viaja SIEMPRE precedido por esta sentencia,
        en la MISMA conexión (``execute_adhoc`` ejecuta la lista completa sobre una sola
        conexión, y los engines remotos usan ``NullPool``, así que el ``SET`` no se filtra a
        otra operación).

        Los valores son la forma CANÓNICA del catálogo y además pasan por whitelist acá:
        viajan al SQL como identificadores, no como parámetros.
        """
        cs = validate_identifier(charset, dialect, "charset")
        co = validate_identifier(collation, dialect, "collation")
        return f"SET NAMES {cs} COLLATE {co}"

    def _build_plan(
        self,
        job: CollationConversionJob,
        inv,
        selection: dict,
        adapter=None,
    ) -> _Plan:
        """Despacha por MODO. El modo ``universal`` (MySQL/MariaDB) es el cuerpo de abajo."""
        if job.mode == COLLATION_MODE_COLUMNS:
            return self._build_plan_columns(job, inv, selection, adapter)
        dialect = job.engine
        charset = job.target_charset
        collation = job.target_collation
        db_q = quote_identifier(
            validate_identifier(job.database_name, dialect, "base de datos", allow_existing=True),
            dialect,
        )
        cs = validate_identifier(charset, dialect, "charset")
        co = validate_identifier(collation, dialect, "collation")

        plan = _Plan(include_database_default=bool(selection.get("include_database_default", True)))

        # --- Paso 1: el DEFAULT de la BD ------------------------------------------- #
        # Cambia SOLO el default que heredarán los objetos NUEVOS; NO toca las tablas
        # existentes (por eso hace falta el paso 2) ni los objetos ya creados (paso 3). Va
        # PRIMERO para que los objetos recreados después queden asociados al default nuevo.
        if plan.include_database_default:
            plan.steps.append(
                _Step(
                    object_type=COLLATION_OBJ_DATABASE,
                    object_name=job.database_name,
                    action="alter_database",
                    sql=f"ALTER DATABASE {db_q} CHARACTER SET {cs} COLLATE {co}",
                    previous_charset=inv.db_charset,
                    previous_collation=inv.db_collation,
                )
            )

        # --- Paso 2: tablas -------------------------------------------------------- #
        by_name = {t.name: t for t in inv.tables}
        wanted_tables = list(dict.fromkeys(selection.get("tables") or []))
        for name in wanted_tables:
            info = by_name.get(name)
            if info is None:
                plan.missing_tables.append(name)
                continue
            t_q = quote_identifier(
                validate_identifier(name, dialect, "tabla", allow_existing=True), dialect
            )
            if not info.needs_conversion:
                # ``CONVERT TO CHARACTER SET`` REESCRIBE la tabla completa incluso cuando no
                # cambia nada, así que saltearla es un ahorro real (y ``needs_conversion``
                # ya tuvo en cuenta las columnas con COLLATE explícito, no solo el default).
                plan.steps.append(
                    _Step(
                        object_type=COLLATION_OBJ_TABLE, object_name=name, action="skip",
                        reason="La tabla y todas sus columnas de texto ya están en la collation objetivo.",
                        previous_charset=info.charset, previous_collation=info.collation,
                    )
                )
                continue
            plan.steps.append(
                _Step(
                    object_type=COLLATION_OBJ_TABLE,
                    object_name=name,
                    action="convert_table",
                    sql=f"ALTER TABLE {db_q}.{t_q} CONVERT TO CHARACTER SET {cs} COLLATE {co}",
                    previous_charset=info.charset,
                    previous_collation=info.collation,
                )
            )

        # --- Paso 3: objetos con collation congelada ------------------------------- #
        obj_index = {(o.object_type, o.name): o for o in inv.objects}
        seen: set[tuple[str, str]] = set()
        for ref in selection.get("objects") or []:
            key = (ref["object_type"], ref["name"])
            if key in seen:
                continue
            seen.add(key)
            info = obj_index.get(key)
            if info is None:
                plan.missing.append({"object_type": key[0], "name": key[1]})
                continue
            # El DDL real se captura EN LA EJECUCIÓN, no acá: entre el preview y el execute el
            # cuerpo pudo cambiar, y recrear un cuerpo viejo perdería ese cambio. El preview
            # muestra la FORMA del paso.
            plan.steps.append(
                _Step(
                    object_type=key[0],
                    object_name=key[1],
                    action="recreate",
                    sql=(
                        f"SET NAMES {cs} COLLATE {co}; "
                        f"DROP {_DROP_KEYWORDS[key[0]]} IF EXISTS {db_q}."
                        f"{quote_identifier(validate_identifier(key[1], dialect, key[0], allow_existing=True), dialect)}; "
                        f"<SHOW CREATE {_DROP_KEYWORDS[key[0]]} capturado en la ejecución>"
                    ),
                    previous_collation=info.collation_connection,
                )
            )

        plan.warnings.extend(self._plan_warnings(inv, plan, wanted_tables, collation))
        return plan

    # ------------------------------------------------------------------ #
    # Plan del modo ``columns`` (PostgreSQL)                              #
    # ------------------------------------------------------------------ #
    def _build_plan_columns(
        self, job: CollationConversionJob, inv, selection: dict, adapter
    ) -> _Plan:
        """
        Plan de PostgreSQL: UN ``ALTER TABLE`` por tabla seleccionada, con una acción
        ``ALTER COLUMN ... SET DATA TYPE <mismo tipo> COLLATE "x"`` por cada columna de
        texto que todavía no esté en la collation objetivo.

        Lo que este plan NO tiene, y no es un olvido:

        - **Sin ``ALTER DATABASE``**: el ``ENCODING``/``LC_COLLATE``/``LC_CTYPE`` de una BD
          es INMUTABLE tras el ``CREATE DATABASE``. ``include_database_default`` se ignora.
        - **Sin recreación de objetos**: PostgreSQL resuelve la collation dinámicamente en
          cada llamada, así que vistas, funciones y triggers NO arrastran nada congelado.
          Una selección de objetos es un error del cliente y se rechaza (422) en vez de
          ignorarse en silencio.

        Las columnas de una tabla van TODAS en la misma sentencia: una sola pasada, un solo
        ``ACCESS EXCLUSIVE`` y —como PostgreSQL SÍ tiene DDL transaccional— atomicidad real
        por tabla (nunca "media tabla convertida").
        """
        if adapter is None:  # defensa: el modo columns necesita el adapter para rendear
            raise AppHttpException(
                message="No se pudo resolver el plan de conversión (adapter ausente).",
                status_code=500,
                context={"mode": job.mode},
            )
        if selection.get("objects"):
            raise AppHttpException(
                message=(
                    "PostgreSQL no recrea vistas, funciones, triggers ni eventos en una "
                    "conversión de collation: los resuelve dinámicamente y no congelan nada. "
                    "Enviá solo la selección de tablas."
                ),
                status_code=422,
                context={"objects": len(selection.get("objects") or [])},
            )

        collation = job.target_collation
        # El paso de BD NO existe en este modo: el plan nace con include_database_default en
        # False sea lo que sea que haya pedido el cliente (es el default del schema).
        plan = _Plan(include_database_default=False)
        by_name = {t.name: t for t in inv.tables}
        wanted_tables = list(dict.fromkeys(selection.get("tables") or []))
        converted: set[tuple[str, str]] = set()

        for name in wanted_tables:
            info = by_name.get(name)
            if info is None:
                plan.missing_tables.append(name)
                continue
            cols = adapter.columns_to_convert(info, collation)
            if not cols:
                plan.steps.append(
                    _Step(
                        object_type=COLLATION_OBJ_TABLE, object_name=name, action="skip",
                        reason=(
                            "Ninguna columna de texto de la tabla necesita cambio: ya están "
                            "todas en la collation objetivo."
                        ),
                    )
                )
                continue
            plan.steps.append(
                _Step(
                    object_type=COLLATION_OBJ_TABLE,
                    object_name=name,
                    action="convert_columns",
                    sql=adapter.render_collation_change(
                        job.database_name, name, cols, collation
                    ),
                    columns=tuple(c.name for c in cols),
                )
            )
            converted.update((name, c.name) for c in cols)

        plan.warnings.extend(
            self._plan_warnings_columns(
                adapter, job.database_name, inv, plan, wanted_tables, collation, converted
            )
        )
        return plan

    @staticmethod
    def _plan_warnings_columns(
        adapter, database: str, inv, plan: _Plan, wanted_tables: list[str],
        collation: str, converted: set[tuple[str, str]],
    ) -> list[str]:
        out: list[str] = [
            "PostgreSQL no permite cambiar el ENCODING ni el LC_COLLATE de una base ya "
            "creada: este plan NO incluye ningún ALTER DATABASE (para eso hay que volcar y "
            "recargar en una base nueva, es decir el módulo de clonado). Tampoco recrea "
            "vistas/funciones/triggers: no hace falta.",
        ]

        if any(s.action == "convert_columns" for s in plan.steps):
            out.append(
                "Cada ALTER TABLE ... ALTER COLUMN ... TYPE toma un lock ACCESS EXCLUSIVE "
                "sobre la tabla durante toda la operación (bloquea hasta los SELECT) y "
                "RECONSTRUYE todos los índices que incluyan esas columnas: cambiar la "
                "collation cambia el orden, así que el índice viejo no sirve. Como el tipo "
                "es el mismo, PostgreSQL normalmente NO reescribe la tabla en sí, pero la "
                "reconstrucción de índices en tablas grandes ya es una operación larga. "
                "Verificalo contra tu versión antes de una ventana ajustada."
            )

        # Selección PARCIAL: en PostgreSQL el conflicto NO lo rechaza el DDL (como en
        # MySQL/MariaDB) — aparece al CONSULTAR.
        pending = [
            t.name for t in inv.tables
            if t.needs_conversion and t.name not in set(wanted_tables)
        ]
        if pending:
            sample = ", ".join(f"`{n}`" for n in pending[:5])
            more = f" (+{len(pending) - 5} más)" if len(pending) > 5 else ""
            out.append(
                f"Quedan {len(pending)} tabla(s) con columnas de texto fuera de la collation "
                f"objetivo: {sample}{more}. PostgreSQL no rechaza el DDL por esto (a "
                "diferencia de MySQL/MariaDB), pero comparar dos columnas con collations "
                "distintas falla al EJECUTAR la consulta: 'could not determine which "
                "collation to use for string comparison' (SQLSTATE 42P22) en un `=`/`<` o un "
                "JOIN, y 'collation mismatch between implicit collations' (42P21) al "
                "planificar un COALESCE/CASE/UNION/ORDER BY."
            )

        # FKs de texto que quedan con los dos lados en collations distintas.
        try:
            fks = adapter.collatable_foreign_keys(database)
        except Exception:  # noqa: BLE001 — un aviso nunca debe tumbar el preview
            fks = []
        current: dict[tuple[str, str], str | None] = {
            (t.name, c.name): (None if c.is_default_collation else c.current_collation)
            for t in inv.tables
            for c in (t.columns or [])
        }

        def final_collation(key: tuple[str, str]) -> str | None:
            return collation if key in converted else current.get(key)

        broken: list[str] = []
        for fk in fks:
            src = (fk.table, fk.column)
            dst = (fk.referenced_table, fk.referenced_column)
            if src not in current or dst not in current:
                continue  # una punta no es colacionable/visible: no hay conflicto que avisar
            if src not in converted and dst not in converted:
                continue  # este plan no cambia nada de esta FK
            if final_collation(src) == final_collation(dst):
                continue
            broken.append(
                f"`{fk.table}`.`{fk.column}` → `{fk.referenced_table}`."
                f"`{fk.referenced_column}`"
            )
        if broken:
            sample = ", ".join(broken[:5])
            more = f" (+{len(broken) - 5} más)" if len(broken) > 5 else ""
            out.append(
                f"{len(broken)} FOREIGN KEY de texto quedarían con los dos lados en "
                f"collations DISTINTAS: {sample}{more}. PostgreSQL (hasta la 17) NO valida la "
                "collation de una FK ni revalida el constraint después de un ALTER, así que "
                "no vas a ver ningún error al aplicar esto: el JOIN de la FK va a fallar en "
                "tiempo de consulta (42P22) y la propia FK puede quedar lógicamente "
                "inconsistente. PostgreSQL 18 sí lo exige, y un pg_dump/pg_upgrade a 18 "
                "fallaría al restaurar. Incluí ambas tablas en la conversión."
            )
        return out

    @staticmethod
    def _plan_warnings(inv, plan: _Plan, wanted_tables: list[str], collation: str) -> list[str]:
        out: list[str] = []

        # Selección PARCIAL de tablas: el riesgo más concreto de este feature después de los
        # objetos congelados. MySQL/MariaDB exigen el MISMO charset/collation en ambos lados
        # de una FK, así que convertir unas tablas y no otras puede fallar con 3780/1832, o
        # dejar comparaciones entre columnas de distinta collation (Illegal mix of
        # collations) que antes funcionaban.
        pending = [t.name for t in inv.tables if t.needs_conversion and t.name not in set(wanted_tables)]
        if pending:
            sample = ", ".join(f"`{n}`" for n in pending[:5])
            more = f" (+{len(pending) - 5} más)" if len(pending) > 5 else ""
            out.append(
                f"Quedan {len(pending)} tabla(s) sin convertir que NO están en la collation "
                f"objetivo: {sample}{more}. MySQL/MariaDB exigen la misma collation en ambos "
                "lados de una FK y comparar columnas de collations distintas produce "
                "'Illegal mix of collations': una conversión parcial puede romper consultas "
                "que hoy funcionan."
            )

        outdated = [o for o in inv.objects if o.is_outdated]
        selected_objs = {
            (s.object_type, s.object_name) for s in plan.steps if s.action == "recreate"
        }
        left = [o for o in outdated if (o.object_type, o.name) not in selected_objs]
        if left:
            sample = ", ".join(f"{o.object_type} `{o.name}`" for o in left[:5])
            more = f" (+{len(left) - 5} más)" if len(left) > 5 else ""
            out.append(
                f"Quedan {len(left)} objeto(s) con la collation vieja congelada y sin "
                f"recrear: {sample}{more}. Es EXACTAMENTE el caso que esta herramienta "
                "existe para evitar: sus parámetros VARCHAR/CHAR, variables DECLARE y "
                "literales seguirán en la collation anterior y producirán 'Illegal mix of "
                "collations' en producción."
            )

        if any(s.action == "convert_table" for s in plan.steps):
            out.append(
                "ALTER TABLE ... CONVERT TO CHARACTER SET REESCRIBE cada tabla completa "
                "(puede tardar y bloquear escrituras en tablas grandes) y convierte también "
                "los datos de sus columnas de texto. Si el charset nuevo usa más bytes por "
                "carácter (p. ej. utf8mb3 → utf8mb4), un índice existente puede superar el "
                "límite de longitud de clave de InnoDB y fallar con "
                "(1071, 'Specified key was too long')."
            )

        if any(
            s.action == "recreate" and s.object_type in _GRANTED_OBJECT_TYPES
            for s in plan.steps
        ):
            out.append(
                "Al dropear una PROCEDURE/FUNCTION, MySQL/MariaDB BORRAN sus privilegios de "
                "rutina. El gateway los lee de mysql.procs_priv antes del DROP y los "
                "reaplica después; si no puede leerlos, NO dropea la rutina y reporta el "
                "paso como error (nunca destruye privilegios que no podría restaurar)."
            )

        if any(s.action == "recreate" and s.object_type == "event" for s in plan.steps):
            out.append(
                "Un EVENT con una fecha de ejecución ya pasada y ON COMPLETION NOT PRESERVE "
                "puede rechazar su recreación verbatim ('Event execution time is in the "
                "past'). Ese paso quedará en error sin afectar a los demás; el DDL capturado "
                "queda guardado en el ítem para recrearlo a mano con una fecha nueva."
            )

        return out

    # ------------------------------------------------------------------ #
    # Token de confirmación                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def execution_token(db_ref: str, charset: str | None, collation: str, plan: _Plan) -> str:
        """
        SHA256 del plan EXACTO. Mismo mecanismo que ``CloneController.clone_execution_token``
        (y no el HMAC stateless de ``confirm_token.py``, que se usa donde NO hay fila donde
        anclar el plan): acá el job persistido es el ancla, y atar el token al plan hace que
        cualquier cambio de selección, de objetivo o del inventario invalide la confirmación.
        """
        parts: list[str] = [
            db_ref, charset or "", collation, str(int(plan.include_database_default))
        ]
        for s in plan.steps:
            parts.append(f"{s.action}:{s.object_type}:{s.object_name}:{s.sql or ''}")
        blob = "\x1f".join(parts)
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    # ------------------------------------------------------------------ #
    # Preview                                                             #
    # ------------------------------------------------------------------ #
    def preview(
        self,
        job_id: int,
        *,
        tables: list[str],
        objects: list[dict],
        include_database_default: bool = True,
        force: bool = False,
    ) -> dict:
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._assert_not_expired(job)
            if job.status != COLLATION_STATUS_PENDING:
                raise AppHttpException(
                    message=(
                        f"El job ya está en estado '{job.status}'; crea un plan nuevo para "
                        "previsualizar otra conversión."
                    ),
                    status_code=409,
                    context={"status": job.status},
                )
            server_id = job.server_id
            database = job.database_name
            collation = job.target_collation
            charset = job.target_charset
            fingerprint = job.source_fingerprint
            mode = job.mode
        finally:
            session.close()

        target = self._job_target_by_server(server_id)
        adapter = get_adapter(target)
        inv = adapter.collation_inventory(database, target_collation=collation)
        current_fp = self._inventory_fingerprint(inv)
        if current_fp != fingerprint and not force:
            raise AppHttpException(
                message=(
                    "El inventario de la base de datos cambió desde que se creó el plan "
                    "(se agregaron/borraron objetos o cambió alguna collation). Volvé a "
                    "crear el plan, o reintentá con force=true."
                ),
                status_code=409,
                context={"collation_conversion_job_id": job_id},
            )

        selection = {
            "tables": tables,
            "objects": objects,
            "include_database_default": include_database_default,
        }

        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            plan = self._build_plan(job, inv, selection, adapter)
            db_ref = f"{job.server_id}:{job.database_name}"
            token = self.execution_token(db_ref, charset, collation, plan)
            job.selection = json.dumps(selection)
            job.confirm_token = token
            # Con force=true se ADOPTA el inventario nuevo como base: si no, el execute
            # volvería a chocar con el mismo 409 y force quedaría sin efecto real.
            if force:
                job.source_fingerprint = current_fp
            job.previous_db_charset = inv.db_charset
            job.previous_db_collation = inv.db_collation
            session.commit()
        finally:
            session.close()

        return {
            "job_id": job_id,
            "database": database,
            "mode": mode,
            "target_charset": charset,
            "target_collation": collation,
            "include_database_default": plan.include_database_default,
            "steps": [
                {
                    "object_type": s.object_type, "object_name": s.object_name,
                    "action": s.action, "sql": s.sql, "reason": s.reason,
                    "columns": list(s.columns) or None,
                }
                for s in plan.steps
            ],
            "tables_to_convert": sum(
                1 for s in plan.steps if s.action in _CONVERT_ACTIONS
            ),
            "tables_skipped": sum(
                1 for s in plan.steps
                if s.action == "skip" and s.object_type == COLLATION_OBJ_TABLE
            ),
            "columns_to_convert": sum(len(s.columns) for s in plan.steps),
            "objects_to_recreate": sum(1 for s in plan.steps if s.action == "recreate"),
            "missing": plan.missing,
            "missing_tables": plan.missing_tables,
            # El aviso de FK cross-database es de MySQL/MariaDB: PostgreSQL no soporta FKs
            # entre bases, y su aviso de FK (intra-BD, por collation) ya viene en
            # ``plan.warnings``.
            "warnings": plan.warnings + (
                self._external_fk_warnings(adapter, database)
                if mode == COLLATION_MODE_UNIVERSAL else []
            ),
            "confirm_token": token,
        }

    # ------------------------------------------------------------------ #
    # Execute (valida y encola el job asíncrono)                          #
    # ------------------------------------------------------------------ #
    def execute(
        self,
        job_id: int,
        *,
        confirm_target_name: str,
        confirm_token: str,
        force: bool = False,
        admin: dict | None = None,
    ) -> dict:
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            self._assert_not_expired(job)
            if job.status != COLLATION_STATUS_PENDING:
                raise AppHttpException(
                    message=f"El job ya está en estado '{job.status}'; no se puede re-ejecutar.",
                    status_code=409,
                    context={"status": job.status},
                )
            if job.selection is None:
                raise AppHttpException(
                    message="Falta previsualizar el plan antes de ejecutarlo.",
                    status_code=409,
                    context={"collation_conversion_job_id": job_id},
                )
            # Confirmación de DOBLE factor de backend: nombre exacto de la BD (obliga a
            # identificar CONSCIENTEMENTE cuál se convierte) + token atado al plan resuelto.
            if confirm_target_name != job.database_name:
                raise AppHttpException(
                    message="confirm_target_name no coincide con el nombre de la base de datos.",
                    status_code=422,
                    context={"required": "confirm_target_name == database"},
                )
            if job.database_id is not None and not force:
                md = session.get(ManagedDatabase, job.database_id)
                if md is not None and md.status == ProvisionStatus.error:
                    raise AppHttpException(
                        message=(
                            "La base de datos está en cuarentena (status=error). "
                            "Reintentá con force=true."
                        ),
                        status_code=409,
                        context={"managed_database_id": job.database_id},
                    )
            server_id = job.server_id
            database = job.database_name
            collation = job.target_collation
            charset = job.target_charset
            fingerprint = job.source_fingerprint
            managed_id = job.database_id
            selection = json.loads(job.selection)
        finally:
            session.close()

        # Anti-TOCTOU: releer el inventario y revalidar el token contra el plan ACTUAL.
        target = self._job_target_by_server(server_id)
        adapter = get_adapter(target)
        inv = adapter.collation_inventory(database, target_collation=collation)
        current_fp = self._inventory_fingerprint(inv)
        if current_fp != fingerprint and not force:
            raise AppHttpException(
                message=(
                    "El inventario de la base de datos cambió desde el preview; volvé a "
                    "previsualizar (o reintentá con force=true)."
                ),
                status_code=409,
                context={"collation_conversion_job_id": job_id},
            )
        if current_fp != fingerprint and force:
            # ADOPTAR el inventario actual como base, igual que preview(force=true). Sin
            # esto, force=true acá pasaba este chequeo pero el WORKER (_pipeline) vuelve a
            # comparar el fingerprint de forma INCONDICIONAL al arrancar — como red de
            # seguridad final, sin enterarse de este `force` — y encontraba el mismo drift,
            # matando el job en `failed` unos segundos después sin haber tocado el motor.
            # force quedaba sin efecto real: 200 en el momento, pero la conversión moría sola.
            session = self._session()
            try:
                job = self._job_or_404(session, job_id)
                job.source_fingerprint = current_fp
                session.commit()
            finally:
                session.close()

        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            plan = self._build_plan(job, inv, selection, adapter)
            db_ref = f"{server_id}:{database}"
            expected = self.execution_token(db_ref, charset, collation, plan)
        finally:
            session.close()
        if confirm_token != expected:
            raise AppHttpException(
                message="confirm_token no coincide con el plan actual; volvé a previsualizar.",
                status_code=422,
                context={},
            )
        if not any(s.action != "skip" for s in plan.steps):
            raise AppHttpException(
                message=(
                    "El plan no tiene ningún paso que ejecutar (ni ALTER DATABASE, ni tablas "
                    "que convertir, ni objetos que recrear)."
                ),
                status_code=422,
                context={"steps": len(plan.steps)},
            )

        # Auditoría de intención FAIL-CLOSED antes de encolar: rastro durable garantizado.
        audit.record_intent(
            "collation_conversion.execute",
            admin=admin,
            target_type="managed_database" if managed_id is not None else "server_database",
            target_id=managed_id,
            server_id=server_id,
            touched_engine=True,
            detail=(
                f"conversión {job_id}: {db_ref} → {charset or '-'}/{collation} "
                f"(tablas={sum(1 for s in plan.steps if s.action in _CONVERT_ACTIONS)}, "
                f"columnas={sum(len(s.columns) for s in plan.steps)}, "
                f"objetos={sum(1 for s in plan.steps if s.action == 'recreate')})"
            ),
        )

        from app.services import collation_conversion_runner

        collation_conversion_runner.enqueue(job_id)
        return self.get_plan(job_id)

    # ------------------------------------------------------------------ #
    # Cancelación / barrido                                               #
    # ------------------------------------------------------------------ #
    def cancel(self, job_id: int, *, admin: dict | None = None) -> dict:
        """
        Cancelación COOPERATIVA: el worker corta en el próximo punto seguro (entre pasos).

        NO interrumpe un ``ALTER TABLE ... CONVERT TO CHARACTER SET`` en curso — esa
        sentencia la ejecuta el motor y solo terminaría matando la conexión, lo que dejaría
        la tabla a medio reescribir. Cancelar detiene los pasos que TODAVÍA no empezaron.
        """
        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            if job.status not in (COLLATION_STATUS_PENDING, COLLATION_STATUS_RUNNING):
                raise AppHttpException(
                    message=f"El job no se puede cancelar en estado '{job.status}'.",
                    status_code=409,
                    context={"status": job.status},
                )
            job.cancel_requested = True
            session.commit()
            session.refresh(job)
            return self._serialize_summary(job)
        finally:
            session.close()

    def sweep_interrupted(self) -> int:
        """Marca ``running → interrupted`` (barrido de arranque tras un reinicio)."""
        session = self._session()
        try:
            rows = (
                session.query(CollationConversionJob)
                .filter(CollationConversionJob.status == COLLATION_STATUS_RUNNING)
                .all()
            )
            for job in rows:
                job.status = COLLATION_STATUS_INTERRUPTED
                job.finished_at = _utcnow()
                job.error = (
                    "El proceso se reinició mientras el job estaba en ejecución. Revisá los "
                    "ítems ya aplicados antes de crear un plan nuevo."
                )
            session.commit()
            return len(rows)
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Persistencia de estado (usada por el worker)                        #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _clean_error(exc: Exception) -> str:
        orig = getattr(exc, "orig", None)
        return str(orig if orig is not None else exc)[:500]

    def _set_status(self, job_id, status, *, phase=None, error=None, finished=False):
        session = self._session()
        try:
            job = session.get(CollationConversionJob, job_id)
            if job is None:
                return
            job.status = status
            if phase is not None:
                job.phase = phase
            if error is not None:
                job.error = error
            if finished:
                job.finished_at = _utcnow()
            session.commit()
        finally:
            session.close()

    def _set_progress(self, job_id, progress: dict):
        session = self._session()
        try:
            job = session.get(CollationConversionJob, job_id)
            if job is not None:
                job.progress = json.dumps(progress)
                session.commit()
        finally:
            session.close()

    def _record_item(self, job_id: int, row: dict) -> None:
        session = self._session()
        try:
            session.add(CollationConversionJobItem(job_id=job_id, **row))
            session.commit()
        finally:
            session.close()

    def _cancel_checker(self, job_id):
        """Callable que lee ``cancel_requested``, cacheado 2s para no martillar la BD."""
        state = {"val": False, "ts": 0.0}

        def check() -> bool:
            now = time.monotonic()
            if now - state["ts"] > 2.0:
                session = self._session()
                try:
                    job = session.get(CollationConversionJob, job_id)
                    state["val"] = bool(job.cancel_requested) if job else False
                finally:
                    session.close()
                state["ts"] = now
            return state["val"]

        return check

    def _quarantine(self, managed_id: int) -> None:
        session = self._session()
        try:
            md = session.get(ManagedDatabase, managed_id)
            if md is not None:
                md.status = ProvisionStatus.error
                session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Ejecución asíncrona (corre en un worker de collation_conversion_runner)
    # ------------------------------------------------------------------ #
    def run_job(self, job_id: int) -> None:
        """
        Pipeline completo: ``ALTER DATABASE`` → ``ALTER TABLE ... CONVERT TO`` por tabla →
        ``DROP``+``CREATE`` por objeto. Best-effort con reporte POR ÍTEM: un paso fallido no
        aborta los demás (mismo criterio "reportar, no abortar" que ``apply_profile_bulk``/
        ``apply_all``), porque abortar dejaría la BD a mitad de camino — el estado más
        peligroso para este feature. Nunca lanza: registra el fallo en el job.
        """
        # 1) Reclamar ATÓMICAMENTE (pending → running): si dos workers compiten por el mismo
        #    job, solo uno afecta 1 fila; el otro sale sin hacer nada.
        session = self._session()
        try:
            claimed = (
                session.query(CollationConversionJob)
                .filter(
                    CollationConversionJob.id == job_id,
                    CollationConversionJob.status == COLLATION_STATUS_PENDING,
                )
                .update(
                    {
                        CollationConversionJob.status: COLLATION_STATUS_RUNNING,
                        CollationConversionJob.started_at: _utcnow(),
                        CollationConversionJob.error: None,
                    },
                    synchronize_session=False,
                )
            )
            session.commit()
            if not claimed:
                return
        finally:
            session.close()

        from app.services import collation_conversion_runner

        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            server = get_server_or_404(session, job.server_id)
            target = build_target(server)
            ctx = {
                "server_id": job.server_id,
                "database": job.database_name,
                "managed_id": job.database_id,
                "engine": job.engine,
                "mode": job.mode,
                "charset": job.target_charset,
                "collation": job.target_collation,
                "selection": json.loads(job.selection) if job.selection else {},
                "fingerprint": job.source_fingerprint,
                "db_ref": f"{job.server_id}:{job.database_name}",
            }
        finally:
            session.close()

        guard = collation_conversion_runner.database_guard(ctx["db_ref"])
        with guard:
            try:
                self._pipeline(job_id, target, ctx)
            except AppHttpException as exc:
                self._set_status(
                    job_id, COLLATION_STATUS_FAILED, error=exc.message, finished=True
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("Pipeline de conversión %s falló", job_id, exc_info=True)
                self._set_status(
                    job_id, COLLATION_STATUS_FAILED, error=self._clean_error(exc), finished=True
                )

    def _pipeline(self, job_id: int, target: ServerTarget, ctx: dict) -> None:
        cancel = self._cancel_checker(job_id)
        adapter = get_adapter(target)
        engine = EngineType(ctx["engine"])
        lock_key = (
            ctx["managed_id"]
            if ctx["managed_id"] is not None
            else _synthetic_lock_key(ctx["server_id"], ctx["database"])
        )
        runner = MigrationRunner()

        inv = adapter.collation_inventory(ctx["database"], target_collation=ctx["collation"])
        if self._inventory_fingerprint(inv) != ctx["fingerprint"]:
            # Anti-TOCTOU final: el inventario pudo cambiar entre el execute y el arranque
            # del worker. Preferimos NO tocar nada antes que convertir un esquema distinto
            # del que el operador confirmó.
            self._set_status(
                job_id, COLLATION_STATUS_FAILED, finished=True,
                error="El inventario cambió antes de ejecutar; volvé a planear la conversión.",
            )
            return

        session = self._session()
        try:
            job = self._job_or_404(session, job_id)
            plan = self._build_plan(job, inv, ctx["selection"], adapter)
        finally:
            session.close()

        # TODAS las fases mutantes corren bajo UN ÚNICO advisory lock del motor, sostenido en
        # una conexión dedicada del worker: serializa cross-proceso esta conversión contra
        # otra conversión, un clon o un execute de schema-comparison sobre la MISMA BD física
        # (comparten espacio de claves de lock).
        with runner.advisory_lock(target, engine=engine, lock_key=lock_key):
            self._run_phases(job_id, runner, adapter, target, ctx, plan, cancel, engine, lock_key)

    def _run_phases(
        self, job_id, runner, adapter, target, ctx, plan: _Plan, cancel, engine, lock_key
    ) -> None:
        seq = 0
        had_failure = False
        progress = {"phase": None, "tables_done": 0, "objects_done": 0}
        last_persist = [0.0]

        def bump(**kw):
            progress.update(kw)
            now = time.monotonic()
            if now - last_persist[0] >= _PROGRESS_PERSIST_SECONDS:
                last_persist[0] = now
                self._set_progress(job_id, dict(progress))

        db_name = ctx["database"]
        # El ``SET NAMES`` es MySQL/MariaDB puro: fija la ``collation_connection`` que el
        # motor CONGELA en el objeto recreado. En el modo ``columns`` no existe (PostgreSQL
        # no congela nada y no hay objetos que recrear), y además ``charset`` es None ahí:
        # calcularlo rompería el worker antes de empezar.
        session_sql = (
            self._session_collation_sql(ctx["charset"], ctx["collation"], ctx["engine"])
            if ctx["mode"] == COLLATION_MODE_UNIVERSAL
            else ""
        )

        def run_one(sql: str, *, prepend_session: bool = False) -> tuple[bool, str | None, int | None]:
            """Ejecuta una sentencia (opcionalmente precedida por el SET NAMES objetivo)."""
            statements = ([session_sql] if prepend_session else []) + [sql]
            results = runner.execute_adhoc(
                target, db_name=db_name, engine=engine, lock_key=lock_key,
                statements=statements, already_locked=True, stop_on_error=True,
            )
            by_index = {r.index: r for r in results}
            total_ms = sum(r.execution_ms or 0 for r in results)
            for i in range(len(statements)):
                r = by_index.get(i)
                if r is None:
                    return False, "La sentencia no se ejecutó (se cortó antes).", total_ms
                if r.status != "applied":
                    return False, r.error, total_ms
            return True, None, total_ms

        # ---------------- Fase 1: default de la BD -------------------------------- #
        db_steps = [s for s in plan.steps if s.action == "alter_database"]
        if db_steps:
            self._set_status(job_id, COLLATION_STATUS_RUNNING, phase=COLLATION_PHASE_DATABASE)
            bump(phase=COLLATION_PHASE_DATABASE)
            for step in db_steps:
                ok, err, ms = run_one(step.sql)
                had_failure = had_failure or not ok
                self._record_item(job_id, dict(
                    seq=seq, object_type=step.object_type, object_name=step.object_name,
                    previous_charset=step.previous_charset,
                    previous_collation=step.previous_collation,
                    sql=step.sql, status=COLLATION_ITEM_OK if ok else COLLATION_ITEM_ERROR,
                    error=err, execution_ms=ms, executed_at=_utcnow(),
                ))
                seq += 1
            if had_failure:
                # El ALTER DATABASE es el único paso cuyo fallo SÍ corta: los objetos que se
                # recrearían después quedarían asociados a un default que no cambió, o sea el
                # problema que la operación viene a resolver. Mejor no tocar nada más.
                self._finish(
                    job_id, ctx, failed=True, seq=seq, progress=progress,
                    error=(
                        "Falló el ALTER DATABASE; no se continuó con las tablas ni los "
                        "objetos para no dejar la conversión a mitad. Ver los ítems."
                    ),
                )
                return

        # ---------------- Fase 2: tablas ------------------------------------------ #
        table_steps = [
            s for s in plan.steps
            if s.object_type == COLLATION_OBJ_TABLE
            and s.action in (*_CONVERT_ACTIONS, "skip")
        ]
        if table_steps:
            self._set_status(job_id, COLLATION_STATUS_RUNNING, phase=COLLATION_PHASE_TABLES)
            bump(phase=COLLATION_PHASE_TABLES)
            done = 0
            for step in table_steps:
                if cancel():
                    self._set_progress(job_id, dict(progress))
                    self._set_status(
                        job_id, COLLATION_STATUS_CANCELED, phase=COLLATION_PHASE_TABLES,
                        finished=True,
                    )
                    return
                if step.action == "skip":
                    self._record_item(job_id, dict(
                        seq=seq, object_type=step.object_type, object_name=step.object_name,
                        previous_charset=step.previous_charset,
                        previous_collation=step.previous_collation,
                        status=COLLATION_ITEM_SKIPPED, error=step.reason,
                        executed_at=_utcnow(),
                    ))
                    seq += 1
                    continue
                ok, err, ms = run_one(step.sql)
                had_failure = had_failure or not ok
                self._record_item(job_id, dict(
                    seq=seq, object_type=step.object_type, object_name=step.object_name,
                    previous_charset=step.previous_charset,
                    previous_collation=step.previous_collation,
                    sql=step.sql, status=COLLATION_ITEM_OK if ok else COLLATION_ITEM_ERROR,
                    error=err, execution_ms=ms, executed_at=_utcnow(),
                    columns_affected=len(step.columns) or None,
                ))
                seq += 1
                done += 1
                bump(tables_done=done)

        # ---------------- Fase 3: objetos con collation congelada ----------------- #
        obj_steps = [s for s in plan.steps if s.action == "recreate"]
        if obj_steps:
            self._set_status(job_id, COLLATION_STATUS_RUNNING, phase=COLLATION_PHASE_OBJECTS)
            bump(phase=COLLATION_PHASE_OBJECTS)
            done = 0
            for step in obj_steps:
                if cancel():
                    self._set_progress(job_id, dict(progress))
                    self._set_status(
                        job_id, COLLATION_STATUS_CANCELED, phase=COLLATION_PHASE_OBJECTS,
                        finished=True,
                    )
                    return
                failed = self._recreate_object(
                    job_id, runner, adapter, target, ctx, step, seq, session_sql,
                    engine, lock_key,
                )
                had_failure = had_failure or failed
                seq += 1
                done += 1
                bump(objects_done=done)

        # Lo seleccionado que ya NO existe al momento de ejecutar (lo borraron entre el
        # preview y el worker). Se deja rastro como 'skipped' en vez de desaparecer en
        # silencio, pero NO cuenta como fallo: no hay nada roto que arreglar.
        for name in plan.missing_tables:
            self._record_item(job_id, dict(
                seq=seq, object_type=COLLATION_OBJ_TABLE, object_name=name,
                status=COLLATION_ITEM_SKIPPED, executed_at=_utcnow(),
                error="La tabla ya no existe en la base de datos; se omitió.",
            ))
            seq += 1
        for ref in plan.missing:
            self._record_item(job_id, dict(
                seq=seq, object_type=ref["object_type"], object_name=ref["name"],
                status=COLLATION_ITEM_SKIPPED, executed_at=_utcnow(),
                error="El objeto ya no existe en la base de datos; se omitió.",
            ))
            seq += 1

        self._finish(job_id, ctx, failed=had_failure, seq=seq, progress=progress)

    def _recreate_object(
        self, job_id, runner, adapter, target, ctx, step: _Step, seq: int,
        session_sql: str, engine, lock_key,
    ) -> bool:
        """
        DROP+CREATE de UN objeto con la collation objetivo. Devuelve True si falló.

        Orden y por qué: (1) capturar el DDL, (2) capturar los grants de rutina, (3)
        PERSISTIR el ítem con el DDL capturado ANTES de tocar el motor, (4) DROP+CREATE en
        una sola conexión precedidos por ``SET NAMES`` objetivo, (5) reaplicar los grants.

        El paso (3) no es cosmético: MySQL/MariaDB NO tienen DDL transaccional, así que si el
        ``CREATE`` falla después de un ``DROP`` exitoso el objeto DESAPARECIÓ del motor y la
        columna ``captured_ddl`` del ítem es la única copia con la que el operador puede
        recrearlo. Persistirla antes de ejecutar es lo que hace ese fallo recuperable.
        """
        otype = step.object_type
        oname = step.object_name
        dialect = ctx["engine"]
        db_name = ctx["database"]
        base = dict(
            seq=seq, object_type=otype, object_name=oname,
            previous_collation=step.previous_collation,
        )

        # (1) DDL exacto. Se captura AHORA (no en el preview): el cuerpo pudo cambiar y
        # recrear una versión vieja perdería ese cambio.
        try:
            ddl = adapter.capture_object_ddl(db_name, otype, oname)
        except AppHttpException as exc:
            self._record_item(job_id, dict(
                **base, status=COLLATION_ITEM_ERROR, executed_at=_utcnow(),
                error=f"No se pudo capturar el DDL del objeto: {exc.message}",
            ))
            return True
        except Exception:  # el detalle va al log con traceback; acá solo se reporta el ítem
            logger.warning(
                "Conversión %s: fallo al capturar el DDL de %s %s", job_id, otype, oname,
                exc_info=True,
            )
            self._record_item(job_id, dict(
                **base, status=COLLATION_ITEM_ERROR, executed_at=_utcnow(),
                error="No se pudo capturar el DDL del objeto (ver logs del gateway).",
            ))
            return True

        # (2) Grants de rutina. FAIL-CLOSED: si no se pueden leer, NO se dropea. El motor
        # los borra con el DROP, así que dropear a ciegas destruiría privilegios sin forma de
        # restaurarlos — peor que no convertir el objeto.
        grants = []
        if otype in _GRANTED_OBJECT_TYPES:
            try:
                grants = adapter.routine_grants(db_name, otype.upper(), oname)
            except Exception:  # el detalle va al log con traceback; acá solo se reporta el ítem
                logger.warning(
                    "Conversión %s: no se pudieron leer los grants de %s %s", job_id,
                    otype, oname, exc_info=True,
                )
                self._record_item(job_id, dict(
                    **base, status=COLLATION_ITEM_SKIPPED, captured_ddl=ddl,
                    executed_at=_utcnow(),
                    grants_error=(
                        "No se pudieron leer los privilegios de la rutina (mysql.procs_priv "
                        "ilegible y el fallback por SHOW GRANTS también falló). NO se "
                        "dropeó: el motor borra esos privilegios con el DROP y recrearla sin "
                        "poder restaurarlos dejaría la rutina sin permisos. Otorgá SELECT "
                        "sobre mysql.procs_priv a la credencial del gateway y reintentá."
                    ),
                    error="Objeto no recreado: privilegios de rutina ilegibles (fail-closed).",
                ))
                return True

        # (3) Persistir el DDL capturado ANTES de ejecutar (copia de recuperación).
        item_id = self._record_item_returning(job_id, dict(
            **base, captured_ddl=ddl, sql=ddl,
            grants_captured=len(grants) if otype in _GRANTED_OBJECT_TYPES else None,
        ))

        # (4) SET NAMES objetivo + DROP + CREATE, en la MISMA conexión y cortando al primer
        # fallo (si el SET NAMES falla NO se recrea nada: recrear con la collation vieja
        # volvería a congelar justo lo que se quiere cambiar).
        drop_sql = (
            f"DROP {_DROP_KEYWORDS[otype]} IF EXISTS "
            f"{quote_identifier(validate_identifier(db_name, dialect, 'base de datos', allow_existing=True), dialect)}."
            f"{quote_identifier(validate_identifier(oname, dialect, otype, allow_existing=True), dialect)}"
        )
        t0 = time.monotonic()
        results = runner.execute_adhoc(
            target, db_name=db_name, engine=engine, lock_key=lock_key,
            statements=[session_sql, drop_sql, ddl],
            already_locked=True, stop_on_error=True,
        )
        ms = int((time.monotonic() - t0) * 1000)
        by_index = {r.index: r for r in results}
        failure_at = None
        for i in range(3):
            r = by_index.get(i)
            if r is None or r.status != "applied":
                failure_at = (i, r.error if r else "no ejecutada")
                break

        if failure_at is not None:
            idx, err = failure_at
            stage = {0: "SET NAMES", 1: "DROP", 2: "CREATE"}[idx]
            note = ""
            if idx == 2:
                # El caso crítico: el DROP pasó y el CREATE no → el objeto ya no existe.
                note = (
                    " ATENCIÓN: el DROP se aplicó y el CREATE no, así que el objeto NO existe "
                    "en la base de datos. El DDL original está guardado en 'captured_ddl' de "
                    "este ítem: usalo para recrearlo a mano tras corregir la causa."
                )
            self._update_item(item_id, dict(
                status=COLLATION_ITEM_ERROR, execution_ms=ms, executed_at=_utcnow(),
                error=f"Falló en {stage}: {err}{note}",
            ))
            return True

        # (5) Reaplicar los grants de rutina. Un fallo acá NO se silencia: el objeto existe
        # pero perdió permisos, así que el ítem queda en error para que el operador lo vea.
        reapplied = None
        grants_error = None
        if grants:
            try:
                reapplied = adapter.apply_routine_grants(db_name, grants)
            except Exception:  # el detalle va al log con traceback; acá solo se reporta el ítem
                logger.warning(
                    "Conversión %s: no se pudieron reaplicar los grants de %s %s", job_id,
                    otype, oname, exc_info=True,
                )
                grants_error = (
                    f"La rutina SE RECREÓ correctamente, pero no se pudieron reaplicar sus "
                    f"{len(grants)} privilegio(s): quedó sin permisos para quien la usaba. "
                    "Reaplicalos a mano (ver logs del gateway para el detalle del motor)."
                )
        self._update_item(item_id, dict(
            status=COLLATION_ITEM_ERROR if grants_error else COLLATION_ITEM_OK,
            execution_ms=ms, executed_at=_utcnow(),
            grants_reapplied=reapplied, grants_error=grants_error,
            error="Objeto recreado, privilegios de rutina NO reaplicados." if grants_error else None,
        ))
        return grants_error is not None

    def _record_item_returning(self, job_id: int, row: dict) -> int:
        session = self._session()
        try:
            item = CollationConversionJobItem(job_id=job_id, **row)
            session.add(item)
            session.commit()
            session.refresh(item)
            return item.id
        finally:
            session.close()

    def _update_item(self, item_id: int, values: dict) -> None:
        session = self._session()
        try:
            item = session.get(CollationConversionJobItem, item_id)
            if item is None:
                return
            for k, v in values.items():
                setattr(item, k, v)
            session.commit()
        finally:
            session.close()

    def _finish(
        self, job_id: int, ctx: dict, *, failed: bool, seq: int, progress: dict,
        error: str | None = None,
    ) -> None:
        self._set_progress(job_id, dict(progress))
        self._set_status(
            job_id,
            COLLATION_STATUS_FAILED if failed else COLLATION_STATUS_SUCCEEDED,
            phase=COLLATION_PHASE_DONE,
            finished=True,
            error=(
                error or ("Al menos un paso falló; ver los ítems." if failed else None)
            ),
        )
        # Cuarentena de la BD gestionada ante fallo (consistente con el flujo apply/clon):
        # la protege del próximo execute hasta que un admin la revise (force).
        if failed and ctx["managed_id"] is not None:
            self._quarantine(ctx["managed_id"])
        # Auditoría AGREGADA del resultado (una entrada, patrón apply_profile_bulk/apply_all).
        # El worker corre fuera del ciclo de request: sin Request ID/admin; la intención ya
        # quedó registrada con record_intent al encolar.
        audit.record(
            "collation_conversion.execute",
            status="error" if failed else "success",
            target_type="managed_database" if ctx["managed_id"] is not None else "server_database",
            target_id=ctx["managed_id"],
            server_id=ctx["server_id"],
            touched_engine=True,
            detail=(
                f"conversión {job_id}: {ctx['db_ref']} → {ctx['charset']}/{ctx['collation']} "
                f"({seq} paso(s))"
            ),
        )
