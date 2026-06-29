"""model_migrations: columna reviewed (gate de aprobación de baseline de snapshot, R1)

Revision ID: b8c9d0e1f2a3
Revises: a7b8c9d0e1f2
Create Date: 2026-06-29 12:00:00.000000

R1 (deuda de seguridad del Plan 09): un baseline generado por SNAPSHOT contiene DDL
capturado del motor (vistas/rutinas/triggers) que es potencialmente no confiable. La
columna ``reviewed`` exige una aprobación humana explícita antes de aplicarlo: el
``apply``/``apply-all`` rechaza (409) un baseline ``reviewed=false``.

``server_default='1'``: todo lo existente y las migraciones escritas a mano quedan
revisadas (retrocompat); solo los baselines de snapshot se insertan con ``reviewed=false``.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b8c9d0e1f2a3'
down_revision: Union[str, None] = 'a7b8c9d0e1f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'model_migrations',
        sa.Column(
            'reviewed', sa.Boolean(), nullable=False, server_default='1',
            comment='Aprobación del admin; un baseline de snapshot nace en false hasta revisarse',
        ),
    )


def downgrade() -> None:
    op.drop_column('model_migrations', 'reviewed')
