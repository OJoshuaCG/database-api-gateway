"""privileges catalog: tabla de privilegios por motor

Revision ID: a1b2c3d4e5f6
Revises: 5429b83cc392
Create Date: 2026-06-23 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '5429b83cc392'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'privileges',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('engine', sa.String(length=16), nullable=False,
                  comment='Motor que admite el privilegio: mysql | mariadb | postgresql'),
        sa.Column('name', sa.String(length=64), nullable=False,
                  comment='Token del privilegio, p. ej. SELECT, CREATE VIEW, GRANT OPTION'),
        sa.Column('category', sa.String(length=16), server_default='object', nullable=False,
                  comment='object = otorgable sobre objetos; admin = global/servidor'),
        sa.Column('context', sa.String(length=128), nullable=True,
                  comment="Niveles donde aplica (informativo), p. ej. 'Tables,Columns'"),
        sa.Column('description', sa.String(length=255), nullable=False,
                  comment='Qué permite el privilegio (breve)'),
        sa.Column('is_sensitive', sa.Boolean(), server_default='0', nullable=False,
                  comment='Requiere confirmación extra al otorgar (ALL, GRANT OPTION, MAINTAIN)'),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=False,
                  comment='Si la plataforma controla/expone este privilegio'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'),
                  nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'),
                  nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_privileges')),
        sa.UniqueConstraint('engine', 'name', name='uq_privileges_engine_name'),
        comment='Catálogo de privilegios soportados por cada motor de BD',
    )
    with op.batch_alter_table('privileges', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_privileges_engine'), ['engine'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('privileges', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_privileges_engine'))
    op.drop_table('privileges')
