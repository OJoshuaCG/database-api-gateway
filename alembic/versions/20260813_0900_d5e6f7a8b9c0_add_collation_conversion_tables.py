"""add collation conversion tables

Revision ID: d5e6f7a8b9c0
Revises: c4d5e6f7a8b9
Create Date: 2026-08-13 09:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'd5e6f7a8b9c0'
down_revision: Union[str, None] = 'c4d5e6f7a8b9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# El DDL capturado de una rutina/trigger puede ser grande: LONGTEXT en MySQL/MariaDB,
# TEXT en PostgreSQL/SQLite (mismo criterio que clone_job_items.sql).
_SQL_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), 'mariadb').with_variant(mysql.LONGTEXT(), 'mysql')


def upgrade() -> None:
    """Aplica los cambios de esta migración (alembic upgrade)."""
    op.create_table(
        'collation_conversions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del job de conversión'),
        sa.Column('server_id', sa.Integer(), nullable=False, comment='Servidor donde vive la BD a convertir'),
        sa.Column('database_name', sa.String(length=64), nullable=False, comment='Nombre de la BD en el motor'),
        sa.Column('database_id', sa.Integer(), nullable=True, comment='managed_database_id si la BD está en el inventario; NULL si es cruda'),
        sa.Column('engine', sa.String(length=20), nullable=False, comment="Motor ('mysql'|'mariadb'; 'postgresql' no aplica)"),
        sa.Column('mode', sa.String(length=20), server_default='universal', nullable=False, comment='universal (MySQL/MariaDB: BD + tablas + los 5 tipos de objeto)'),
        sa.Column('target_charset', sa.String(length=64), nullable=False, comment='Charset objetivo (forma CANÓNICA del catálogo)'),
        sa.Column('target_collation', sa.String(length=64), nullable=False, comment='Collation objetivo (forma CANÓNICA del catálogo)'),
        sa.Column('previous_db_charset', sa.String(length=64), nullable=True, comment='Charset default que tenía la BD antes (auditoría)'),
        sa.Column('previous_db_collation', sa.String(length=64), nullable=True, comment='Collation default que tenía la BD antes (auditoría)'),
        sa.Column('selection', sa.Text(), nullable=True, comment='JSON {tables: [...], objects: [{object_type, name}]} de la selección del preview; NULL = todavía sin previsualizar'),
        sa.Column('source_fingerprint', sa.String(length=64), nullable=False, comment='SHA256 del inventario normalizado al planear (anti-TOCTOU)'),
        sa.Column('confirm_token', sa.String(length=64), nullable=True, comment='Hash del plan del último preview; execute exige que coincida'),
        sa.Column('expires_at', sa.DateTime(), nullable=False, comment='TTL del plan: tras expirar, execute exige replanear (410)'),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False, comment='pending | running | succeeded | failed | interrupted | canceled'),
        sa.Column('phase', sa.String(length=30), nullable=True, comment='Fase actual: database | tables | objects | done'),
        sa.Column('progress', sa.Text(), nullable=True, comment='JSON de progreso (conteos por fase)'),
        sa.Column('error', sa.Text(), nullable=True, comment='Error bloqueante (limpio, sin secretos) si status=failed'),
        sa.Column('cancel_requested', sa.Boolean(), server_default='0', nullable=False, comment='Flag cooperativo: el worker corta en el próximo punto seguro'),
        sa.Column('started_at', sa.DateTime(), nullable=True, comment='Momento en que el worker empezó a ejecutar'),
        sa.Column('finished_at', sa.DateTime(), nullable=True, comment='Momento en que el worker terminó (éxito/fallo/cancel)'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], name=op.f('fk_collation_conversions_server_id_servers'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['database_id'], ['managed_databases.id'], name=op.f('fk_collation_conversions_database_id_managed_databases'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_collation_conversions')),
        comment='Cabecera + estado de una conversión de charset/collation de una BD',
    )
    with op.batch_alter_table('collation_conversions', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_collation_conversions_server_id'), ['server_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_collation_conversions_database_id'), ['database_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_collation_conversions_status'), ['status'], unique=False)

    op.create_table(
        'collation_conversion_items',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del paso'),
        sa.Column('job_id', sa.Integer(), nullable=False, comment='Job al que pertenece este paso'),
        sa.Column('seq', sa.Integer(), nullable=False, comment='Orden GLOBAL de ejecución del paso'),
        sa.Column('object_type', sa.String(length=20), nullable=False, comment='database | table | procedure | function | trigger | event | view'),
        sa.Column('object_name', sa.String(length=512), nullable=False, comment='Nombre del objeto en el motor'),
        sa.Column('previous_charset', sa.String(length=64), nullable=True, comment='Charset que tenía el objeto antes del paso'),
        sa.Column('previous_collation', sa.String(length=64), nullable=True, comment='Collation que tenía antes: TABLE_COLLATION en tablas, collation_connection congelada en los objetos con cuerpo'),
        sa.Column('captured_ddl', _SQL_TEXT, nullable=True, comment='DDL exacto capturado con SHOW CREATE antes del DROP (copia de recuperación)'),
        sa.Column('sql', _SQL_TEXT, nullable=True, comment='Sentencia principal del paso (ALTER DATABASE / ALTER TABLE / CREATE)'),
        sa.Column('status', sa.String(length=20), nullable=True, comment='pending | ok | error | skipped (NULL = aún no ejecutado)'),
        sa.Column('error', sa.Text(), nullable=True, comment='Error del motor si el paso falló (limpio, sin secretos)'),
        sa.Column('grants_captured', sa.Integer(), nullable=True, comment='Cuántos privilegios de rutina se leyeron antes del DROP'),
        sa.Column('grants_reapplied', sa.Integer(), nullable=True, comment='Cuántos privilegios de rutina se reaplicaron tras el CREATE'),
        sa.Column('grants_error', sa.Text(), nullable=True, comment='Motivo si los privilegios de rutina no se pudieron leer o reaplicar'),
        sa.Column('execution_ms', sa.Integer(), nullable=True, comment='Duración del paso en milisegundos'),
        sa.Column('executed_at', sa.DateTime(), nullable=True, comment='Momento de ejecución del paso'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['job_id'], ['collation_conversions.id'], name=op.f('fk_collation_conversion_items_job_id_collation_conversions'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_collation_conversion_items')),
        comment='Paso individual de una conversión de charset/collation (BD/tabla/objeto)',
    )
    with op.batch_alter_table('collation_conversion_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_collation_conversion_items_job_id'), ['job_id'], unique=False)


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade).

    NOTA (mismo patrón que clone_jobs / schema_comparison): NO se emiten `drop_index(...)`
    explícitos antes de `drop_table(...)`. En MySQL/MariaDB soltar un índice que respalda una
    FK de la propia tabla falla ("Cannot drop index ...: needed in a foreign key
    constraint"), y `drop_table` ya elimina índices + FKs junto con la tabla. Se sueltan
    hijas antes que padres para respetar las FKs.
    """
    op.drop_table('collation_conversion_items')
    op.drop_table('collation_conversions')
