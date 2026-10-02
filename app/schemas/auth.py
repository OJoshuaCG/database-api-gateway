"""Schemas Pydantic de autenticación."""

from datetime import datetime

from pydantic import BaseModel, Field

from app.utils.security import PASSWORD_MAX_LENGTH


class LoginIn(BaseModel):
    username: str = Field(..., min_length=1, max_length=128)
    password: str = Field(..., min_length=1)


class AdminOut(BaseModel):
    id: int
    username: str


class SessionOut(BaseModel):
    """
    Una sesión viva del propio usuario.

    ``sid_prefix`` y no el ``sid``: ese identificador **es** la credencial de sesión, así que no
    puede viajar en un cuerpo de respuesta. Ocho caracteres alcanzan para distinguir filas en
    una pantalla y no sirven para autenticarse.
    """

    sid_prefix: str = Field(..., description="Prefijo del identificador, solo para identificar la fila")
    current: bool = Field(..., description="Si es la sesión desde la que se hizo esta llamada")
    created_at: datetime = Field(..., description="Inicio de la sesión (UTC). Ancla del vencimiento absoluto")
    last_seen_at: datetime = Field(..., description="Último request visto (UTC)")
    ip: str | None = Field(None, description="IP del login: es lo que permite reconocer un acceso ajeno")


class RevokeOthersOut(BaseModel):
    revoked: int = Field(..., description="Cuántas sesiones se cerraron (la actual NO se cuenta)")


class PasswordChangeIn(BaseModel):
    """
    Cambio de la password propia.

    ``new_password`` NO lleva ``min_length`` en el schema a propósito: el largo mínimo lo exige
    ``assert_password_policy``, que responde el código estable ``gateway_user.weak_password``
    con el ``min_length``. Un ``min_length`` acá cortaría antes con un 422 de Pydantic sin
    código, y la SPA no podría distinguirlo de cualquier otro error de validación.
    """

    current_password: str = Field(..., min_length=1)
    new_password: str = Field(..., min_length=1, max_length=PASSWORD_MAX_LENGTH)


class PasswordChangeOut(BaseModel):
    revoked_sessions: int = Field(
        ..., description="Cuántas sesiones OTRAS que la actual se cerraron"
    )
