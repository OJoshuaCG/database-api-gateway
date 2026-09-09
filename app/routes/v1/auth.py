"""Endpoints de autenticación: login, logout, me."""

from fastapi import APIRouter, Request

from app.controllers.auth_controller import AuthController
from app.core.auth import login_session, logout_session
from app.core.authz import SelfRead
from app.core.limiter import limiter
from app.controllers.authz_controller import AuthzController
from app.schemas.auth import AdminOut, LoginIn
from app.schemas.authz import MeOut
from app.utils.response import ApiResponse, empty, success

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=ApiResponse[AdminOut])
@limiter.limit("5/minute")
def login(request: Request, credentials: LoginIn):
    admin = AuthController().authenticate(credentials.username, credentials.password)
    login_session(request, admin)
    return success(data=admin, message="Sesión iniciada.")


@router.post("/logout", response_model=ApiResponse[None])
def logout(request: Request, actor: SelfRead):
    logout_session(request)
    return empty("Sesión cerrada.")


@router.get("/me", response_model=ApiResponse[MeOut])
def me(actor: SelfRead):
    """
    Identidad y capacidades EFECTIVAS del actor.

    Aditivo: `id` y `username` siguen ahí, así que la SPA de hoy no se rompe. Lo que se agrega
    —`capabilities`, `role`, `scope_roles`— sale del MISMO predicado que hace cumplir
    `require()`, no de una lista paralela.

    **Es una pista de UI. Decide el servidor, siempre.**
    """
    return success(data=AuthzController().me(actor))
