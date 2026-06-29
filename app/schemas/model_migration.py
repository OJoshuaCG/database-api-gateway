"""Schemas Pydantic del recurso ModelMigration (migraciones de un blueprint)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Versión: solo dígitos (4–10). Se compara/ordena NUMÉRICAMENTE (no lexicográfico).
_VERSION = r"^\d{4,10}$"
# Cota de tamaño del SQL de una migración (256 KB). Falla temprano con 422 en vez de
# depender solo del límite global de tamaño de request (RequestSizeMiddleware).
_MAX_SQL = 262_144


class ModelMigrationCreate(BaseModel):
    version: str = Field(..., pattern=_VERSION, description="Secuencial con padding: 0001, 0002…")
    name: str = Field(..., min_length=1, max_length=200)
    up_sql: str = Field(..., min_length=1, max_length=_MAX_SQL, description="Delta SQL base (estilo MySQL de referencia)")
    up_sql_mysql: str | None = Field(None, max_length=_MAX_SQL, description="Override manual MySQL/MariaDB (opcional)")
    up_sql_postgresql: str | None = Field(None, max_length=_MAX_SQL, description="Override manual PostgreSQL (opcional)")
    down_sql: str | None = Field(
        None, max_length=_MAX_SQL,
        description="Rollback confirmado (opcional). Si se omite, se sugiere uno auto-generado.",
    )


class ModelMigrationPatch(BaseModel):
    """Confirma el rollback o añade overrides DESPUÉS de crear la migración."""

    name: str | None = Field(None, min_length=1, max_length=200)
    down_sql: str | None = Field(None, max_length=_MAX_SQL, description="Confirma el rollback de esta versión")
    up_sql_mysql: str | None = Field(None, max_length=_MAX_SQL, description="Añade/actualiza override MySQL")
    up_sql_postgresql: str | None = Field(None, max_length=_MAX_SQL, description="Añade/actualiza override PostgreSQL")


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
    source_engine: str | None = Field(
        None, description="Motor de origen si proviene de un snapshot; None = portable (Plan 09)"
    )
    is_baseline: bool = False
    has_non_portable: bool = Field(
        False, description="True si incluye objetos procedurales no traducibles cross-engine"
    )
    created_at: datetime
    updated_at: datetime


class MigrationStatusOut(BaseModel):
    """Estado de una BD gestionada frente a las migraciones de su blueprint."""

    managed_database_id: int
    model_id: int | None = None
    slug: str | None = None
    current_version: str | None = None
    latest_available: str | None = None
    pending_count: int
    pending_versions: list[str]


class MigrationResultOut(BaseModel):
    """Resultado de aplicar/revertir una migración sobre una BD."""

    migration_id: int
    version: str
    status: str  # applied | failed
    error: str | None = None
    execution_ms: int


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
