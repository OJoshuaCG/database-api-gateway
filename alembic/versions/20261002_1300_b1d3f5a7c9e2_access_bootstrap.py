"""access_bootstrap — ventana de arranque de los accesos (C4)

Revision ID: b1d3f5a7c9e2
Revises: a9c1e3b5d7f0

Desde C3 toda elevación espera a un segundo ``access_admin``, y crear el segundo es en sí una
elevación: una instalación con uno solo no podría completar nada. La ventana de arranque es la
salida acotada (``app/services/bootstrap_window.py``). Esta migración:

1. Crea ``access_bootstrap`` (fila única, ``id = 1``).
2. Inserta la fila según cuántos ``access_admin`` activos CON CREDENCIAL hay hoy (el criterio de
   ``count_active_access_admins``):
   - **≤ 1**: POR ABRIR (``opened_at`` NULL). El primer arranque después de la migración la abre,
     así que el plazo corre desde ese arranque y no desde el ``alembic upgrade``, que puede
     correr mucho antes en un pipeline. Incluye la instalación nueva (cero usuarios).
   - **≥ 2**: cerrada con ``closed_reason='multiple_admins_at_upgrade'``. Ya hay quien apruebe:
     la ventana nunca hizo falta.

**No toca cuentas.** La cuenta combinada sembrada antes (``owner`` + ``access_admin`` +
``security_officer``) sigue igual, heredada por ``f8b0d2e4a6c9``. El cambio de siembra (solo
``viewer`` + ``access_admin``) aplica a instalaciones NUEVAS.

**Idempotente paso a paso** (MySQL/MariaDB no tienen DDL transaccional): la tabla se crea solo si
falta y la fila se inserta solo si no está.

**Downgrade** borra la tabla. El código sin la tabla falla cerrado (la ventana no existe: las
elevaciones quedan pendientes, como en C3).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b1d3f5a7c9e2"
down_revision: Union[str, None] = "a9c1e3b5d7f0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "access_bootstrap"
_MULTIPLE = "multiple_admins_at_upgrade"


def _existe(bind, tabla: str = _TABLE) -> bool:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return sa.inspect(bind).has_table(tabla)


def _crear_tabla() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=False, nullable=False,
                  comment="Siempre 1: la fila es única"),
        sa.Column("opened_at", sa.DateTime(), nullable=True,
                  comment="Apertura (UTC); NULL = la abre el primer arranque"),
        sa.Column("closes_at", sa.DateTime(), nullable=True,
                  comment="Vencimiento de la ventana (UTC)"),
        sa.Column("closed_at", sa.DateTime(), nullable=True,
                  comment="Cierre definitivo (UTC); NULL = abierta o por abrir"),
        sa.Column("closed_reason", sa.String(length=32), nullable=True,
                  comment="second_admin | deadline | multiple_admins_at_upgrade"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"),
                  nullable=False, comment="Fecha y hora de creación del registro"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"),
                  nullable=False, comment="Fecha y hora de última actualización del registro"),
        sa.PrimaryKeyConstraint("id", name="pk_access_bootstrap"),
        sa.CheckConstraint("id = 1", name="ck_access_bootstrap_singleton"),
        sa.CheckConstraint(
            "closed_reason IS NULL OR closed_reason IN "
            "('second_admin', 'deadline', 'multiple_admins_at_upgrade')",
            name="ck_access_bootstrap_closed_reason",
        ),
        comment="Ventana de arranque de los accesos del gateway (fila única)",
    )


def _access_admins(bind) -> int:
    """``access_admin`` activos con credencial: el criterio de ``count_active_access_admins``."""
    if not (_existe(bind, "users") and _existe(bind, "user_global_capabilities")):
        return 0
    fila = bind.execute(
        sa.text(
            "SELECT COUNT(*) FROM user_global_capabilities g JOIN users u ON u.id = g.user_id "
            "WHERE g.capability = 'access_admin' AND u.is_active = :activo "
            "AND u.hashed_password <> ''"
        ),
        {"activo": True},
    ).first()
    return int(fila[0] or 0) if fila else 0


def _sembrar_fila(bind) -> None:
    if bind.execute(sa.text("SELECT 1 FROM access_bootstrap WHERE id = 1")).first():
        return
    if _access_admins(bind) <= 1:
        bind.execute(sa.text(
            "INSERT INTO access_bootstrap (id, created_at, updated_at) "
            "VALUES (1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
        ))
    else:
        bind.execute(
            sa.text(
                "INSERT INTO access_bootstrap (id, closed_at, closed_reason, created_at, "
                "updated_at) VALUES (1, CURRENT_TIMESTAMP, :motivo, CURRENT_TIMESTAMP, "
                "CURRENT_TIMESTAMP)"
            ),
            {"motivo": _MULTIPLE},
        )


def upgrade() -> None:
    bind = op.get_bind()
    if not _existe(bind):
        _crear_tabla()
    _sembrar_fila(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if _existe(bind):
        op.drop_table(_TABLE)
