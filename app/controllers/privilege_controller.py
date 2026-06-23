"""
Controller del catálogo de privilegios.

Lectura del catálogo (filtrado por motor/activación) y toggle de activación.
La validación del motor se hace contra ``EngineType`` para no aceptar valores libres.
"""

from app.exceptions import AppHttpException
from app.models.enums import EngineType
from app.services import privilege_catalog


class PrivilegeController:
    @staticmethod
    def _validate_engine(engine: str | None) -> str | None:
        if engine is None:
            return None
        try:
            return EngineType(engine).value
        except ValueError as exc:
            raise AppHttpException(
                message="Motor inválido. Use: mysql, mariadb o postgresql.",
                status_code=422,
                context={"engine": engine},
            ) from exc

    def list_privileges(self, engine: str | None = None, active: bool | None = None):
        engine = self._validate_engine(engine)
        return privilege_catalog.list_privileges(engine=engine, active=active)

    def set_active(self, privilege_id: int, is_active: bool):
        priv = privilege_catalog.set_active(privilege_id, is_active)
        if priv is None:
            raise AppHttpException(
                message="Privilegio no encontrado en el catálogo.",
                status_code=404,
                context={"privilege_id": privilege_id},
            )
        return priv
