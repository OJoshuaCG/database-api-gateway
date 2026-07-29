"""
Schemas de gestión de bases de datos a NIVEL SERVIDOR (crear / borrar / ver usuarios),
independientes del inventario (funciona con BDs adoptadas y crudas).
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field


class DatabaseCreateIn(BaseModel):
    # ``register`` colisiona con un atributo interno de ``BaseModel``: se expone como alias
    # de la API (el frontend sigue enviando ``register``) sobre el campo ``register_inventory``.
    model_config = ConfigDict(populate_by_name=True)

    name: str = Field(..., min_length=1, max_length=64, description="Nombre de la BD a crear")
    charset: str | None = Field(
        default=None, description="MySQL/MariaDB: CHARACTER SET. PostgreSQL: ENCODING."
    )
    collation: str | None = Field(
        default=None, description="MySQL/MariaDB: COLLATE. PostgreSQL: LOCALE (LC_COLLATE/LC_CTYPE)."
    )
    owner: str | None = Field(
        default=None, description="Dueño a nivel motor (PostgreSQL OWNER; ignorado en MySQL/MariaDB)."
    )
    register_inventory: bool = Field(
        default=False,
        alias="register",
        description="Si True, además registra la BD en el inventario (requiere owner_id).",
    )
    owner_id: int | None = Field(
        default=None, description="ServerUser propietario del registro; obligatorio si register=True."
    )
    notes: str | None = None


class DatabaseCreateOut(BaseModel):
    database: str
    engine: str
    registered: bool
    managed_database_id: int | None = None


class DropPreviewOut(BaseModel):
    database: str
    engine: str
    active_connections: int
    is_managed: bool
    managed_database_id: int | None = None
    confirm_token: str
    expires_at: datetime
    warnings: list[str] = Field(default_factory=list)


class DatabaseDropIn(BaseModel):
    confirm_target_name: str = Field(..., description="Debe coincidir EXACTO con el nombre de la BD.")
    confirm_token: str = Field(..., description="Token del preview (firmado, TTL 2 min).")
    force_disconnect: bool = Field(
        default=False,
        description="PostgreSQL: termina conexiones activas antes del DROP. MySQL: no-op.",
    )


class DatabaseDropOut(BaseModel):
    database: str
    engine: str
    dropped: bool
    inventory_removed: bool
    terminated_connections: int = 0


class DatabaseGranteeOut(BaseModel):
    username: str
    host: str | None = None
    is_global: bool = False
    privileges: list[str] = Field(default_factory=list)
    levels: list[str] = Field(default_factory=list)
    status: str  # adopted | unmanaged
    server_user_id: int | None = None


class DatabaseGranteesOut(BaseModel):
    dialect: str
    supports_hosts: bool
    database: str
    grantees: list[DatabaseGranteeOut]
