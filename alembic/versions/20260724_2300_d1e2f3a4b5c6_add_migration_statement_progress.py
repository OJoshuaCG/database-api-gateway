"""add migration_statement_progress table

Revision ID: d1e2f3a4b5c6
Revises: b1c2d3e4f5a6
Create Date: 2026-07-24 23:00:00.000000

Checkpoint de sentencias SQL dentro de UNA migración de blueprint, por BD gestionada y
dirección (up/down). Habilita que un ``apply``/``rollback`` que falló A MITAD de una
migración (DDL en AUTOCOMMIT: las sentencias previas ya commitearon físicamente) se
retome desde la última sentencia exitosa, en vez de re-ejecutar desde cero y chocar con
"el objeto ya existe". Ver ``app/services/db_admin/migration_progress.py``.

NO verificado contra un motor real en este entorno (sin Docker/MySQL disponibles) —
verificar el ciclo upgrade → downgrade → upgrade contra la BD del gateway real antes de
desplegar, siguiendo el mismo patrón usado para las migraciones anteriores del repo.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'd1e2f3a4b5c6'
down_revision: Union[str, None] = 'b1c2d3e4f5a6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Aplica los cambios de esta migración (alembic upgrade)."""
    op.create_table(
        'migration_statement_progress',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del checkpoint'),
        sa.Column('managed_database_id', sa.Integer(), nullable=False, comment='BD gestionada sobre la que se está aplicando/revirtiendo'),
        sa.Column('model_migration_id', sa.Integer(), nullable=False, comment='Migración cuyo progreso se rastrea'),
        sa.Column('direction', sa.String(length=4), nullable=False, comment="'up' (apply) | 'down' (rollback)"),
        sa.Column('total_statements', sa.Integer(), nullable=False, comment='Cantidad total de sentencias de esta dirección'),
        sa.Column('last_statement_index', sa.Integer(), server_default='0', nullable=False, comment='Sentencias (1-based) ejecutadas con éxito antes del último fallo'),
        sa.Column('migration_checksum', sa.String(length=64), nullable=False, comment='Checksum de ModelMigration al generar el checkpoint (fail-closed si no coincide)'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['managed_database_id'], ['managed_databases.id'], name=op.f('fk_migration_statement_progress_managed_database_id_managed_databases'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_migration_id'], ['model_migrations.id'], name=op.f('fk_migration_statement_progress_model_migration_id_model_migrations'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_migration_statement_progress')),
        sa.UniqueConstraint('managed_database_id', 'model_migration_id', 'direction', name=op.f('uq_msp_db_migration_direction')),
        comment='Checkpoint de sentencias SQL ejecutadas dentro de UNA migración (permite resumir tras un fallo parcial)',
    )


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade).

    Solo ``drop_table``: el ``UniqueConstraint`` se elimina junto con la tabla. No hay
    índices explícitos previos que soltar (mismo patrón que ``clone_jobs``/
    ``schema_comparisons``: nunca ``drop_index`` de algo FK-backed antes de
    ``drop_table`` — MySQL/MariaDB lo rechaza).
    """
    op.drop_table('migration_statement_progress')
