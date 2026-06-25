"""plan02 model migrations: model_migrations + database_migration_history

Revision ID: e5f6a7b8c9d0
Revises: d4e5f6a7b8c9
Create Date: 2026-06-25 09:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e5f6a7b8c9d0'
down_revision: Union[str, None] = 'd4e5f6a7b8c9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TS = dict(server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False)


def upgrade() -> None:
    op.create_table(
        'model_migrations',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False,
                  comment='ID único de la migración'),
        sa.Column('model_id', sa.Integer(), nullable=False,
                  comment='Blueprint al que pertenece esta migración'),
        sa.Column('version', sa.String(length=10), nullable=False,
                  comment="Versión secuencial con padding ('0001', '0002'…); se ordena NUMÉRICAMENTE"),
        sa.Column('name', sa.String(length=200), nullable=False,
                  comment='Descripción corta de la migración'),
        sa.Column('up_sql', sa.Text(), nullable=False,
                  comment='SQL base del delta (dialecto de referencia: MySQL)'),
        sa.Column('up_sql_mysql', sa.Text(), nullable=True,
                  comment='Override manual para MySQL/MariaDB (opcional)'),
        sa.Column('up_sql_postgresql', sa.Text(), nullable=True,
                  comment='Override manual para PostgreSQL (opcional)'),
        sa.Column('down_sql_suggested', sa.Text(), nullable=True,
                  comment='Rollback auto-generado (solo sugerencia, no se aplica)'),
        sa.Column('down_sql', sa.Text(), nullable=True,
                  comment='Rollback CONFIRMADO por el admin; si es NULL no hay rollback disponible'),
        sa.Column('checksum', sa.String(length=64), nullable=False,
                  comment='SHA256(up_sql + variantes + down_sql + version) — detecta alteración'),
        sa.Column('created_at', sa.DateTime(), comment='Fecha y hora de creación del registro', **_TS),
        sa.Column('updated_at', sa.DateTime(), comment='Fecha y hora de última actualización del registro', **_TS),
        sa.ForeignKeyConstraint(['model_id'], ['database_models.id'],
                                name=op.f('fk_model_migrations_model_id_database_models'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_model_migrations')),
        sa.UniqueConstraint('model_id', 'version', name='uq_model_migrations_model_version'),
        comment='Migraciones versionadas (deltas SQL) de cada blueprint',
    )
    # Sin índice propio sobre model_id: el UNIQUE(model_id, version) lo cubre.

    op.create_table(
        'database_migration_history',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False,
                  comment='ID único del registro de historial'),
        sa.Column('managed_database_id', sa.Integer(), nullable=False,
                  comment='BD gestionada sobre la que se aplicó la migración'),
        sa.Column('model_migration_id', sa.Integer(), nullable=False,
                  comment='Migración aplicada/revertida'),
        sa.Column('applied_at', sa.DateTime(), nullable=False,
                  comment='Momento en que se ejecutó el intento'),
        sa.Column('status', sa.Enum('applied', 'failed', name='migrationstatus',
                                     native_enum=False, length=20), nullable=False,
                  comment='Desenlace del intento (applied | failed)'),
        sa.Column('error', sa.Text(), nullable=True,
                  comment='Detalle del error si status=failed (sin secretos)'),
        sa.Column('execution_ms', sa.Integer(), nullable=True,
                  comment='Duración de la ejecución en milisegundos'),
        sa.Column('created_at', sa.DateTime(), comment='Fecha y hora de creación del registro', **_TS),
        sa.Column('updated_at', sa.DateTime(), comment='Fecha y hora de última actualización del registro', **_TS),
        sa.ForeignKeyConstraint(['managed_database_id'], ['managed_databases.id'],
                                name=op.f('fk_database_migration_history_managed_database_id_managed_databases'),
                                ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_migration_id'], ['model_migrations.id'],
                                name=op.f('fk_database_migration_history_model_migration_id_model_migrations'),
                                ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_database_migration_history')),
        comment='Historial de aplicación/rollback de migraciones por BD gestionada',
    )
    # Índice compuesto (managed_database_id, applied_at): cubre filtro + orden del
    # historial por BD. El prefijo izquierdo sirve también los filtros por solo BD.
    op.create_index('ix_dmh_managed_db_applied_at', 'database_migration_history',
                    ['managed_database_id', 'applied_at'], unique=False)
    op.create_index(op.f('ix_database_migration_history_model_migration_id'),
                    'database_migration_history', ['model_migration_id'], unique=False)


def downgrade() -> None:
    op.drop_index(op.f('ix_database_migration_history_model_migration_id'),
                  table_name='database_migration_history')
    op.drop_index('ix_dmh_managed_db_applied_at', table_name='database_migration_history')
    op.drop_table('database_migration_history')
    op.drop_table('model_migrations')
