"""Schemas Pydantic del recurso ModelMigration (migraciones de un blueprint)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Versión: solo dígitos (4–10). Se compara/ordena NUMÉRICAMENTE (no lexicográfico).
_VERSION = r"^\d{4,10}$"
# Cota de tamaño del SQL de una migración (256 KB). Falla temprano con 422 en vez de
# depender solo del límite global de tamaño de request (RequestSizeMiddleware).
_MAX_SQL = 262_144


class ModelMigrationCreate(BaseModel):
    version: str | None = Field(
        None,
        pattern=_VERSION,
        description=(
            "Opcional: si se omite, el gateway asigna automáticamente la SIGUIENTE versión "
            "secuencial (max+1). Pásala solo si quieres fijarla manualmente (0001, 0002…)."
        ),
    )
    name: str = Field(..., min_length=1, max_length=200)
    up_sql: str = Field(..., min_length=1, max_length=_MAX_SQL, description="Delta SQL base (estilo MySQL de referencia)")
    up_sql_mysql: str | None = Field(None, max_length=_MAX_SQL, description="Override manual MySQL/MariaDB (opcional)")
    up_sql_postgresql: str | None = Field(None, max_length=_MAX_SQL, description="Override manual PostgreSQL (opcional)")
    down_sql: str | None = Field(
        None, max_length=_MAX_SQL,
        description="Rollback confirmado (opcional). Si se omite, se sugiere uno auto-generado.",
    )
    capture_selects: bool = Field(
        False,
        description=(
            "OPT-IN: guardar el RESULTADO de las sentencias de lectura de esta versión "
            "cuando se aplique/revierta (GET .../migrations/{version}/select-results). "
            "Es la única vía por la que el gateway persiste datos de negocio, así que la "
            "versión nace SIN revisar (reviewed=false → apply da 409 hasta aprobarla con "
            "PATCH reviewed=true) y cada apply exige además allow_result_capture=true. "
            "Los MISMOS dos controles rigen para el rollback: el down_sql también captura."
        ),
    )


class ModelMigrationPatch(BaseModel):
    """Confirma el rollback o añade overrides DESPUÉS de crear la migración."""

    name: str | None = Field(None, min_length=1, max_length=200)
    up_sql: str | None = Field(
        None,
        min_length=1,
        max_length=_MAX_SQL,
        description=(
            "Corrige el SQL base del delta (dialecto de referencia: MySQL). SOLO permitido "
            "si la migración NO se ha aplicado en ninguna BD (409 si ya se aplicó → usa "
            "fix-forward). Al cambiarlo se regenera el rollback sugerido y el checksum."
        ),
    )
    down_sql: str | None = Field(None, max_length=_MAX_SQL, description="Confirma el rollback de esta versión")
    up_sql_mysql: str | None = Field(None, max_length=_MAX_SQL, description="Añade/actualiza override MySQL")
    up_sql_postgresql: str | None = Field(None, max_length=_MAX_SQL, description="Añade/actualiza override PostgreSQL")
    reviewed: bool | None = Field(
        None,
        description=(
            "Aprueba (true) un baseline de snapshot tras revisar su DDL — habilita su apply "
            "(R1). En una versión con 'capture_selects=true' la aprobación es de una "
            "CONSULTA CONCRETA: si en la misma llamada (o en una posterior) cambia el SQL "
            "('up_sql', overrides o 'down_sql'), se vuelve a poner en false automáticamente."
        ),
    )
    capture_selects: bool | None = Field(
        None,
        description=(
            "Activa/desactiva la captura de resultados de los SELECT de esta versión. "
            "ACTIVARLA vuelve a poner reviewed=false (hay que re-aprobar la versión "
            "sabiendo que va a extraer datos), y con la captura activa cualquier cambio de "
            "SQL también revoca la aprobación; desactivarla no purga lo ya capturado — "
            "para eso está DELETE .../migrations/{version}/select-results."
        ),
    )


class ModelMigrationSummary(BaseModel):
    """Item compacto para listados (no incluye el SQL completo ni traducciones)."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    model_id: int
    version: str
    name: str
    has_mysql_override: bool
    has_postgresql_override: bool
    has_rollback: bool
    checksum: str
    kind: str = "schema"
    is_baseline: bool = False
    reviewed: bool = True
    capture_selects: bool = False
    created_at: datetime


