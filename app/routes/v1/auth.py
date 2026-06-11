"""Endpoints de autenticación: login, logout, me."""

from fastapi import APIRouter, Request

from app.controllers.auth_controller import AuthController
from app.core.auth import AdminDep, login_session, logout_session
from app.core.limiter import limiter
from app.schemas.auth import AdminOut, LoginIn
from app.utils.response import ApiResponse, empty, success

router = APIRouter(prefix="/auth", tags=["Auth"])


@router.post("/login", response_model=ApiResponse[AdminOut])
@limiter.limit("5/minute")
def login(request: Request, credentials: LoginIn):
    admin = AuthController().authenticate(credentials.username, credentials.password)
    login_session(request, admin)
    return success(data=admin, message="Sesión iniciada.")


@router.post("/logout", response_model=ApiResponse[None])
def logout(request: Request, admin: AdminDep):
    logout_session(request)
    return empty("Sesión cerrada.")


@router.get("/me", response_model=ApiResponse[AdminOut])
def me(admin: AdminDep):
    return success(data=admin)
