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
from app.services.capability_catalog import GatewayRole, GlobalCapability
from app.models.user_model import UserModel
from app.utils.security import hash_password

logger = get_logger(__name__)

# Claves bajo las que se guarda la sesión en la cookie.
SESSION_USER_ID = "admin_id"
SESSION_USERNAME = "admin_username"


def login_session(request: Request, user: dict) -> None:
    """
    Marca la sesión como autenticada para el usuario dado.

    El ``clear()`` va PRIMERO y no es cosmético: sin él, cualquier clave que ya estuviera en
    la sesión sobrevive al login. Hoy es inocuo porque acá solo viven dos claves y el login
    las sobreescribe — pero deja de serlo en cuanto la sesión guarde algo más (un marcador de
    reautenticación, un flag de "2FA pendiente"), porque ahí un valor plantado por el dueño
    anterior de la sesión pasa al dueño nuevo. ``logout_session`` y ``get_current_admin`` ya
    limpiaban; el login era el único de los tres que no.
    """
    request.session.clear()
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

    El rol y las capacidades globales se fijan EXPLÍCITAMENTE y no se heredan del default de
    la columna: ``users.gateway_role`` tiene ``server_default='viewer'`` a propósito —para que
    ninguna fila nazca con privilegio— así que un despliegue nuevo sin este bloque sembraría
    un administrador que no puede administrar. Y ``owner`` no alcanza solo: ``servers.admin``,
    ``catalogs.write`` y ``gateway.admin`` viven **únicamente** en las capacidades globales, así
    que sin las dos filas de ``user_global_capabilities`` el admin recién sembrado no podría dar
    de alta un servidor ni rotar la clave de datos.

    Es también el escritor que hace que esas dos tablas no nazcan inertes: el lector es el
    resolvedor de ``Actor``.
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
            "gateway_role": GatewayRole.OWNER.value,
        }
    )
    user_model.grant_global_capabilities(
        ADMIN_USERNAME,
        [GlobalCapability.ACCESS_ADMIN.value, GlobalCapability.SECURITY_OFFICER.value],
    )
    logger.info("Administrador '%s' sembrado.", ADMIN_USERNAME)
