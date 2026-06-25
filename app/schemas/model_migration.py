"""Schemas Pydantic del recurso ModelMigration (migraciones de un blueprint)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Versión: solo dígitos, mínimo 4 (padding). Orden lexicográfico = cronológico.
_VERSION = r"^\d{4,10}$"


class ModelMigrationCreate(BaseModel):
    version: str = Field(..., pattern=_VERSION, description="Secuencial con padding: 0001, 0002…")
    name: str = Field(..., min_length=1, max_length=200)
    up_sql: str = Field(..., min_length=1, description="Delta SQL base (estilo MySQL de referencia)")
    up_sql_mysql: str | None = Field(None, description="Override manual MySQL/MariaDB (opcional)")
    up_sql_postgresql: str | None = Field(None, description="Override manual PostgreSQL (opcional)")
    down_sql: str | None = Field(
        None,
        description="Rollback confirmado (opcional). Si se omite, se sugiere uno auto-generado.",
    )


class ModelMigrationPatch(BaseModel):
    """Confirma el rollback o añade overrides DESPUÉS de crear la migración."""

    name: str | None = Field(None, min_length=1, max_length=200)
    down_sql: str | None = Field(None, description="Confirma el rollback de esta versión")
    up_sql_mysql: str | None = Field(None, description="Añade/actualiza override MySQL")
    up_sql_postgresql: str | None = Field(None, description="Añade/actualiza override PostgreSQL")


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
    """Resultado del apply masivo para una BD del blueprint."""

    managed_database_id: int
    database_name: str
    server_id: int
    ok: bool
    applied: list[MigrationResultOut] = Field(default_factory=list)
    error: str | None = None


class ApplyAllOut(BaseModel):
    model_id: int
    total_databases: int
    processed: int
    results: list[ApplyAllItemOut]
