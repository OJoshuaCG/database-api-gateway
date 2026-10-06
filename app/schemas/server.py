"""
Schemas Pydantic del recurso Server.

Regla de oro: NINGÚN schema de salida (`ServerOut`) expone la credencial
pseudo-root (ni cifrada ni descifrada). Solo se informa `has_root_password`.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import EngineType, ServerStatus

# Modos TLS válidos hacia el motor destino. None/"" => sin TLS (se omite el paso).
_SSL_MODES = {"disable", "allow", "prefer", "require", "verify-ca", "verify-full"}


def _normalize_ssl_mode(value: str | None) -> str | None:
    """None o vacío => None (sin TLS). En otro caso debe ser un modo válido."""
    if value is None:
        return None
    v = value.strip().lower()
    if v == "":
        return None
    if v not in _SSL_MODES:
        raise ValueError(f"ssl_mode inválido. Use uno de: {', '.join(sorted(_SSL_MODES))}")
    return v


class ServerCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=100)
    host: str = Field(..., min_length=1, max_length=255)
    port: int = Field(..., ge=1, le=65535)
    engine: EngineType
    root_username: str = Field(..., min_length=1, max_length=128)
    # Entra en texto plano; el controller lo cifra antes de persistir.
    root_password: str = Field(..., min_length=1)
    # TLS por conexión: si se especifica, se usa; si no, se omite. Opcional.
    ssl_mode: str | None = Field(None, description="disable|allow|prefer|require|verify-ca|verify-full")
    notes: str | None = None
    is_active: bool = True

    _v_ssl = field_validator("ssl_mode")(staticmethod(_normalize_ssl_mode))


class ServerUpdate(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=100)
    host: str | None = Field(None, min_length=1, max_length=255)
    port: int | None = Field(None, ge=1, le=65535)
    engine: EngineType | None = None
    root_username: str | None = Field(None, min_length=1, max_length=128)
    # Si se provee, se re-cifra; si se omite, no cambia.
    root_password: str | None = Field(None, min_length=1)
    ssl_mode: str | None = Field(None, description="disable|allow|prefer|require|verify-ca|verify-full")
    notes: str | None = None
    is_active: bool | None = None

    _v_ssl = field_validator("ssl_mode")(staticmethod(_normalize_ssl_mode))


class ServerOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    host: str
    port: int
    engine: EngineType
    root_username: str
    ssl_mode: str | None = None
    status: ServerStatus
    is_active: bool
    notes: str | None = None
    has_root_password: bool = False
    # Credencial de SOLO LECTURA del MCP: solo si existe y cuándo se verificó. Ni el usuario ni
    # el cifrado salen nunca (plan 12 §5.2).
    has_readonly_credential: bool = False
    readonly_verified_at: datetime | None = None
    # ``SELECT ON mysql.proc`` server-wide habilitado para la credencial de solo lectura. Es un
    # dato de riesgo, no un secreto: la SPA lo muestra junto al toggle.
    readonly_proc_grant: bool = False
    created_at: datetime
    updated_at: datetime


class ReadonlyCredentialIn(BaseModel):
    """
    Alta o reemplazo de la credencial de solo lectura que usa el MCP para leer el catálogo.

    Reemplazarla **borra la verificación**: una credencial nueva no hereda la observación de la
    anterior, así que hasta que alguien corra ``test-connection?credential=readonly`` el servidor
    queda fuera del MCP.
    """

    model_config = ConfigDict(extra="forbid")

    username: str = Field(..., min_length=1, max_length=128)
    # Entra en texto plano; el controller lo cifra antes de persistir.
    password: str = Field(..., min_length=1)


class ReadonlyProcGrantIn(BaseModel):
    """
    Enciende o apaga ``SELECT ON mysql.proc`` para la credencial de solo lectura (MariaDB < 11.3 /
    MySQL 5.7). Es SERVER-WIDE: expone el código de las rutinas de TODAS las bases del servidor.

    Habilitar exige ``acknowledgement`` igual, carácter por carácter, a
    ``server_catalog.READONLY_PROC_ACK_TEXT``. Deshabilitar no lo necesita: cortar nunca debe
    requerir más fricción que abrir.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool
    acknowledgement: str | None = None


#: Qué pasó con los grants del motor al cambiar la bandera.
ProcGrantEngineState = Literal["converged", "not_alterable", "no_credential"]


class ReadonlyProcGrantOut(BaseModel):
    """
    Resultado del cambio de la bandera. ``engine_grant``:

    - ``converged``: la cuenta propia del gateway se re-aprovisionó (``REVOKE ALL`` y re-grant,
      con ``mysql.proc`` solo si la bandera quedó encendida) y la sonda pasó.
    - ``not_alterable``: la credencial se registró a mano; el gateway NO toca sus grants. Solo se
      re-corrió la sonda. Si el operador no ajustó los grants en el motor, el servidor queda sin
      verificar (``server.readonly_verified_at`` nulo).
    - ``no_credential``: el servidor no tiene credencial de solo lectura; solo cambió la bandera.
    """

    server: ServerOut
    engine_grant: ProcGrantEngineState


# ─── Reconciliación (drift): plano en vivo vs inventario del gateway ───────── #


class ReconcileDatabaseItem(BaseModel):
    """Estado de una BD cruzando el motor en vivo con el inventario del gateway."""

    name: str
    # managed = en motor y en inventario · unmanaged = solo en motor (adoptable)
    # · orphan = solo en inventario (se borró por fuera)
    state: str
    managed_id: int | None = None
    owner_id: int | None = None
    status: str | None = None


class ReconcileUserItem(BaseModel):
    username: str
    host: str | None = None
    state: str
    managed_id: int | None = None


class ReconcileResult(BaseModel):
    server_id: int
    databases: list[ReconcileDatabaseItem]
    users: list[ReconcileUserItem]
