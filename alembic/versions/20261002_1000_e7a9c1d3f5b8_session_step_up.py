"""gateway_sessions: ventana de step-up (``step_up_at``, ``step_up_failures``)

Las capacidades con ``requires_step_up`` pasan a exigir una contraseña confirmada hace menos de
``STEP_UP_TTL_SECONDS``. La ventana vive en la SESIÓN —es prueba de presencia de quien tiene esa
cookie, no un atributo de la cuenta— así que son dos columnas en ``gateway_sessions``:

- ``step_up_at`` (nullable): última confirmación. El login la fija en ``created_at``; NULL
  significa "ninguna", y es lo que reciben las sesiones vivas al aplicar esto: la primera
  operación sensible de cada una pide la contraseña. No se rellena con ``created_at`` porque
  una sesión abierta antes del deploy no confirmó nada dentro de la ventana.
- ``step_up_failures`` (``NOT NULL DEFAULT 0``): fallos consecutivos; al tope la sesión se
  revoca con motivo ``step_up_failed``.

**Idempotente paso a paso**: MySQL/MariaDB no tienen DDL transaccional, así que cada columna se
agrega solo si falta y el reintento de una corrida que murió a mitad no choca consigo mismo.

Los ``comment=`` replican los del modelo: sin ellos ``alembic check`` reporta drift.

Revision ID: e7a9c1d3f5b8
Revises: d6f8a0b2c4e7
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e7a9c1d3f5b8"
down_revision: Union[str, None] = "d6f8a0b2c4e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "gateway_sessions"


def _columnas_nuevas() -> list[sa.Column]:
    return [
        sa.Column(
            "step_up_at",
            sa.DateTime(),
            nullable=True,
            comment=(
                "Última confirmación de contraseña (UTC): login o POST /auth/step-up. "
                "NULL = ninguna"
            ),
        ),
        sa.Column(
            "step_up_failures",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("0"),
            comment="Fallos CONSECUTIVOS de step-up. Al llegar al tope la sesión se revoca",
        ),
    ]


def _columnas(bind) -> set[str]:
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def upgrade() -> None:
    existentes = _columnas(op.get_bind())
    for columna in _columnas_nuevas():
        if columna.name not in existentes:
            op.add_column(_TABLE, columna)


def downgrade() -> None:
    existentes = _columnas(op.get_bind())
    for columna in reversed(_columnas_nuevas()):
        if columna.name in existentes:
            op.drop_column(_TABLE, columna.name)
