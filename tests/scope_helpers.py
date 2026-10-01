"""
Arnés compartido de los tests de capa 2: sembrar BDs del inventario y otorgar grants por alcance.

Se extrajo de ``test_scope_layer.py`` cuando el test del registro de rutas pasó a necesitar lo
mismo. Va directo por ORM/SQL: lo que se mide es la autorización, no el alta de la BD.
"""

from sqlalchemy import text

from app.core.actor import admin_actor
from app.core.database import Database
from app.services.capability_catalog import GatewayRole


def env_id(slug: str) -> int:
    with Database().engine.begin() as conn:
        fila = conn.execute(
            text("SELECT id FROM environments WHERE slug = :s"), {"s": slug}
        ).fetchone()
    assert fila, f"no existe el entorno {slug}"
    return fila[0]


def sembrar_bd(
    server_id: int = 1, environment_id: int | None = None, name: str | None = None
) -> int:
    """Una BD del inventario, directo por ORM."""
    from app.models.managed_database import ManagedDatabase

    s = Database().get_declarative_base_session()
    try:
        bd = ManagedDatabase(
            name=name or f"bd_{environment_id or 'null'}_{server_id}",
            server_id=server_id,
            owner_id=1,
            environment_id=environment_id,
        )
        s.add(bd)
        s.commit()
        return bd.id
    finally:
        s.close()


def otorgar(scope_type: str, scope_id: int, role: str) -> None:
    """Le da al admin sembrado un grant por alcance, directo en la tabla."""
    with Database().engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO access_grants (user_id, scope_type, scope_id, role, created_at) "
                "VALUES (1, :t, :i, :r, CURRENT_TIMESTAMP)"
            ),
            {"t": scope_type, "i": scope_id, "r": role},
        )


def actor_con(base: GatewayRole, grants=()):
    return admin_actor(user_id=1, username="admin", role=base, grants=list(grants))
