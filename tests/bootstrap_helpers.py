"""
La cuenta ``admin`` de los tests: una INSTALACIÓN EXISTENTE, no una nueva. Sin efectos al importarse.

POR QUÉ EXISTE
--------------
Desde C4 la siembra de producción (``bootstrap_admin``) crea ``viewer`` + ``access_admin`` y nada
más, y abre la ventana de arranque. Cientos de tests usan ``admin_client`` para OPERAR (crear
servidores, bases, exportar...), lo que exige ``owner`` y ``security_officer``. En vez de debilitar
la siembra de producción para que los tests sigan andando, la fixture ``client`` siembra ANTES del
``lifespan`` exactamente lo que tiene una instalación que se actualizó desde antes de C2:

- ``admin`` con ``owner`` + ``access_admin`` + ``security_officer``;
- su combinación HEREDADA (``sod_exceptions``, ``reason='grandfathered'``), como la deja la
  migración ``f8b0d2e4a6c9`` (``sod_service.grandfather_user``, el helper de C2);
- la ventana de arranque CERRADA por plazo (``deadline``), como queda pasadas las 72 h.

Con eso ``bootstrap_admin`` ve un ``access_admin`` activo y no hace nada, ``startup`` encuentra la
ventana cerrada y la deja cerrada, y las elevaciones siguen yendo a segundo aprobador (lo que
miden los tests de C3). Los tests de la siembra NUEVA usan ``fresh_client``, que no pre-siembra.
"""

from __future__ import annotations

from datetime import timedelta

ADMIN = "admin"
ADMIN_PASSWORD = "admin123"


def seed_existing_install_admin() -> None:
    """Pre-siembra la cuenta combinada heredada y la ventana cerrada (ver el módulo)."""
    from app.models.access_bootstrap import CLOSED_DEADLINE
    from app.models.access_bootstrap_model import AccessBootstrapModel, utcnow
    from app.models.user_model import UserModel
    from app.services.capability_catalog import GatewayRole, GlobalCapability
    from app.services.sod_service import grandfather_user
    from app.utils.security import hash_password

    um = UserModel()
    um.create(
        {
            "username": ADMIN,
            "email": f"{ADMIN}@gateway.local",
            "hashed_password": hash_password(ADMIN_PASSWORD),
            "full_name": "Administrador",
            "notes": None,
            "is_active": True,
            "gateway_role": GatewayRole.OWNER.value,
        }
    )
    um.grant_global_capabilities(
        ADMIN,
        [GlobalCapability.ACCESS_ADMIN.value, GlobalCapability.SECURITY_OFFICER.value],
    )
    grandfather_user(um.find_by_username(ADMIN)["id"])
    now = utcnow()
    AccessBootstrapModel().insert(
        opened_at=now - timedelta(hours=73),
        closes_at=now - timedelta(hours=1),
        closed_at=now - timedelta(hours=1),
        closed_reason=CLOSED_DEADLINE,
    )
