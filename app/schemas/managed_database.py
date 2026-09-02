"""Schemas Pydantic del recurso ManagedDatabase (BD gestionada en un servidor)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.models.enums import ProvisionStatus

# Whitelist alineada con identifiers.py (validación fail-fast en la API).
_DBNAME = r"^[A-Za-z_][A-Za-z0-9_]{0,62}$"
_CHARSET = r"^[A-Za-z0-9_]{1,64}$"
# En PostgreSQL la "collation" de una BD es el LOCALE del SO ('en_US.UTF-8', 'de_DE@euro'):
# lleva puntos, guiones y '@', que ``_CHARSET`` rechaza. Este patrón es solo un filtro
# fail-fast de forma; la autoridad de QUÉ combinación se admite es el catálogo
# ``charset_collation_options`` (ver app/services/charset_catalog.py), que valida la
# creación contra la allowlist habilitada antes de tocar el motor.
_COLLATION = r"^[A-Za-z][A-Za-z0-9_.@\-]{0,127}$"


class ManagedDatabaseCreate(BaseModel):
    name: str = Field(..., pattern=_DBNAME)
    server_id: int = Field(..., ge=1)
    owner_id: int = Field(..., ge=1, description="ServerUser propietario, del mismo servidor")
    model_id: int | None = Field(None, ge=1)
    # ``model_version`` sigue declarado para poder RECHAZARLO con un mensaje que nombre su
    # reemplazo. Si se borrara del schema, Pydantic lo ignoraría en silencio y el cliente
    # seguiría creyendo que declara el estado inicial.
    #
    # Por qué dejó de aceptarse: se escribía en la fila del inventario SIN tocar el motor, así
    # que la base quedaba vacía declarando estar en la versión N. Y esa columna no es
    # decorativa: ``_policy_flags`` la lee para decidir si una versión del blueprint es
    # borrable, de modo que declararla congelaba esa versión como ``in_use`` sin que ninguna
    # base la tuviera aplicada. Es literalmente el agujero que el ``PATCH`` de acá abajo ya
    # había cerrado, con el mismo argumento.
    model_version: str | None = Field(
        None,
        max_length=50,
        deprecated=True,
        description=(
            "YA NO SE ACEPTA (422). Para crear la base ya migrada usá "
            "'apply_migrations'/'target_version'; para registrar una que YA está físicamente "
            "en esa versión, 'POST /managed-databases/adopt'."
        ),
    )
    apply_migrations: bool = Field(
        False,
        description=(
            "Aplica las migraciones del blueprint inmediatamente después de crear la BD, en la "
            "misma llamada. Exige 'provision=true' y 'model_id'."
        ),
    )
    target_version: str | None = Field(
        None,
        pattern=r"^\d{4,10}$",
        description=(
            "Versión objetivo (inclusive) para 'apply_migrations'. Omitirla aplica hasta la "
            "última. Mismo criterio que el '?version=' de la ruta de apply."
        ),
    )
    environment_id: int | None = Field(
        None,
        ge=1,
        description=(
            "Entorno de despliegue que clasifica la BD. Si se omite se usa el entorno "
            "marcado is_default. OJO: en este POST omitirlo y mandarlo en null son "
            "indistinguibles, y los dos reciben el default."
        ),
    )
    charset: str | None = Field(
        None, pattern=_CHARSET, description="MySQL/MariaDB: CHARACTER SET. PostgreSQL: ENCODING."
    )
    collation: str | None = Field(
        None, pattern=_COLLATION, description="MySQL/MariaDB: COLLATE. PostgreSQL: LOCALE."
    )
    notes: str | None = None


class ManagedDatabaseUpdate(BaseModel):
    # name/server_id/owner_id NO se editan aquí (owner: usar reassign-owner).
    #
    # ``model_version`` TAMPOCO, y no es un olvido: era escribible a ciegas por el cliente, sin
    # confirmación y sin rastro de qué cambió, así que "declarar que esta BD ya está en la
    # versión X" era un simple PATCH — y esa caché es la que cualquier gate de promoción entre
    # entornos tiene que leer. La versión la escriben ``apply`` / ``rollback`` / ``stamp``
    # releyendo el motor, y para declararla a mano sigue estando ``POST /{id}/migrations/stamp``,
    # que sí valida que la versión exista en el blueprint.
    model_id: int | None = Field(None, ge=1)
    environment_id: int | None = Field(
        None,
        ge=1,
        description=(
            "Reclasifica la BD. Enviar null DESCLASIFICA (la deja sin entorno); no enviarlo "
            "no cambia nada. El entorno tiene que existir y estar activo."
        ),
    )
    charset: str | None = Field(None, pattern=_CHARSET)
    collation: str | None = Field(None, pattern=_CHARSET)
    notes: str | None = None


class ReassignOwnerIn(BaseModel):
    owner_id: int = Field(..., ge=1, description="Nuevo propietario (ServerUser del mismo servidor)")


class AdoptDatabaseIn(BaseModel):
    """
    Adopta una BD que YA existe en el motor (Plan 09): registra metadata SIN ejecutar
    CREATE DATABASE. El gateway verifica que la BD exista realmente (404 si no).
    """

    name: str = Field(..., pattern=_DBNAME, description="Nombre EXACTO de la BD existente en el motor")
    server_id: int = Field(..., ge=1)
    owner_id: int = Field(..., ge=1, description="ServerUser propietario, del mismo servidor")
    model_id: int | None = Field(None, ge=1, description="Blueprint a vincular (opcional)")
    environment_id: int | None = Field(
        None,
        ge=1,
        description=(
            "Entorno de despliegue que clasifica la BD. Si se omite se usa el entorno "
            "marcado is_default. OJO: en este POST omitirlo y mandarlo en null son "
            "indistinguibles, y los dos reciben el default."
        ),
    )
    model_version: str | None = Field(
        None,
        max_length=50,
        description=(
            "Versión del blueprint en la que YA se encuentra la BD adoptada. Si se indica, "
            "el gateway hace 'stamp' de esa versión en el motor (sin ejecutar DDL) para que "
            "el 'apply' no reintente crear lo que ya existe. Omitir = la BD llega 'en ceros'. "
            "Requiere 'model_id'."
        ),
    )
    charset: str | None = Field(None, pattern=_CHARSET, description="Opcional (no se aplica DDL)")
    collation: str | None = Field(None, pattern=_CHARSET)
    notes: str | None = None


class MigrationOutcomeOut(BaseModel):
    """
    Desenlace de la migración encadenada al alta.

    Va en el cuerpo y NO en el código HTTP a propósito: si la BD se creó y la migración falló,
    la petición no fracasó — hay una base real en el servidor de un tercero, y un 4xx sugeriría
    que no quedó nada. El operador necesita saber las dos cosas por separado.
    """

    ok: bool
    from_version: str | None = None
    to_version: str | None = None
    applied: list[str] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = Field(
        None,
        description=(
            "Código estable del motivo. Es lo que distingue 'volvé con apply?force=true' de "
            "'la BD ni siquiera se creó', que exigen endpoints distintos."
        ),
    )


class ManagedDatabaseOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    name: str
    server_id: int
    owner_id: int
    model_id: int | None = None
    model_version: str | None = None
    environment_id: int | None = Field(
        None, description="Entorno de despliegue que clasifica esta BD; null = sin clasificar"
    )
    charset: str | None = None
    collation: str | None = None
    status: ProvisionStatus
    notes: str | None = None
    origin: str = "provisioned"
    created_at: datetime
    updated_at: datetime
    migration: MigrationOutcomeOut | None = Field(
        None,
        description=(
            "Solo en el alta con 'apply_migrations'. Su ausencia significa que no se pidió "
            "migrar; ok=false significa que la BD SÍ se creó y la migración falló — la fila "
            "queda en cuarentena y el 'error_code' dice a qué endpoint volver."
        ),
    )


class ManagedDatabaseProvisionOut(BaseModel):
    """
    Resultado de ``POST /managed-databases/{id}/provision``.

    Tiene forma propia y no es un ``ManagedDatabaseOut`` a secas porque los dos desenlaces
    exitosos terminan en ``status=active`` y solo ``provisioned`` los distingue: el normal
    (se ejecutó el ``CREATE DATABASE``) y la convergencia por carrera (otra llamada al mismo
    endpoint sobre la misma fila ganó y esta recibió 1007/42P04 del motor).
    """

    database: ManagedDatabaseOut
    provisioned: bool = Field(
        ...,
        description=(
            "True si esta llamada ejecutó el CREATE DATABASE. False = convergencia por "
            "carrera: la BD ya había sido creada por una llamada simultánea y el estado se "
            "reconcilió sin emitir DDL."
        ),
    )
    previous_status: ProvisionStatus = Field(
        ..., description="Estado que tenía la fila antes de aprovisionar (pending | error | active)"
    )
    charset: str | None = Field(
        None, description="Forma CANÓNICA del catálogo que efectivamente viajó al DDL"
    )
    collation: str | None = None
