from fastapi import APIRouter
from fastapi.responses import JSONResponse
from sqlalchemy import text

from app.core.database import Database
from app.core.environments import APP_ENV, APP_NAME

router = APIRouter(tags=["health"])


@router.get("/health")
async def health():
    """
    Liveness: el proceso está vivo y responde.
    No está versionado ni tiene rate limiting. No comprueba dependencias.
    Útil para health checks de Docker/Kubernetes (livenessProbe).
    """
    return {
        "status": "ok",
        "service": APP_NAME,
        "environment": APP_ENV,
    }


@router.get("/health/ready")
def readiness():
    """
    Readiness: la app puede atender tráfico, i.e. la BD de metadatos del gateway es
    alcanzable. Devuelve 503 si el ``SELECT 1`` falla, para que el balanceador deje
    de enrutar a esta instancia.

    Es ``def`` (no ``async``): el ``SELECT 1`` es I/O bloqueante y FastAPI lo corre en
    el threadpool, sin bloquear el event loop.
    """
    try:
        session = Database().get_declarative_base_session()
        try:
            session.execute(text("SELECT 1"))
        finally:
            session.close()
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "status": "unavailable",
                "service": APP_NAME,
                "environment": APP_ENV,
                "detail": "metadata database unreachable",
            },
        )
    return {
        "status": "ready",
        "service": APP_NAME,
        "environment": APP_ENV,
    }