class ModelMigrationOut(BaseModel):
    """Detalle completo: incluye SQL, overrides, rollback y traducciones calculadas."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    model_id: int
    version: str
    name: str
    up_sql: str
    up_sql_mysql: str | None = None
    up_sql_postgresql: str | None = None
    down_sql: str | None = None
    down_sql_suggested: str | None = None
    translated: dict[str, str] = Field(
        default_factory=dict, description="up_sql traducido por motor (mysql, postgresql)"
    )
    checksum: str
    kind: str = Field(
        "schema", description="'schema' (DDL) | 'data' (datos-semilla upsert, atado a source_engine)"
    )
    source_engine: str | None = Field(
        None, description="Motor de origen si proviene de un snapshot; None = portable (Plan 09)"
    )
    is_baseline: bool = False
    has_non_portable: bool = Field(
        False, description="True si incluye objetos procedurales no traducibles cross-engine"
    )
    reviewed: bool = Field(
        True, description="False = baseline de snapshot pendiente de revisión; no aplicable hasta aprobarse (R1)"
    )
    capture_selects: bool = Field(
        False,
        description=(
            "True = los SELECT de esta versión guardan su resultado (cifrado) al aplicarse "
            "O al revertirse (el down_sql captura igual que el up_sql). Requiere "
            "reviewed=true y allow_result_capture=true en cada apply y en cada rollback."
        ),
    )
    created_at: datetime
    updated_at: datetime


class PartialApplicationOut(BaseModel):
    """Una migración que quedó APLICADA A MEDIAS en esta BD."""

    version: str | None = None
    model_migration_id: int
    applied_statements: int = Field(
        0, description="Sentencias que ya commitearon con éxito (el DDL va en AUTOCOMMIT)."
    )
    total_statements: int = Field(0, description="Sentencias totales de la migración.")
    reconcilable: bool = Field(
        False,
        description=(
            "True si el gateway puede deshacer automáticamente lo aplicado "
            "(POST /migrations/reconcile-partial). Requiere manifiesto de sentencias."
        ),
    )
    reason: str | None = Field(
        None, description="Por qué NO es reconciliable automáticamente, si aplica."
    )
    statements_to_undo: int = Field(0, description="Reversos que se ejecutarían.")


class MigrationStatusOut(BaseModel):
    """Estado de una BD gestionada frente a las migraciones de su blueprint."""

    managed_database_id: int
    model_id: int | None = None
    slug: str | None = None
    current_version: str | None = None
    latest_available: str | None = None
    pending_count: int
    pending_versions: list[str]
    has_partial_application: bool = Field(
        False,
        description=(
            "True si alguna migración quedó aplicada a medias. OJO: 'current_version' NO "
            "lo refleja — Alembic solo registra la versión cuando el upgrade TERMINA, así "
            "que el estado se lee como sano aunque la BD tenga cambios físicos de una "
            "versión que el ledger no conoce. Con esto en true, 'rollback' responde 409."
        ),
    )
    partial_application: list[PartialApplicationOut] = Field(default_factory=list)


class MigrationReconcileStatementOut(BaseModel):
    seq: int
    status: str | None = None  # applied | failed (ausente en dry_run)
    sql: str | None = None  # solo en dry_run
    error: str | None = None
    execution_ms: int | None = None


class MigrationReconcilePartialOut(BaseModel):
    """
    Resultado de reconciliar una aplicación PARCIAL: deshacer las sentencias que sí
    corrieron de una migración que falló a mitad.

    NO toca la tabla de versión de Alembic: la versión parcial nunca se registró, así que
    esto no es un ``downgrade`` sino una compensación que devuelve el plano físico al
    estado que el ledger ya afirma.
    """

    managed_database_id: int
    database_name: str
    server_id: int
    version: str
    applied_statements: int
    total_statements: int
    statements_to_undo: int
    unreversible_statements: list[dict] = Field(
        default_factory=list,
        description="Sentencias aplicadas SIN reverso: quedan en la BD (solo con force=true).",
    )
    unconfirmed_reverses: list[dict] = Field(
        default_factory=list,
        description=(
            "Reversos que existen pero NO son demostrablemente seguros: pueden fallar (una "
            "UNIQUE/CHECK/FK que se re-crea valida los datos actuales) o no restaurar los "
            "datos (recrear una tabla borrada devuelve la estructura, no las filas). "
            "Revisalos en el dry-run: no bloquean, son el mejor reverso disponible."
        ),
    )
    dry_run: bool = False
    undone_count: int = 0
    failed: bool = False
    fully_reconciled: bool = Field(
        False, description="True si la BD volvió a coincidir con su versión registrada."
    )
    remaining_applied_statements: int = Field(
        0, description="Sentencias que siguen aplicadas si la reconciliación no terminó."
    )
    statements: list[MigrationReconcileStatementOut] = Field(default_factory=list)
    results: list[MigrationReconcileStatementOut] = Field(default_factory=list)


class MigrationResultOut(BaseModel):
    """Resultado de aplicar/revertir una migración sobre una BD."""

    migration_id: int
    version: str
    status: str  # applied | failed
    error: str | None = None
    execution_ms: int
    resumed: bool = Field(
        False, description="True si este intento retomó desde un checkpoint parcial previo"
    )
    resumed_from_statement: int | None = Field(
        None, description="Sentencia (1-based) desde la que se retomó, si resumed=true"
    )
    statement_total: int | None = Field(
        None, description="Cantidad total de sentencias de esta migración/dirección"
    )
    failed_at_statement_index: int | None = Field(
        None,
        description=(
            "Sentencia (1-based) en la que falló, si status=failed. Null si no se pudo "
            "determinar (migración no resumible) o si status=applied."
        ),
    )


class MigrationAutoReconcileOut(BaseModel):
    """
    Auto-reconciliación ejecutada por el sistema tras un fallo a mitad de una migración.

    Existe para que un `apply` fallido no deje la BD en un estado que el admin tenga que
    entender y desarmar a mano. Se deshacen EXACTAMENTE las sentencias que alcanzaron a
    commitear, en orden inverso, y la BD vuelve a su versión anterior.
    """

    version: str = Field(..., description="Versión que falló y se deshizo")
    attempted: bool = True
    undone_count: int = 0
    statements_to_undo: int = 0
    fully_reconciled: bool = Field(
        False, description="True si la BD volvió a coincidir con su versión registrada"
    )
    unconfirmed_reverses: list[dict] = Field(
        default_factory=list,
        description="Reversos ejecutados que NO son demostrablemente seguros (revisar)",
    )
    unreversible_statements: list[dict] = Field(
        default_factory=list,
        description="Sentencias aplicadas sin reverso: siguen en la BD",
    )
    error: str | None = None


class MigrationApplyOut(BaseModel):
    """
    Resultado de `POST .../migrations/apply` (cubre apply real y dry-run).

    Una sola llamada aplica TODAS las pendientes en orden hasta `target_version`
    (o hasta la última si se omite). `from_version`→`to_version` reportan el salto real;
    `no_op=true` cuando no había nada que aplicar (ya al día o versión pedida ≤ actual).
    """

    managed_database_id: int
    database_name: str | None = None
    server_id: int | None = None
    from_version: str | None = Field(None, description="Versión de la BD ANTES de aplicar")
    to_version: str | None = Field(None, description="Versión de la BD DESPUÉS de aplicar")
    target_version: str | None = Field(
        None, description="Versión objetivo solicitada; null = última disponible"
    )
    applied_count: int = 0
    failed: bool = False
    quarantined: bool = False
    no_op: bool = Field(False, description="True si no había migraciones que aplicar")
    dry_run: bool = False
    pending_versions: list[str] = Field(default_factory=list)
    results: list[MigrationResultOut] = Field(default_factory=list)
    captured_select_count: int = Field(
        0,
        description=(
            "Cuántas capturas de resultados de SELECT escribió ESTA corrida (no lo que haya "
            "acumulado en la tabla de corridas anteriores: contarlo así afirmaba escrituras "
            "que no habían ocurrido). Es un PUNTERO, no los datos: las filas nunca viajan acá "
            "(con LOGGER_MIDDLEWARE_SHOW_BODY=true irían al log de aplicación). Se leen —las "
            "de esta corrida y las anteriores— con "
            "GET /managed-databases/{id}/migrations/{version}/select-results."
        ),
    )
    select_results_available: bool = Field(
        False, description="Atajo de captured_select_count > 0 para el frontend."
    )
    reconciliation: MigrationAutoReconcileOut | None = Field(
        None,
        description=(
            "Qué hizo el sistema por su cuenta si una migración falló A MITAD (solo puede "
            "pasar en MySQL/MariaDB: en PostgreSQL el motor deshace la migración solo). "
            "null = no hizo falta. Con 'fully_reconciled=true' la BD volvió limpia a su "
            "versión anterior y NO queda en cuarentena: solo hay que corregir la migración."
        ),
    )


class MigrationRollbackOut(BaseModel):
    """
    Resultado de `POST .../migrations/rollback`. Revierte SECUENCIALMENTE en una sola
    llamada desde `from_version` hasta `to_version` (= `target_version` solicitado, o
    una versión menos si se omitió). `reverted_versions` lista lo deshecho (de la más
    reciente a la más antigua).
    """

    managed_database_id: int
    database_name: str | None = None
    server_id: int | None = None
    from_version: str | None = Field(None, description="Versión ANTES de revertir")
    to_version: str | None = Field(None, description="Versión DESPUÉS de revertir (null = base)")
    target_version: str | None = Field(None, description="Destino solicitado/resuelto")
    reverted_count: int = 0
    failed: bool = False
    quarantined: bool = False
    no_op: bool = False
    reverted_versions: list[str] = Field(default_factory=list)
    results: list[MigrationResultOut] = Field(default_factory=list)
    captured_select_count: int = Field(
        0,
        description=(
            "Capturas de SELECT que escribió ESTE rollback (puntero). 0 es lo normal cuando "
            "el down_sql no tiene lecturas, aunque la versión sí tenga capturas de su apply: "
            "esas se leen igual con GET .../select-results."
        ),
    )
    select_results_available: bool = Field(
        False, description="Atajo de captured_select_count > 0 para el frontend."
    )


class MigrationSelectResultItemOut(BaseModel):
    """
    UNA sentencia de lectura capturada dentro de una migración.

    ``durability='rolled_back'`` (solo posible en PostgreSQL) significa que el motor deshizo
    la transacción de la migración: las filas son diagnóstico de lo que se vio DURANTE el
    intento, no el estado final de la BD.
    """

    statement_index: int = Field(
        ..., description="Índice 1-based de la sentencia (el mismo que usa el checkpoint)"
    )
    direction: str = Field(..., description="'up' (apply) | 'down' (rollback)")
    sql: str = Field(..., description="Sentencia capturada (recortada, sin contraseñas)")
    sql_hash: str
    status: str = Field(..., description="'ok' | 'error' (la sentencia corrió; la captura falló)")
    durability: str = Field(..., description="'committed' | 'rolled_back' | 'unknown'")
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(
        default_factory=list,
        description="Filas como LISTAS (no dicts): una consulta puede repetir nombres de columna.",
    )
    row_count: int = 0
    truncated: bool = Field(
        False, description="True si el resultado real excedía los topes de filas/bytes."
    )
    payload_bytes: int = 0
    error: str | None = None
    captured_at: datetime
    migration_checksum: str


class MigrationSelectResultsOut(BaseModel):
    """Capturas de una versión sobre UNA BD gestionada. Lectura AUDITADA (fail-closed)."""

    managed_database_id: int
    database_name: str | None = None
    server_id: int | None = None
    model_migration_id: int
    version: str
    capture_selects: bool = Field(
        False, description="Si la versión tiene la captura habilitada actualmente."
    )
    stale: bool = Field(
        False,
        description=(
            "True si alguna captura fue tomada con un checksum distinto al actual de la "
            "versión: describe un SQL que ya cambió. (El PATCH que edita el SQL purga estas "
            "filas, así que en la práctica solo se ve con datos previos a ese fix.)"
        ),
    )
    expected_indexes: list[int] = Field(
        default_factory=list,
        description=(
            "Índices de sentencia que HOY serían capturables en el 'up' de esta versión "
            "para el motor de esta BD. Se derivan en el momento de la lectura, no se "
            "persisten."
        ),
    )
    missing_indexes: list[int] = Field(
        default_factory=list,
        description=(
            "De los esperados, los que no tienen captura: típicamente la migración falló "
            "antes de llegar a esa sentencia, o se aplicó cuando la captura estaba apagada."
        ),
    )
    durability_warning: str | None = Field(
        None,
        description="Presente si alguna captura quedó 'rolled_back' (el motor deshizo la migración).",
    )
    items: list[MigrationSelectResultItemOut] = Field(default_factory=list)


class MigrationHistoryOut(BaseModel):
    """Entrada del historial de aplicación de migraciones de una BD gestionada."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    managed_database_id: int
    model_migration_id: int
    version: str | None = None  # versión de la migración (join), si existe
    applied_at: datetime
    status: str
    error: str | None = None
    execution_ms: int | None = None


class ApplyAllItemOut(BaseModel):
    """Resultado del apply masivo para una BD del blueprint (apply o dry-run)."""

    managed_database_id: int
    database_name: str | None = None
    server_id: int | None = None
    ok: bool
    applied: list[MigrationResultOut] = Field(default_factory=list)
    dry_run: bool = False
    pending_versions: list[str] = Field(default_factory=list)
    error: str | None = None


class ApplyAllOut(BaseModel):
    model_id: int
    total_databases: int
    processed: int
    results: list[ApplyAllItemOut]
