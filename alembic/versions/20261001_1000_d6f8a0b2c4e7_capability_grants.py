"""capability_grants — capacidades puntuales del gateway (plano de CONTROL)

Revision ID: d6f8a0b2c4e7
Revises: c5e7a9b1d3f6

Tabla ADITIVA: nace vacía, así que ninguna decisión de autorización existente cambia el día
del deploy. El porqué del modelo (``live_key`` como base del ``UNIQUE``, sin sentinela ni FK
en ``scope_id``) está en el docstring de ``app/models/capability_grant.py``; acá van solo las
decisiones que se ven en el DDL.

**Idempotente paso a paso.** MySQL/MariaDB no tienen DDL transaccional: si esto muere a mitad,
el próximo arranque lo corre entero otra vez. La tabla se crea solo si falta y cada índice
secundario solo si su nombre no existe (se descubre por introspección, con un inspector NUEVO
por paso porque cachea).

**Downgrade** borra la tabla. Es seguro: el código sin la tabla no concede nada (el lector
falla cerrado) y los roles siguen funcionando. ``audit_log`` conserva la historia.

Los ``comment=`` y ``server_default`` replican los del modelo: sin ellos ``alembic check``
reporta drift permanente. Los nombres de las constraints son los que produce la convención de
``app/models/base.py`` (``ck_<tabla>_<nombre>``).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d6f8a0b2c4e7"
down_revision: Union[str, None] = "c5e7a9b1d3f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "capability_grants"

_LIVE = "'pending', 'active'"
_ALL = "'pending', 'active', 'rejected', 'expired', 'cancelled', 'revoked'"

_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_capability_grants_user_status", ["user_id", "status"]),
    ("ix_capability_grants_status_expires", ["status", "expires_at"]),
)


def _existe(bind) -> bool:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return sa.inspect(bind).has_table(_TABLE)


def _indices(bind) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    if not _existe(bind):
        op.create_table(
            _TABLE,
            sa.Column(
                "id",
                sa.Integer(),
                autoincrement=True,
                nullable=False,
                comment="ID único de la capacidad puntual",
            ),
            sa.Column(
                "user_id",
                sa.Integer(),
                nullable=False,
                comment="Usuario del gateway que recibe la capacidad",
            ),
            sa.Column(
                "capability",
                sa.String(length=64),
                nullable=False,
                comment="Capacidad del catálogo (modulo.accion), otorgable",
            ),
            sa.Column(
                "scope_type",
                sa.String(length=16),
                nullable=False,
                comment="Eje del alcance: environment | server",
            ),
            sa.Column(
                "scope_id",
                sa.Integer(),
                nullable=False,
                comment="Id del entorno o servidor (> 0, sin FK: destino polimórfico)",
            ),
            sa.Column(
                "status",
                sa.String(length=16),
                nullable=False,
                server_default="pending",
                comment="pending | active | rejected | expired | cancelled | revoked",
            ),
            sa.Column(
                "live_key",
                sa.SmallInteger(),
                nullable=True,
                comment="1 si la fila está viva (pending/active), NULL si es terminal; base del UNIQUE",
            ),
            sa.Column(
                "requested_by",
                sa.Integer(),
                nullable=True,
                comment="Administrador que pidió u otorgó la capacidad",
            ),
            sa.Column(
                "decided_by",
                sa.Integer(),
                nullable=True,
                comment="Administrador que aprobó, rechazó o revocó",
            ),
            sa.Column(
                "requested_at",
                sa.DateTime(),
                nullable=False,
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                comment="Fecha y hora (UTC) de la solicitud u otorgamiento",
            ),
            sa.Column(
                "decided_at",
                sa.DateTime(),
                nullable=True,
                comment="Fecha y hora (UTC) de la decisión",
            ),
            sa.Column(
                "expires_at",
                sa.DateTime(),
                nullable=True,
                comment="Vencimiento de una solicitud pendiente (7 días); NULL en las demás",
            ),
            sa.Column(
                "request_reason",
                sa.String(length=500),
                nullable=True,
                comment="Motivo declarado al pedir la capacidad",
            ),
            sa.Column(
                "decision_reason",
                sa.String(length=500),
                nullable=True,
                comment="Motivo de la decisión (rechazo o revocación)",
            ),
            sa.Column(
                "created_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
                comment="Fecha y hora de creación del registro",
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(),
                server_default=sa.text("(CURRENT_TIMESTAMP)"),
                nullable=False,
                comment="Fecha y hora de última actualización del registro",
            ),
            sa.ForeignKeyConstraint(
                ["user_id"],
                ["users.id"],
                name="fk_capability_grants_user_id_users",
                ondelete="CASCADE",
            ),
            sa.ForeignKeyConstraint(
                ["requested_by"],
                ["users.id"],
                name="fk_capability_grants_requested_by_users",
                ondelete="SET NULL",
            ),
            sa.ForeignKeyConstraint(
                ["decided_by"],
                ["users.id"],
                name="fk_capability_grants_decided_by_users",
                ondelete="SET NULL",
            ),
            sa.PrimaryKeyConstraint("id", name="pk_capability_grants"),
            sa.UniqueConstraint(
                "user_id",
                "capability",
                "scope_type",
                "scope_id",
                "live_key",
                name="uq_capability_grants_live",
            ),
            sa.CheckConstraint(
                "scope_type IN ('environment', 'server') AND scope_id > 0",
                name="ck_capability_grants_scope",
            ),
            sa.CheckConstraint(f"status IN ({_ALL})", name="ck_capability_grants_status"),
            sa.CheckConstraint(
                f"(status IN ({_LIVE}) AND live_key IS NOT NULL AND live_key = 1) "
                f"OR (status NOT IN ({_LIVE}) AND live_key IS NULL)",
                name="ck_capability_grants_live_key_status",
            ),
            comment="Capacidades puntuales del gateway por usuario y alcance (plano de CONTROL)",
        )

    for nombre, columnas in _INDEXES:
        if nombre not in _indices(bind):
            op.create_index(nombre, _TABLE, columnas)


def downgrade() -> None:
    bind = op.get_bind()
    if not _existe(bind):
        return
    for nombre, _ in reversed(_INDEXES):
        if nombre in _indices(bind):
            op.drop_index(nombre, table_name=_TABLE)
    op.drop_table(_TABLE)
