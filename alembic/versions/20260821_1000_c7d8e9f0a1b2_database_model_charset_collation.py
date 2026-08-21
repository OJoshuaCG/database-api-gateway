"""database_models: charset y collation de referencia del esquema

Revision ID: c7d8e9f0a1b2
Revises: a9b0c1d2e3f4
Create Date: 2026-08-21 10:00:00.000000

Un blueprint ES el esquema base que sus BDs replican, y el juego de caracteres forma parte
del esquema tanto como las columnas. Declararlo da un valor de REFERENCIA estable: el
validador puede avisar cuando una migración fuerza un COLLATE distinto del declarado, y se
pueden detectar BDs que se han desviado.

Ambas NULABLES y sin ``server_default``: los blueprints existentes no tienen un valor
correcto que inventar, y rellenarlos con uno arbitrario (utf8mb4_general_ci, pongamos)
generaría avisos falsos en masa. Mientras estén vacías, la comparación se hace contra el
collation real de las BDs asociadas y la UI ofrece adoptarlo; el wizard de snapshot las
rellena desde el origen, que es el camino por el que nacen la mayoría.

Semántica de la familia MySQL. PostgreSQL usa ``encoding`` + ``lc_collate``/``lc_ctype``,
que no son equivalentes: contra destinos PostgreSQL no se compara.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c7d8e9f0a1b2'
down_revision: Union[str, None] = 'a9b0c1d2e3f4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'database_models',
        sa.Column(
            'charset',
            sa.String(length=50),
            nullable=True,
            comment='Juego de caracteres de referencia (familia MySQL)',
        ),
    )
    op.add_column(
        'database_models',
        sa.Column(
            'collation',
            sa.String(length=100),
            nullable=True,
            comment='Collation de referencia (familia MySQL)',
        ),
    )


def downgrade() -> None:
    op.drop_column('database_models', 'collation')
    op.drop_column('database_models', 'charset')
