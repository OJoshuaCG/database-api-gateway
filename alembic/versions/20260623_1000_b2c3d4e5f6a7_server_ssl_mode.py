"""server: columna ssl_mode (TLS por conexión)

Revision ID: b2c3d4e5f6a7
Revises: a1b2c3d4e5f6
Create Date: 2026-06-23 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'b2c3d4e5f6a7'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table('servers', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'ssl_mode',
                sa.String(length=20),
                nullable=True,
                comment=(
                    "Política TLS hacia ESTE servidor. NULL/vacío = sin TLS. "
                    "PostgreSQL: require|verify-ca|verify-full|prefer|allow|disable. "
                    "MySQL/MariaDB: cualquier valor distinto de 'disable' cifra el transporte."
                ),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table('servers', schema=None) as batch_op:
        batch_op.drop_column('ssl_mode')
