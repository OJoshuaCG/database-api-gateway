"""crypto_keys: DEK envuelta para rotación de cifrado

Revision ID: c3d4e5f6a7b8
Revises: b2c3d4e5f6a7
Create Date: 2026-06-24 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c3d4e5f6a7b8'
down_revision: Union[str, None] = 'b2c3d4e5f6a7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'crypto_keys',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('dek_wrapped', sa.Text(), nullable=False,
                  comment='DEK cifrada (envuelta) por la KEK. Nunca en claro.'),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=False,
                  comment='DEK activa actual (solo una activa a la vez)'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'),
                  nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'),
                  nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_crypto_keys')),
        comment='Claves de datos (DEK) envueltas por la KEK derivada de SECRET_KEY',
    )
    with op.batch_alter_table('crypto_keys', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_crypto_keys_is_active'), ['is_active'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('crypto_keys', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_crypto_keys_is_active'))
    op.drop_table('crypto_keys')
