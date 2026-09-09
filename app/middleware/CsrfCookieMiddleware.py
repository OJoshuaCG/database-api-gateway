"""
Publica la cookie de transporte del token CSRF.

Es un middleware y no una línea en el login por una razón concreta: la cookie de sesión la
renueva Starlette en cada respuesta, pero la de CSRF no la renueva nadie, así que un cliente que
la pierde —una pestaña nueva después de que expire la cookie de sesión del navegador, un
``localStorage`` limpiado, un dispositivo distinto— se quedaría sin token y sin forma de pedirlo
salvo volver a loguearse. Acá se repone en cualquier respuesta donde falte o esté desactualizada.

El token es una función determinista del ``sid`` (ver ``app/core/csrf.py``), así que reponerla no
requiere estado ni invalida nada: da el mismo valor que el request anterior.

**No es httpOnly, y eso es correcto**: el JS de la SPA tiene que poder leerla para mandar el
header. Lo que la protege es que el token se recomputa del ``sid`` server-side, así que un valor
plantado por un subdominio hermano no valida — la cookie es transporte, no la prueba.
"""

from fastapi import Request
from starlette.middleware.base import BaseHTTPMiddleware

from app.core.csrf import cookie_name, token_for
from app.core.environments import SESSION_COOKIE_SECURE


class CsrfCookieMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        respuesta = await call_next(request)

        # `request.session` existe porque este middleware corre POR DENTRO del
        # SessionMiddleware (se agrega antes en `create_versioned_app`, y el último agregado es
        # el más externo). Si no hay sesión, no hay `sid` del que derivar nada.
        try:
            sid = request.session.get("sid")
        except (AssertionError, KeyError):
            # Sin SessionMiddleware en el stack (una sub-app que no lo monte): no hay nada que
            # publicar y no es un error.
            return respuesta

        if not sid:
            return respuesta

        esperado = token_for(sid)
        if request.cookies.get(cookie_name()) != esperado:
            respuesta.set_cookie(
                cookie_name(),
                esperado,
                # NO httpOnly: el JS tiene que leerla. Ver el docstring del módulo.
                httponly=False,
                # `strict` y no `lax`: esta cookie no necesita viajar en ninguna navegación
                # cross-site, así que el modo más restrictivo no cuesta nada.
                samesite="strict",
                secure=SESSION_COOKIE_SECURE,
                path="/",
            )
        return respuesta
