"""
Servicio del catálogo de privilegios (tabla `privileges`).

- ``seed_privileges()``: llenado idempotente desde ``privilege_seed_rows()``. Se llama
  en el arranque (lifespan). Inserta los que falten y refresca la metadata
  (categoría/contexto/descripción/sensibilidad), pero **preserva ``is_active``** en las
  filas existentes: si un operador desactivó un privilegio, un reinicio no lo reactiva.
- ``list_privileges()`` / ``set_active()``: lectura y toggle del catálogo.
"""

from app.core.database import Database
from app.core.logger import get_logger
from app.models.privilege import Privilege
from app.services.db_admin.privilege_seed import privilege_seed_rows

logger = get_logger(__name__)


def seed_privileges() -> None:
    """Idempotente. Crea las filas faltantes y actualiza metadata; no toca is_active."""
    session = Database().get_declarative_base_session()
    try:
        existing = {(p.engine, p.name): p for p in session.query(Privilege).all()}
        created = 0
        for row in privilege_seed_rows():
            current = existing.get((row["engine"], row["name"]))
            if current is None:
                session.add(Privilege(**row))
                created += 1
            else:
                # Refrescar metadata informativa; PRESERVAR el toggle del operador.
                current.category = row["category"]
                current.context = row["context"]
                current.description = row["description"]
                current.is_sensitive = row["is_sensitive"]
        session.commit()
        if created:
            logger.info("Catálogo de privilegios sembrado: %d nuevos.", created)
    except Exception:  # noqa: BLE001 — el seeding no debe tumbar el arranque
        session.rollback()
        logger.exception("No se pudo sembrar el catálogo de privilegios.")
    finally:
        session.close()


def list_privileges(
    engine: str | None = None, active: bool | None = None
) -> list[Privilege]:
    """
    Lista el catálogo, opcionalmente filtrado por motor y/o estado de activación.

    Para el caso de uso principal ("traer solo los permisos activos de mysql o
    postgres") usar ``list_privileges(engine="mysql", active=True)``.
    """
    session = Database().get_declarative_base_session()
    try:
        q = session.query(Privilege)
        if engine is not None:
            q = q.filter(Privilege.engine == engine)
        if active is not None:
            q = q.filter(Privilege.is_active == active)
        return q.order_by(Privilege.engine, Privilege.name).all()
    finally:
        session.close()


def set_active(privilege_id: int, is_active: bool) -> Privilege | None:
    """Activa/desactiva un privilegio del catálogo. Devuelve la fila o None si no existe."""
    session = Database().get_declarative_base_session()
    try:
        priv = session.get(Privilege, privilege_id)
        if priv is None:
            return None
        priv.is_active = is_active
        session.commit()
        session.refresh(priv)
        session.expunge(priv)
        return priv
    finally:
        session.close()
