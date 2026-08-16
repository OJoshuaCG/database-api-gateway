"""add export job tables

Revision ID: a9b0c1d2e3f4
Revises: f8a9b0c1d2e3
Create Date: 2026-08-16 14:04:59.840876

Crea el esquema del módulo de exportación de bases de datos: cabecera del job
(``export_jobs``), reporte por objeto (``export_job_items``) y artefacto efímero
(``export_artifacts``).

NOTA sobre el autogenerado: la corrida trajo además cambios AJENOS a esta migración
—alteraciones de ``server_default`` en ``charset_collation_options``,
``migration_statement_progress`` y ``query_executions``, y un índice faltante en
``model_migration_statements``—. Se quitaron a mano. Los ``server_default`` son ruido de
reflexión de SQLite (``0`` vs ``'0'``), y el índice es drift PREEXISTENTE de otro módulo:
arrastrarlo acá lo escondería dentro de una migración que dice hacer otra cosa, y su
``downgrade`` lo borraría aunque nunca lo hubiera creado esta versión.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'a9b0c1d2e3f4'
down_revision: Union[str, None] = 'f8a9b0c1d2e3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# El ExportSpec y la selección resuelta son JSON que crece con el catálogo: LONGTEXT en
# MySQL/MariaDB, TEXT en PostgreSQL/SQLite (mismo criterio que clone_job_items.sql).
_JSON_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), 'mariadb').with_variant(mysql.LONGTEXT(), 'mysql')


def upgrade() -> None:
    """Aplica los cambios de esta migración (alembic upgrade)."""
    op.create_table(
        'export_jobs',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del job de exportación'),
        sa.Column('server_id', sa.Integer(), nullable=False, comment='Servidor donde vive la BD a exportar'),
        sa.Column('database_name', sa.String(length=64), nullable=False, comment='Nombre de la BD en el motor'),
        sa.Column('database_id', sa.Integer(), nullable=True, comment='managed_database_id si la BD está en el inventario; NULL si es cruda'),
        sa.Column('engine', sa.String(length=20), nullable=False, comment="Motor del origen ('mysql'|'mariadb'|'postgresql')"),
        sa.Column('spec', _JSON_TEXT, nullable=False, comment='ExportSpec COMPLETO en JSON: autosuficiente para reproducir el mismo artefacto'),
        sa.Column('resolved_selection', _JSON_TEXT, nullable=True, comment='JSON de la selección ya RESUELTA a objetos explícitos; se congela en el preview'),
        sa.Column('source_fingerprint', sa.String(length=64), nullable=False, comment='SHA256 del snapshot normalizado del origen (anti-TOCTOU)'),
        sa.Column('confirm_token', sa.String(length=64), nullable=True, comment='Hash del PLAN RESUELTO del último preview; execute exige que coincida'),
        sa.Column('expires_at', sa.DateTime(), nullable=False, comment='TTL del plan: tras expirar, preview/execute exigen replanear (410)'),
        sa.Column('status', sa.String(length=20), server_default='pending', nullable=False, comment='pending | running | succeeded | failed | interrupted | canceled'),
        sa.Column('phase', sa.String(length=30), nullable=True, comment='Fase actual del orden de emisión: structure | data | bodies | done'),
        sa.Column('progress', sa.Text(), nullable=True, comment='JSON de progreso (objetos/filas/bytes emitidos)'),
        sa.Column('error', sa.Text(), nullable=True, comment='Error bloqueante (motivo acotado, NUNCA str(exc) del motor) si failed'),
        sa.Column('cancel_requested', sa.Boolean(), server_default='0', nullable=False, comment='Flag cooperativo: el worker corta en el próximo punto seguro'),
        sa.Column('structure_drift_detected', sa.Boolean(), server_default='0', nullable=False, comment='El catálogo del origen cambió durante la corrida (el snapshot MVCC de MySQL no cubre el diccionario de datos)'),
        sa.Column('created_by_admin_id', sa.Integer(), nullable=True, comment='Admin que creó el plan. Sin FK a propósito (criterio de audit_log.admin_id): borrar un admin no debe mutilar el rastro'),
        sa.Column('idempotency_key', sa.String(length=128), nullable=True, comment='Clave de reintento del cliente. UNIQUE: un reintento devuelve el MISMO plan en vez de una segunda exportación'),
        sa.Column('started_at', sa.DateTime(), nullable=True, comment='Momento en que el worker empezó a generar'),
        sa.Column('finished_at', sa.DateTime(), nullable=True, comment='Momento en que el worker terminó'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['database_id'], ['managed_databases.id'], name=op.f('fk_export_jobs_database_id_managed_databases'), ondelete='SET NULL'),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], name=op.f('fk_export_jobs_server_id_servers'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_export_jobs')),
        sa.UniqueConstraint('idempotency_key', name=op.f('uq_export_jobs_idempotency_key')),
        comment='Cabecera + estado de una exportación de BD a un artefacto',
    )
    with op.batch_alter_table('export_jobs', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_export_jobs_server_id'), ['server_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_export_jobs_database_id'), ['database_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_export_jobs_status'), ['status'], unique=False)

    op.create_table(
        'export_job_items',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del ítem'),
        sa.Column('job_id', sa.Integer(), nullable=False, comment='Job al que pertenece este ítem'),
        sa.Column('seq', sa.Integer(), nullable=False, comment='Orden GLOBAL de emisión del objeto en el artefacto'),
        sa.Column('object_type', sa.String(length=40), nullable=False, comment='table | view | materialized_view | routine | trigger | event | sequence | ...'),
        sa.Column('object_name', sa.String(length=512), nullable=False, comment='Nombre del objeto tal como lo da el catálogo'),
        sa.Column('phase', sa.String(length=30), nullable=True, comment='Fase del orden de emisión en la que salió'),
        sa.Column('status', sa.String(length=20), nullable=True, comment='pending | ok | error | skipped (NULL = aún no procesado)'),
        sa.Column('reason', sa.Text(), nullable=True, comment='Motivo ACOTADO de un skip/error. NUNCA str(exc) del motor: puede incrustar VALORES de filas'),
        sa.Column('rows_exported', sa.Integer(), nullable=True, comment='Filas emitidas (solo objetos con datos)'),
        sa.Column('bytes_written', sa.Integer(), nullable=True, comment='Bytes que este objeto aportó al artefacto'),
        sa.Column('deterministic', sa.Boolean(), nullable=True, comment='False = las filas salieron SIN orden garantizado (tabla sin PK)'),
        sa.Column('execution_ms', sa.Integer(), nullable=True, comment='Duración de la emisión de este objeto (ms)'),
        sa.Column('executed_at', sa.DateTime(), nullable=True, comment='Momento en que se emitió el objeto'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['job_id'], ['export_jobs.id'], name=op.f('fk_export_job_items_job_id_export_jobs'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_export_job_items')),
        comment='Resultado por objeto de una exportación (reporte de incidencias)',
    )
    with op.batch_alter_table('export_job_items', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_export_job_items_job_id'), ['job_id'], unique=False)

    op.create_table(
        'export_artifacts',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del artefacto'),
        sa.Column('job_id', sa.Integer(), nullable=False, comment='Job que lo generó (uno por job)'),
        sa.Column('storage_name', sa.String(length=128), nullable=False, comment='Nombre OPACO del archivo en el spool: el cliente nunca envía ni recibe una ruta'),
        sa.Column('byte_size', sa.Integer(), nullable=False, comment='Tamaño en bytes del artefacto entregado'),
        sa.Column('sha256', sa.String(length=64), nullable=False, comment='Checksum: verificación de integridad sin abrir el archivo'),
        sa.Column('content_type', sa.String(length=100), nullable=False, comment='MIME del artefacto (text/plain, application/gzip, application/zip)'),
        sa.Column('part_count', sa.Integer(), server_default='1', nullable=False, comment='Cantidad de partes (>1 con split_max_bytes / organization=per_object)'),
        sa.Column('state', sa.String(length=20), server_default='available', nullable=False, comment='available | consumed (descarga de un solo uso) | purged (TTL/huérfano)'),
        sa.Column('expires_at', sa.DateTime(), nullable=False, comment='Momento de purga. Indexado: la tarea periódica del lifespan barre por esta columna'),
        sa.Column('downloaded_at', sa.DateTime(), nullable=True, comment='Momento de la última descarga'),
        sa.Column('download_count', sa.Integer(), server_default='0', nullable=False, comment='Cuántas veces se descargó (con un solo uso, a lo sumo 1)'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['job_id'], ['export_jobs.id'], name=op.f('fk_export_artifacts_job_id_export_jobs'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_export_artifacts')),
        sa.UniqueConstraint('job_id', name=op.f('uq_export_artifacts_job_id')),
        comment='Artefacto generado por una exportación (efímero, con TTL)',
    )
    with op.batch_alter_table('export_artifacts', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_export_artifacts_state'), ['state'], unique=False)
        batch_op.create_index(batch_op.f('ix_export_artifacts_expires_at'), ['expires_at'], unique=False)


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade).

    NOTA (mismo patrón que clone_jobs / collation_conversions / schema_comparison): NO se
    emiten ``drop_index(...)`` explícitos antes de ``drop_table(...)``. En MySQL/MariaDB
    soltar un índice que respalda una FK de la propia tabla falla con "Cannot drop index
    ...: needed in a foreign key constraint", y ``drop_table`` ya elimina índices y FKs
    junto con la tabla. El autogenerado los emite igual; hay que quitarlos a mano cada vez.

    Se sueltan las HIJAS antes que la PADRE: ``export_job_items`` y ``export_artifacts``
    referencian ``export_jobs``.
    """
    op.drop_table('export_artifacts')
    op.drop_table('export_job_items')
    op.drop_table('export_jobs')
