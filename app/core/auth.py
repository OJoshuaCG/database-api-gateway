"""
Autenticación del gateway: sesión firmada + administrador único.

El gateway es una herramienta interna; no gestiona múltiples usuarios. La sesión
se guarda en una cookie httpOnly firmada (Starlette SessionMiddleware, backend
itsdangerous). Toda la lógica de "quién está autenticado" pasa por la dependencia
`get_current_admin`, de modo que migrar a OIDC/SSO en el futuro no requiere tocar
los endpoints.
"""

from typing import Annotated

from fastapi import Depends, Request

from app.core.environments import ADMIN_PASSWORD, ADMIN_USERNAME
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.user_model import UserModel
from app.utils.security import hash_password

logger = get_logger(__name__)

# Claves bajo las que se guarda la sesión en la cookie.
SESSION_USER_ID = "admin_id"
SESSION_USERNAME = "admin_username"


def login_session(request: Request, user: dict) -> None:
    """Marca la sesión como autenticada para el usuario dado."""
    request.session[SESSION_USER_ID] = user["id"]
    request.session[SESSION_USERNAME] = user["username"]


def logout_session(request: Request) -> None:
    request.session.clear()


def get_current_admin(request: Request) -> dict:
    """
    Dependencia que exige una sesión válida. Verifica que el usuario siga existiendo
    y activo. Devuelve {id, username}. Lanza 401 si no hay sesión válida.
    """
    admin_id = request.session.get(SESSION_USER_ID)
    if not admin_id:
        raise AppHttpException(message="No autenticado.", status_code=401)

    user = UserModel().find_by_id(admin_id)
    if not user or not user.get("is_active"):
        request.session.clear()
        raise AppHttpException(
            message="Sesión inválida o usuario inactivo.", status_code=401
        )
    return {"id": user["id"], "username": user["username"]}


# Alias de tipo para inyectar en endpoints protegidos.
AdminDep = Annotated[dict, Depends(get_current_admin)]


def bootstrap_admin() -> None:
    """
    Siembra el administrador único desde ADMIN_USERNAME/ADMIN_PASSWORD si aún no
    existe. Idempotente. Se llama en el lifespan de arranque.
    """
    if not ADMIN_PASSWORD:
        logger.warning(
            "ADMIN_PASSWORD no está definido; no se sembró ningún administrador."
        )
        return

    user_model = UserModel()
    if user_model.find_by_username(ADMIN_USERNAME):
        return

    user_model.create(
        {
            "username": ADMIN_USERNAME,
            "email": f"{ADMIN_USERNAME}@gateway.local",
            "hashed_password": hash_password(ADMIN_PASSWORD),
            "full_name": "Administrador",
            "notes": None,
            "is_active": True,
            "is_superuser": True,
        }
    )
    logger.info("Administrador '%s' sembrado.", ADMIN_USERNAME)
