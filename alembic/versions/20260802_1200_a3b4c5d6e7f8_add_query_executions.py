"""add query_executions (historial de la consola SQL)

Revision ID: a3b4c5d6e7f8
Revises: e2f3a4b5c6d7
Create Date: 2026-08-02 12:00:00.000000

Historial de la consola SQL. Tabla PROPIA y no ``audit_log.detail``: se pagina, se filtra
y guarda el SQL completo (con contraseñas redactadas), un ciclo de vida distinto al del
rastro de seguridad. NO guarda las filas devueltas, solo conteos.

``downgrade()`` se limita a ``drop_table``: soltar índices a mano antes del DROP es lo que
rompió una migración anterior del repo en MySQL/MariaDB cuando el índice respaldaba una FK.
El DROP de la tabla ya se lleva sus índices.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'a3b4c5d6e7f8'
down_revision: Union[str, None] = 'e2f3a4b5c6d7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# El SQL de una consola puede ser largo: LONGTEXT en MySQL/MariaDB, TEXT en PG/SQLite.
_SQL_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), 'mariadb').with_variant(mysql.LONGTEXT(), 'mysql')


def upgrade() -> None:
    """Aplica los cambios de esta migración (alembic upgrade)."""
    op.create_table(
        'query_executions',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único de la ejecución'),
        sa.Column('server_id', sa.Integer(), nullable=False, comment='Servidor destino sobre el que se ejecutó'),
        sa.Column('database_name', sa.String(length=128), nullable=False, comment='Base de datos sobre la que se ejecutó'),
        sa.Column('engine', sa.String(length=20), nullable=False, comment='Motor: mysql | mariadb | postgresql'),
        sa.Column('admin_id', sa.Integer(), nullable=True, comment='ID del admin del gateway que lanzó la consulta'),
        sa.Column('admin_username', sa.String(length=128), nullable=True, comment='Usuario admin del gateway (desnormalizado)'),
        sa.Column('connection_mode', sa.String(length=20), nullable=False, comment='admin | stored | provided | impersonate'),
        sa.Column('run_as_username', sa.String(length=128), nullable=False, comment='Usuario del MOTOR con el que se conectó (nunca su contraseña)'),
        sa.Column('impersonated_role', sa.String(length=128), nullable=True, comment='Rol adoptado con SET ROLE (solo PostgreSQL, modo impersonate)'),
        sa.Column('sql_text', _SQL_TEXT, nullable=False, comment='SQL enviado, con literales de contraseña redactados y recortado al tope'),
        sa.Column('sql_hash', sa.String(length=64), nullable=False, comment='SHA-256 del SQL original'),
        sa.Column('danger_level', sa.String(length=10), nullable=False, comment='read | write | ddl | blocked'),
        sa.Column('statement_count', sa.Integer(), server_default='0', nullable=False, comment='Sentencias del lote'),
        sa.Column('read_only', sa.Boolean(), server_default='0', nullable=False, comment='Se ejecutó dentro de una transacción de solo lectura'),
        sa.Column('dry_run', sa.Boolean(), server_default='0', nullable=False, comment='Se ejecutó y se revirtió para medir el impacto sin persistir'),
        sa.Column('committed', sa.Boolean(), server_default='0', nullable=False, comment='La transacción se confirmó'),
        sa.Column('status', sa.String(length=20), nullable=False, comment='success | error | blocked | preview'),
        sa.Column('rows_returned', sa.Integer(), server_default='0', nullable=False, comment='Filas devueltas (suma del lote)'),
        sa.Column('rows_affected', sa.Integer(), server_default='0', nullable=False, comment='Filas afectadas (suma del lote)'),
        sa.Column('duration_ms', sa.Integer(), server_default='0', nullable=False, comment='Duración total en milisegundos'),
        sa.Column('error_code', sa.String(length=20), nullable=True, comment='errno/SQLSTATE nativo del motor, si falló'),
        sa.Column('error_message', sa.Text(), nullable=True, comment='Mensaje del motor o motivo del bloqueo'),
        sa.Column('request_id', sa.String(length=32), nullable=True, comment='Request ID que originó la ejecución'),
        sa.Column('ip', sa.String(length=64), nullable=True, comment='IP del cliente que lanzó la consulta'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['server_id'], ['servers.id'], name=op.f('fk_query_executions_server_id_servers'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_query_executions')),
        comment='Historial de ejecuciones de la consola SQL',
    )
    op.create_index(op.f('ix_query_executions_server_id'), 'query_executions', ['server_id'], unique=False)
    op.create_index(op.f('ix_query_executions_sql_hash'), 'query_executions', ['sql_hash'], unique=False)
    op.create_index(op.f('ix_query_executions_request_id'), 'query_executions', ['request_id'], unique=False)


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade)."""
    # Solo drop_table: sus índices caen con ella y soltarlos antes rompería en
    # MySQL/MariaDB si alguno respaldara la FK a servers.
    op.drop_table('query_executions')
