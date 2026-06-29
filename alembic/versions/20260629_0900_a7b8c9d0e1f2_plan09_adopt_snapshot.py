"""plan09: adopción + snapshot (origin en managed_databases; metadatos de baseline en model_migrations)

Revision ID: a7b8c9d0e1f2
Revises: f6a7b8c9d0e1
Create Date: 2026-06-29 09:00:00.000000

Plan 09 (adopción de BDs/usuarios existentes + snapshot estructural como blueprint baseline):
- ``managed_databases.origin`` distingue BDs creadas por el gateway ('provisioned') de las
  adoptadas preexistentes ('adopted').
- ``model_migrations`` gana metadatos de las migraciones generadas por SNAPSHOT:
  ``source_engine`` (motor de origen), ``is_baseline`` y ``has_non_portable`` (incluye
  objetos procedurales no traducibles cross-engine → el blueprint queda atado a su motor).

Columnas con default a nivel servidor: las filas existentes quedan como 'provisioned' /
baseline=false / portable, que es el comportamiento correcto retroactivo.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a7b8c9d0e1f2'
down_revision: Union[str, None] = 'f6a7b8c9d0e1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'managed_databases',
        sa.Column(
            'origin', sa.String(length=20), nullable=False, server_default='provisioned',
            comment="Origen: 'provisioned' (creada por el gateway) | 'adopted' (preexistente)",
        ),
    )
    op.add_column(
        'model_migrations',
        sa.Column(
            'source_engine', sa.String(length=20), nullable=True,
            comment="Motor de origen si proviene de un snapshot; NULL = portable",
        ),
    )
    op.add_column(
        'model_migrations',
        sa.Column(
            'is_baseline', sa.Boolean(), nullable=False, server_default='0',
            comment='True si es el baseline inicial generado por snapshot (Plan 09)',
        ),
    )
    op.add_column(
        'model_migrations',
        sa.Column(
            'has_non_portable', sa.Boolean(), nullable=False, server_default='0',
            comment='True si incluye objetos procedurales no traducibles cross-engine',
        ),
    )


def downgrade() -> None:
    op.drop_column('model_migrations', 'has_non_portable')
    op.drop_column('model_migrations', 'is_baseline')
    op.drop_column('model_migrations', 'source_engine')
    op.drop_column('managed_databases', 'origin')
