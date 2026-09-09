"""
Schemas de los usuarios DEL GATEWAY.

OJO CON EL NOMBRE: son los usuarios que se autentican **contra el gateway**, no los del motor
(``schemas/server_user.py``). Ver el docstring de ``gateway_user_controller``.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

from app.utils.security import PASSWORD_MIN_LENGTH

_USERNAME = r"^[a-z0-9]([a-z0-9._-]{1,38}[a-z0-9])?$"


class ScopeGrantIn(BaseModel):
    """Un acceso con alcance. ``role`` REEMPLAZA al rol base en ese alcance, no se suma."""

    scope_type: str = Field(..., description="'environment' | 'server'")
    scope_id: int = Field(..., ge=1)
    role: str = Field(..., description="viewer | operator | owner")


class GatewayUserCreate(BaseModel):
    """
    Alta de un usuario del gateway. **No hay campo de password, y es a propósito.**

    Si quien crea la cuenta tipeara la password inicial, conocería una credencial funcional de
    esa identidad — y con eso **toda fila de auditoría atribuida a esa persona sería
    repudiable**. La cuenta nace sin credencial y la respuesta trae un token de invitación de un
    solo uso.
    """

    username: str = Field(..., min_length=2, max_length=40, pattern=_USERNAME)
    email: str | None = Field(None, max_length=255)
    full_name: str | None = Field(None, max_length=150)
    notes: str | None = None
    gateway_role: str = Field("viewer", description="viewer | operator | owner")
    global_capabilities: list[str] = Field(
        default_factory=list, description="access_admin | security_officer"
    )


class GatewayUserUpdate(BaseModel):
    """
    PATCH parcial. **``username`` no está y no es un olvido**: es la identidad que se audita, y
    ``audit_log`` la desnormaliza sin FK, así que renombrar reescribiría el significado de las
    filas viejas.
    """

    full_name: str | None = Field(None, max_length=150)
    email: str | None = Field(None, max_length=255)
    notes: str | None = None
    gateway_role: str | None = None
    is_active: bool | None = None


class GatewayUserAccessIn(BaseModel):
    """
    El acceso COMPLETO de la persona. Es un reemplazo, no un incremento.

    Un PUT y no N POSTs por grant porque la pregunta que responde una pantalla de accesos es
    "qué acceso tiene", y con endpoints por grant el estado final depende del orden de N
    llamadas — y una que falle a mitad deja un acceso que nadie pidió.
    """

    global_capabilities: list[str] = Field(default_factory=list)
    scope_grants: list[ScopeGrantIn] = Field(default_factory=list)


class AcceptInviteIn(BaseModel):
    """
    Primera password, elegida por la persona.

    El largo mínimo sale de ``app.utils.security.PASSWORD_MIN_LENGTH``, que es la MISMA
    constante que usa el controller: duplicar el número es duplicar la regla, y el día que
    alguien suba una y no la otra el schema rechaza lo que el controller acepta.
    """

    token: str = Field(..., min_length=8)
    password: str = Field(..., min_length=PASSWORD_MIN_LENGTH, max_length=200)


class ScopeGrantOut(BaseModel):
    scope_type: str
    scope_id: int
    role: str


class GatewayUserOut(BaseModel):
    """
    **Nunca incluye nada de la credencial.** El estado se publica como ``credential_set``:
    quien administra accesos necesita saber si la invitación se aceptó, no ver el hash.
    """

    model_config = ConfigDict(from_attributes=True)

    id: int
    username: str
    email: str | None = None
    full_name: str | None = None
    gateway_role: str
    is_active: bool
    credential_set: bool = Field(
        ..., description="False = invitación pendiente: la cuenta existe y no puede entrar"
    )
    global_capabilities: list[str] = Field(default_factory=list)
    scope_grants: list[ScopeGrantOut] = Field(default_factory=list)
    last_login_at: datetime | None = None
    previous_login_at: datetime | None = None
    last_failed_at: datetime | None = None
    created_at: datetime | None = None


class GatewayUserCreatedOut(GatewayUserOut):
    """
    El alta, con la invitación.

    El token viaja en la respuesta y **el gateway no lo manda a ningún lado**: no hay sustrato
    de notificación (ni SMTP, ni webhook, ni cola) y fingir que lo hay sería peor que no
    tenerlo. Quien crea la cuenta se lo entrega a la persona por el canal que corresponda.
    """

    invite_token: str
    invite_expires_at: datetime


class InviteOut(BaseModel):
    invite_token: str
    invite_expires_at: datetime


class AcceptInviteOut(BaseModel):
    username: str
