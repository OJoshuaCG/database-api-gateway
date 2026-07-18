"""add clone job tables

Revision ID: b1c2d3e4f5a6
Revises: f7a8b9c0d1e2
Create Date: 2026-07-18 10:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'b1c2d3e4f5a6'
down_revision: Union[str, None] = 'f7a8b9c0d1e2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# DDL grande: LONGTEXT en MySQL/MariaDB, TEXT en PG/SQLite (igual que schema_comparison_items).
_SQL_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), 'mariadb').with_variant(mysql.LONGTEXT(), 'mysql')


def upgrade() -> None:
    """Aplica los cambios de esta migración (alembic upgrade)."""
    op.create_table(
        'clone_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del job de clonación'),
        sa.Column('source_server_id', sa.Integer(), nullable=False, comment='Servidor del origen (siempre poblado)'),
        sa.Column('source_database_name', sa.String(length=64), nullable=False, comment='Nombre de la BD origen en el motor'),
        sa.Column('source_database_id', sa.Integer(), nullable=True, comment='managed_database_id del origen si está en inventario; NULL si es cruda'),
        sa.Column('source_engine', sa.String(length=20), nullable=False, comment="Motor del origen ('mysql'|'mariadb'|'postgresql')"),
        sa.Column('target_server_id', sa.Integer(), nullable=False, comment='Servidor del destino (siempre poblado)'),
        sa.Column('target_database_name', sa.String(length=64), nullable=False, comment='Nombre de la BD destino en el motor'),
        sa.Column('target_database_id', sa.Integer(), nullable=True, comment='managed_database_id del destino si está en inventario; NULL si es cruda/nueva'),
        sa.Column('target_engine', sa.String(length=20), nullable=False, comment='Motor del destino (el DDL se renderiza para este dialecto)'),
        sa.Column('include_data', sa.Boolean(), server_default='0', nullable=False, comment='True = clonar estructura + datos; False = solo estructura'),
        sa.Column('clean_mode', sa.String(length=20), server_default='none', nullable=False, comment='none | objects (borra objeto por objeto) | drop_database (reset total)'),
        sa.Column('target_mode', sa.String(length=20), nullable=False, comment='new (crear BD) | existing (BD ya existente)'),
        sa.Column('adopt_target', sa.Boolean(), server_default='0', nullable=False, comment='True = adoptar el destino y asignarle el blueprint del origen (solo clon completo)'),
        sa.Column('adopt_owner_id', sa.Integer(), nullable=True, comment='Owner (ServerUser del servidor destino) para el registro al adoptar; requerido si adopt_target'),
        sa.Column('selection', sa.Text(), nullable=True, comment='JSON de la selección de objetos (cierre resuelto); NULL = clon completo'),
        sa.Column('source_fingerprint', sa.String(length=64), nullable=False, comment='SHA256 del snapshot normalizado del origen al planear (anti-TOCTOU)'),
        sa.Column('confirm_token', sa.String(length=64), nullable=True, comment='Token del último preview; execute exige que coincida'),
        sa.Column('expires_at', sa.DateTime(), nullable=False, comment='TTL del plan: tras expirar, execute exige replanear (410)'),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False, comment='pending | running | succeeded | failed | interrupted | canceled'),
        sa.Column('phase', sa.String(length=30), nullable=True, comment='Fase actual: clean | structure | data | adopt | done'),
        sa.Column('progress', sa.Text(), nullable=True, comment='JSON de progreso (conteos por tabla/fase)'),
        sa.Column('error', sa.Text(), nullable=True, comment='Error bloqueante (limpio, sin secretos) si status=failed'),
        sa.Column('cancel_requested', sa.Boolean(), server_default='0', nullable=False, comment='Flag cooperativo: el worker corta en el próximo punto seguro'),
        sa.Column('started_at', sa.DateTime(), nullable=True, comment='Momento en que el worker empezó a ejecutar'),
        sa.Column('finished_at', sa.DateTime(), nullable=True, comment='Momento en que el worker terminó (éxito/fallo/cancel)'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['source_server_id'], ['servers.id'], name=op.f('fk_clone_jobs_source_server_id_servers'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['target_server_id'], ['servers.id'], name=op.f('fk_clone_jobs_target_server_id_servers'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['source_database_id'], ['managed_databases.id'], name=op.f('fk_clone_jobs_source_database_id_managed_databases'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['target_database_id'], ['managed_databases.id'], name=op.f('fk_clone_jobs_target_database_id_managed_databases'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['adopt_owner_id'], ['server_users.id'], name=op.f('fk_clone_jobs_adopt_owner_id_server_users'), ondelete='SET NULL'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_clone_jobs')),
        comment='Cabecera + estado de una operación de clonación de BD',
    )
    with op.batch_alter_table('clone_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_clone_jobs_source_server_id'), ['source_server_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_clone_jobs_source_database_id'), ['source_database_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_clone_jobs_target_server_id'), ['target_server_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_clone_jobs_target_database_id'), ['target_database_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_clone_jobs_adopt_owner_id'), ['adopt_owner_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_clone_jobs_status'), ['status'], unique=False)

    op.create_table(
        'clone_job_items',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del paso'),
        sa.Column('job_id', sa.Integer(), nullable=False, comment='Job al que pertenece este paso'),
        sa.Column('seq', sa.Integer(), nullable=False, comment='Orden GLOBAL de aplicación del paso'),
        sa.Column('kind', sa.String(length=20), nullable=False, comment='clean | structure | data | adopt'),
        sa.Column('object_type', sa.String(length=40), nullable=False, comment="table|view|routine|trigger|column|index|... o 'database' para clean total"),
        sa.Column('object_name', sa.String(length=512), nullable=False, comment='Nombre del objeto (cualificado donde aplica)'),
        sa.Column('sql', _SQL_TEXT, nullable=True, comment='Sentencia DDL renderizada (estructura/clean); NULL para pasos de datos'),
        sa.Column('status', sa.String(length=20), nullable=True, comment='pending | applied | failed | skipped (NULL = aún no ejecutado)'),
        sa.Column('error', sa.Text(), nullable=True, comment='Error del motor si el paso falló (limpio, sin secretos)'),
        sa.Column('rows_copied', sa.Integer(), nullable=True, comment='Filas copiadas (solo pasos de datos)'),
        sa.Column('execution_ms', sa.Integer(), nullable=True, comment='Duración del paso en milisegundos'),
        sa.Column('executed_at', sa.DateTime(), nullable=True, comment='Momento de ejecución del paso'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['job_id'], ['clone_jobs.id'], name=op.f('fk_clone_job_items_job_id_clone_jobs'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_clone_job_items')),
        comment='Paso individual de un job de clonación (limpieza/estructura/datos/adopt)',
    )
    with op.batch_alter_table('clone_job_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_clone_job_items_job_id'), ['job_id'], unique=False)


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade).

    NOTA (mismo patrón que schema_comparison): NO se emiten `drop_index(...)` explícitos
    antes de `drop_table(...)`. En MySQL/MariaDB soltar un índice que respalda una FK de la
    propia tabla falla ("Cannot drop index ...: needed in a foreign key constraint"), y
    `drop_table` ya elimina índices + FKs junto con la tabla. Se sueltan hijas antes que
    padres para respetar las FKs.
    """
    op.drop_table('clone_job_items')
    op.drop_table('clone_jobs')
