"""gateway_sessions: sesiones del lado del servidor

Es un PREREQUISITO, no una mejora. Con la sesión entera en la cookie firmada, el
``SessionMiddleware`` de Starlette la re-firma con timestamp nuevo en cada respuesta, así que
``SESSION_MAX_AGE`` era timeout de inactividad puro —con actividad continua la sesión **no
expiraba nunca**— y ``session.clear()`` borraba la cookie del cliente sin invalidar nada.

Esta tabla es lo que hace posibles cuatro controles que antes no se podían escribir: vida
absoluta, logout real, revocación al cambiar password o rol, y correlación forense con
``audit_log``.

**Al aplicar esto, todas las sesiones vivas dejan de servir**: la cookie pasa a llevar un ``sid``
y las viejas llevan ``admin_id``, así que quien esté logueado tiene que volver a entrar. Es
aceptable y no hay forma de evitarlo — una migración de cookies exigiría aceptar el formato viejo
un rato, que es exactamente el bypass que esto viene a cerrar.

El ``downgrade`` borra la tabla y con ella el rastro. Es reversible en el esquema, no en el dato:
antes de bajar, exportar la tabla si el rastro importa.

Los ``comment=`` no son adorno: sin ellos ``alembic check`` reporta drift permanente contra el
modelo, que los declara.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e0f1a2b3c4d5"
down_revision: Union[str, None] = "d9e0f1a2b3c4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "gateway_sessions",
        sa.Column(
            "sid",
            sa.String(length=64),
            nullable=False,
            comment=(
                "Identificador opaco de 128 bits en base64url. Es lo ÚNICO que viaja en la cookie"
            ),
        ),
        sa.Column("user_id", sa.Integer(), nullable=False, comment="Dueño de la sesión"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            comment="Inicio de la sesión (UTC). ANCLA de la vida absoluta: no se actualiza nunca",
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(),
            nullable=False,
            comment="Último request visto (UTC). Ancla del timeout de INACTIVIDAD",
        ),
        sa.Column(
            "ip",
            sa.String(length=45),
            nullable=True,
            comment="IP del login (IPv6 completo: 45 caracteres)",
        ),
        sa.Column(
            "user_agent_hash",
            sa.String(length=64),
            nullable=True,
            comment="SHA256 del User-Agent del login",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(),
            nullable=True,
            comment="Cuándo se tachó la sesión (UTC). NULL = viva",
        ),
        sa.Column(
            "revoked_reason",
            sa.String(length=32),
            nullable=True,
            comment=(
                "logout | password_change | role_change | absolute | idle | admin_revoked"
            ),
        ),
        # RESTRICT y no CASCADE: borrar un usuario no puede borrar el rastro de sus sesiones,
        # que es justo lo que se necesita después de un incidente.
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("sid"),
        comment="Sesiones de administrador, con vida absoluta y revocación",
    )
    op.create_index("ix_gateway_sessions_user_id", "gateway_sessions", ["user_id"])
    # Sobre `revoked_at` porque la consulta caliente de la revocación masiva filtra por
    # (user_id, revoked_at IS NULL), y el índice de user_id solo no descarta las tachadas.
    op.create_index("ix_gateway_sessions_revoked_at", "gateway_sessions", ["revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_gateway_sessions_revoked_at", table_name="gateway_sessions")
    op.drop_index("ix_gateway_sessions_user_id", table_name="gateway_sessions")
    op.drop_table("gateway_sessions")
