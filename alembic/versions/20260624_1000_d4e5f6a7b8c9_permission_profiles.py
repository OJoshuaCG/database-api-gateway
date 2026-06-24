"""permission profiles: perfiles de permisos + items

Revision ID: d4e5f6a7b8c9
Revises: c3d4e5f6a7b8
Create Date: 2026-06-24 10:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd4e5f6a7b8c9'
down_revision: Union[str, None] = 'c3d4e5f6a7b8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TS = dict(server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False)


def upgrade() -> None:
    op.create_table(
        'permission_profiles',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('name', sa.String(length=100), nullable=False,
                  comment='Nombre del perfil (único por motor)'),
        sa.Column('engine', sa.String(length=16), nullable=False,
                  comment='Motor al que aplica: mysql | mariadb | postgresql'),
        sa.Column('description', sa.String(length=255), nullable=True,
                  comment='Para qué sirve el perfil (breve)'),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=False,
                  comment='Permite deshabilitar el perfil sin borrarlo'),
        sa.Column('created_at', sa.DateTime(), comment='Fecha y hora de creación del registro', **_TS),
        sa.Column('updated_at', sa.DateTime(), comment='Fecha y hora de última actualización del registro', **_TS),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_permission_profiles')),
        sa.UniqueConstraint('engine', 'name', name='uq_permission_profiles_engine_name'),
        comment='Plantillas de privilegios (perfiles) por motor',
    )
    with op.batch_alter_table('permission_profiles', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_permission_profiles_engine'), ['engine'], unique=False)

    op.create_table(
        'permission_profile_items',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('profile_id', sa.Integer(), nullable=False,
                  comment='Perfil al que pertenece este item'),
        sa.Column('level', sa.String(length=20), nullable=False,
                  comment='Nivel del privilegio: database|schema|table|column|sequence|routine'),
        sa.Column('privileges', sa.Text(), nullable=False,
                  comment='Privilegios canónicos separados por coma (validados contra el catálogo)'),
        sa.Column('created_at', sa.DateTime(), comment='Fecha y hora de creación del registro', **_TS),
        sa.Column('updated_at', sa.DateTime(), comment='Fecha y hora de última actualización del registro', **_TS),
        sa.ForeignKeyConstraint(['profile_id'], ['permission_profiles.id'],
                                name=op.f('fk_permission_profile_items_profile_id_permission_profiles'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_permission_profile_items')),
        sa.UniqueConstraint('profile_id', 'level', name='uq_profile_items_profile_level'),
        comment='Privilegios por nivel que componen un perfil',
    )
    with op.batch_alter_table('permission_profile_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_permission_profile_items_profile_id'), ['profile_id'], unique=False)


def downgrade() -> None:
    with op.batch_alter_table('permission_profile_items', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_permission_profile_items_profile_id'))
    op.drop_table('permission_profile_items')
    with op.batch_alter_table('permission_profiles', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_permission_profiles_engine'))
    op.drop_table('permission_profiles')
