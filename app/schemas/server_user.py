"""
Schemas Pydantic del recurso ServerUser (usuario del motor).

Regla de oro: ningún schema de salida expone el password (ni cifrado ni en claro).
Solo se informa ``has_password``.
"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.services.db_admin.dtos import GrantLevel, ObjectRef

# Whitelist alineada con app/services/db_admin/identifiers.py (validación fail-fast).
_USERNAME = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
_HOST = r"^[A-Za-z0-9_.%:\-]{1,255}$"


class ServerUserCreate(BaseModel):
    server_id: int = Field(..., ge=1)
    username: str = Field(..., pattern=_USERNAME)
    host: str = Field("%", pattern=_HOST, description="Solo MySQL/MariaDB; ignorado en PostgreSQL")
    # Requerido solo si se aprovisiona (?provision=true); el controller lo valida.
    password: str | None = Field(None, min_length=1)
    notes: str | None = None
    is_active: bool = True


class ServerUserUpdate(BaseModel):
    # username/host/server_id son inmutables (cambiarlos = drop + create).
    password: str | None = Field(None, min_length=1)
    is_active: bool | None = None
    notes: str | None = None


class AdoptUserIn(BaseModel):
    """
    Adopta un usuario/rol que YA existe en el motor (Plan 09): registra metadata SIN
    ejecutar CREATE USER y SIN password (has_password=false hasta que se rote). El
    gateway verifica que el usuario exista realmente en el motor (404 si no).
    """

    server_id: int = Field(..., ge=1)
    username: str = Field(..., pattern=_USERNAME)
    host: str = Field("%", pattern=_HOST, description="Solo MySQL/MariaDB; ignorado en PostgreSQL")
    notes: str | None = None


class ServerUserOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    username: str
    host: str
    is_active: bool
    notes: str | None = None
    has_password: bool = False
    created_at: datetime
    updated_at: datetime


# ─── Endpoint unificado: crear usuario + grants iniciales ─────────────────── #

class GrantOnCreate(BaseModel):
    """Grant a aplicar justo después de crear/aprovisionar el usuario."""
    level: GrantLevel
    object_ref: ObjectRef
    privileges: list[str] = Field(min_length=1)
    with_grant_option: bool = False


class ServerUserFullCreate(ServerUserCreate):
    """Igual que ServerUserCreate + lista opcional de grants a aplicar al crear."""
    initial_grants: list[GrantOnCreate] = Field(
        default_factory=list,
        description="Permisos a otorgar en el motor justo después de aprovisionar el usuario.",
    )


class GrantApplyResult(BaseModel):
    level: str
    object: str | None = None
    privileges: list[str]
    success: bool
    error: str | None = None


class ServerUserFullOut(BaseModel):
    """Respuesta del endpoint unificado crear+permisos."""
    user: ServerUserOut
    grants_applied: int
    grant_results: list[GrantApplyResult]


# ─── Vista agrupada por username + CRUD por identidad física ────────────────── #
# Estos schemas alimentan el manejo de usuarios "por identidad" (server_id +
# username + host), que funciona tanto para usuarios adoptados como NO adoptados.
# La agrupación por username elimina la redundancia de listar 'user'@'hostA',
# 'user'@'hostB', ... como si fueran usuarios distintos (en MySQL/MariaDB lo son a
# nivel motor, pero visualmente confunde). En PostgreSQL un rol no tiene host:
# ``supports_hosts=false`` y cada username tiene exactamente una identidad.


class EngineUserIdentityOut(BaseModel):
    """Una identidad concreta de un username: en MySQL un ``'user'@'host'``."""

    host: str | None = None  # None en PostgreSQL (el rol no tiene host)
    status: str  # 'adopted' (en inventario) | 'unmanaged' (solo motor) | 'orphan' (solo inventario)
    server_user_id: int | None = None
    has_password: bool = False  # ¿el gateway conoce/guarda su contraseña?
    is_active: bool | None = None
    notes: str | None = None


class GroupedEngineUserOut(BaseModel):
    username: str
    identity_count: int
    identities: list[EngineUserIdentityOut]


class GroupedEngineUsersOut(BaseModel):
    dialect: str
    supports_hosts: bool  # false en PostgreSQL → el frontend oculta host/agregar-host
    users: list[GroupedEngineUserOut]


class EngineUserCreateIn(BaseModel):
    """Crea un usuario directamente en el motor (con adopción opcional al inventario)."""

    username: str = Field(..., pattern=_USERNAME)
    host: str = Field("%", pattern=_HOST, description="Solo MySQL/MariaDB; ignorado en PostgreSQL")
    password: str = Field(..., min_length=1)
    adopt: bool = Field(
        False,
        description="Si true, además registra el usuario en el inventario del gateway (guarda la contraseña cifrada, permitiendo revelarla luego).",
    )
    notes: str | None = None


class EnginePasswordChangeIn(BaseModel):
    """Cambia la contraseña de un usuario en el motor, esté o no adoptado."""

    username: str = Field(..., pattern=_USERNAME)
    host: str = Field("%", pattern=_HOST, description="Solo MySQL/MariaDB; ignorado en PostgreSQL")
    new_password: str = Field(..., min_length=1)
    adopt: bool = Field(
        False,
        description="Solo aplica si el usuario NO está en el inventario: si true, lo adopta guardando la nueva contraseña cifrada.",
    )


class EngineRevealPasswordIn(BaseModel):
    username: str = Field(..., pattern=_USERNAME)
    host: str = Field("%", pattern=_HOST, description="Solo MySQL/MariaDB; ignorado en PostgreSQL")


class RevealedPasswordOut(BaseModel):
    """
    Contraseña en claro de un usuario. SOLO es posible cuando el gateway fijó esa
    contraseña (create/rotación por el gateway) y la guarda cifrada. El motor solo
    almacena un hash irreversible: una contraseña que el gateway nunca conoció NO se
    puede revelar (409), únicamente rotar.
    """

    username: str
    host: str | None = None
    password: str


class EngineUserActionOut(BaseModel):
    """Resultado de una operación por identidad (create / cambio de contraseña / drop)."""

    username: str
    host: str | None = None
    adopted: bool = False  # ¿quedó (o ya estaba) registrado en el inventario?
    server_user_id: int | None = None


class AddHostIn(BaseModel):
    """
    Agrega un HOST adicional a un usuario existente (clona la cuenta a un nuevo host).
    Exclusivo de MySQL/MariaDB: en esos motores ``'user'@'hostA'`` y ``'user'@'hostB'``
    son cuentas separadas. En PostgreSQL el rol no tiene host → 422.
    """

    username: str = Field(..., pattern=_USERNAME)
    source_host: str = Field(
        "%", pattern=_HOST, description="Host de la cuenta origen desde la que se clona."
    )
    new_host: str = Field(..., pattern=_HOST, description="Nuevo host para el que se crea la cuenta.")
    reuse_password: bool = Field(
        True,
        description="True: copia el hash de la cuenta origen (misma contraseña, sin conocerla en claro). False: usa 'new_password'.",
    )
    new_password: str | None = Field(None, min_length=1)
    copy_grants: bool = Field(
        False,
        description="Si true, replica los permisos de la cuenta origen al nuevo host (best-effort; omite privilegios globales/PROXY).",
    )
    adopt: bool = Field(
        False, description="Si true, registra la nueva identidad en el inventario del gateway."
    )
    notes: str | None = None

    @model_validator(mode="after")
    def _require_new_password(self):
        if not self.reuse_password and not self.new_password:
            raise ValueError("Se requiere 'new_password' cuando reuse_password=false.")
        return self


class AddHostOut(BaseModel):
    username: str
    new_host: str
    password_mode: str  # 'reused' (hash copiado) | 'new'
    grants_copied: int = 0
    grants_error: str | None = None
    adopted: bool = False
    server_user_id: int | None = None


# ─── Adopción masiva de hosts + contraseña con alcance individual/global ────── #
# Estos schemas alimentan tres operaciones que orquestan sobre TODOS los hosts en
# vivo de un username (no una tabla nueva: server_users sigue siendo una fila por
# identidad física; ver docs/features/engine-users-management.md).


class AdoptAllHostsIn(BaseModel):
    """Adopta TODOS los hosts en vivo de un username en una sola operación."""

    username: str = Field(..., pattern=_USERNAME)
    known_password: str | None = Field(
        None,
        min_length=1,
        description=(
            "Si se provee, se guarda cifrada en TODAS las identidades adoptadas. "
            "NUNCA ejecuta ALTER USER (no confirma que sea la contraseña real del motor)."
        ),
    )
    notes: str | None = None


class AdoptAllHostsItemOut(BaseModel):
    host: str | None = None  # None en PostgreSQL
    status: str  # 'adopted' | 'already_adopted'
    server_user_id: int | None = None


class BatchAdoptOut(BaseModel):
    username: str
    dialect: str
    total_hosts: int
    adopted: int
    results: list[AdoptAllHostsItemOut]


class DefineKnownPasswordIn(BaseModel):
    """
    Registra una contraseña YA CONOCIDA por el admin humano (coincide con la real del
    motor, fijada fuera del gateway o de otra forma) SIN ejecutar ALTER USER/ROLE. Solo
    cifra y guarda, para habilitar reveal-password. Distinto de
    EnginePasswordChangeIn/EnginePasswordChangeAllHostsIn, que sí rotan en el motor.
    """

    username: str = Field(..., pattern=_USERNAME)
    scope: Literal["host", "all_hosts"] = Field(
        "host",
        description=(
            "'host': aplica solo a 'host'. 'all_hosts': aplica a TODOS los hosts en "
            "vivo del username (se ignora 'host')."
        ),
    )
    host: str = Field(
        "%", pattern=_HOST, description="Solo si scope='host'; ignorado si scope='all_hosts'."
    )
    known_password: str = Field(..., min_length=1)
    adopt_if_missing: bool = Field(
        False,
        description="Si true, crea la fila de inventario para hosts en vivo sin fila previa.",
    )
    overwrite: bool = Field(
        False,
        description=(
            "Obligatorio en true para sobrescribir una identidad que YA tiene una "
            "contraseña guardada por el gateway (evita reemplazos accidentales de un "
            "valor que ya era revelable correctamente)."
        ),
    )


class KnownPasswordSetItemOut(BaseModel):
    host: str | None = None
    status: str  # 'updated' | 'adopted' | 'skipped_not_found' | 'conflict_needs_overwrite'
    server_user_id: int | None = None


class KnownPasswordSetOut(BaseModel):
    username: str
    scope: str
    total_hosts: int
    updated: int
    results: list[KnownPasswordSetItemOut]


class EnginePasswordChangeAllHostsIn(BaseModel):
    """
    Rota la contraseña REAL (ALTER USER/ROLE) en TODOS los hosts en vivo de un
    username. Irreversible sobre N cuentas reales a la vez: exige 'confirm_username'
    (mismo patrón de doble intención que DROP USER).
    """

    username: str = Field(..., pattern=_USERNAME)
    new_password: str = Field(..., min_length=1)
    confirm_username: str = Field(
        ..., description="Debe coincidir exactamente con 'username' para confirmar la rotación masiva."
    )
    adopt_if_missing: bool = Field(
        False,
        description="Si true, registra en el inventario los hosts sin fila previa tras rotarlos.",
    )

    @model_validator(mode="after")
    def _require_confirmation(self):
        if self.confirm_username != self.username:
            raise ValueError("'confirm_username' debe coincidir exactamente con 'username'.")
        return self


class PasswordChangeItemOut(BaseModel):
    host: str | None = None
    status: str  # 'rotated' | 'error'
    server_user_id: int | None = None
    adopted: bool = False
    error: str | None = None


class PasswordChangeBatchOut(BaseModel):
    username: str
    total_hosts: int
    updated: int
    results: list[PasswordChangeItemOut]
