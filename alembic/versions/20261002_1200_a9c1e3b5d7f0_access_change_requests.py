"""access_change_requests — elevaciones de acceso con segundo aprobador (C3)

Revision ID: a9c1e3b5d7f0
Revises: f8b0d2e4a6c9

C3 retira el techo de otorgamiento por TENENCIA ("nunca más de lo que tenés") y lo reemplaza por
una política de ASIGNACIÓN (``access_admin`` asigna cualquier rol, global o capacidad otorgable)
más un SEGUNDO APROBADOR para todo lo exclusivo de ``owner``. Esta migración crea:

1. ``access_change_requests``: la elevación pendiente (acceso final pedido, huella del acceso al
   pedirla, estado, vencimiento, decisión).
2. ``capability_grants.sod_override_json``: el ``sod_override`` que acompaña a una capacidad
   puntual pendiente se aplica al APROBARLA, así que hay que guardarlo con la solicitud.

**No migra datos.** Las capacidades puntuales que hoy están ``active`` siguen activas aunque la
capacidad sea ahora sensible (``blueprints.apply``, ``schema_diff.execute``,
``collation.execute``): ya las otorgó alguien que tenía la capacidad, bajo la regla de entonces.

**Idempotente paso a paso** (MySQL/MariaDB no tienen DDL transaccional): la tabla se crea solo
si falta, cada índice solo si su nombre no existe y la columna solo si no está.

**Downgrade** borra la tabla y la columna. Las elevaciones pendientes se pierden (nunca
surtieron efecto); ``audit_log`` conserva la historia.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a9c1e3b5d7f0"
down_revision: Union[str, None] = "f8b0d2e4a6c9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "access_change_requests"
_INDICES = {
    "ix_access_change_requests_status_expires": ["status", "expires_at"],
    "ix_access_change_requests_target_status": ["target_user_id", "status"],
}
_CG = "capability_grants"
_CG_COL = "sod_override_json"


def _existe(bind, tabla: str = _TABLE) -> bool:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return sa.inspect(bind).has_table(tabla)


def _indices(bind) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}


def _columnas(bind, tabla: str) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(tabla)}


def _crear_tabla() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False,
                  comment="ID único de la solicitud"),
        sa.Column("target_user_id", sa.Integer(), nullable=False,
                  comment="Usuario del gateway cuyo acceso se eleva"),
        sa.Column("requested_by", sa.Integer(), nullable=True,
                  comment="access_admin que pidió la elevación"),
        sa.Column("desired_state_json", sa.Text(), nullable=False,
                  comment="Acceso final pedido (rol base, globales, alcances, sod_override, "
                          "elevaciones)"),
        sa.Column("before_hash", sa.String(length=64), nullable=False,
                  comment="SHA-256 del acceso al pedirla (detecta solicitudes viejas)"),
        sa.Column("status", sa.String(length=16), server_default="pending", nullable=False,
                  comment="pending | applied | rejected | cancelled | expired"),
        sa.Column("expires_at", sa.DateTime(), nullable=False,
                  comment="Vencimiento de la solicitud pendiente (UTC, 7 días)"),
        sa.Column("decided_by", sa.Integer(), nullable=True,
                  comment="access_admin que aprobó, rechazó o canceló (NULL si fue automático)"),
        sa.Column("decided_at", sa.DateTime(), nullable=True,
                  comment="Fecha y hora (UTC) de la decisión"),
        sa.Column("reason", sa.String(length=500), nullable=True,
                  comment="Motivo de la decisión o del cierre automático"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"),
                  nullable=False, comment="Fecha y hora de creación del registro"),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"),
                  nullable=False, comment="Fecha y hora de última actualización del registro"),
        sa.ForeignKeyConstraint(
            ["target_user_id"], ["users.id"],
            name="fk_access_change_requests_target_user_id_users", ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"],
            name="fk_access_change_requests_requested_by_users", ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["decided_by"], ["users.id"],
            name="fk_access_change_requests_decided_by_users", ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_access_change_requests"),
        sa.CheckConstraint(
            "status IN ('pending', 'applied', 'rejected', 'cancelled', 'expired')",
            name="ck_access_change_requests_status",
        ),
        comment="Elevaciones de acceso del gateway pendientes de un segundo aprobador",
    )


def upgrade() -> None:
    bind = op.get_bind()
    if not _existe(bind):
        _crear_tabla()
    existentes = _indices(bind)
    for nombre, columnas in _INDICES.items():
        if nombre not in existentes:
            op.create_index(nombre, _TABLE, columnas)
    if _existe(bind, _CG) and _CG_COL not in _columnas(bind, _CG):
        op.add_column(
            _CG,
            sa.Column(_CG_COL, sa.Text(), nullable=True,
                      comment="sod_override pedido junto con la solicitud: se aplica al "
                              "APROBARLA (C3)"),
        )


def downgrade() -> None:
    bind = op.get_bind()
    if _existe(bind, _CG) and _CG_COL in _columnas(bind, _CG):
        with op.batch_alter_table(_CG) as batch:
            batch.drop_column(_CG_COL)
    if not _existe(bind):
        return
    existentes = _indices(bind)
    for nombre in _INDICES:
        if nombre in existentes:
            op.drop_index(nombre, table_name=_TABLE)
    op.drop_table(_TABLE)
