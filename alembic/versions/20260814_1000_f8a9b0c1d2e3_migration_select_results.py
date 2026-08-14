"""captura de resultados de SELECT en migraciones: tabla migration_select_results
+ columna model_migrations.capture_selects

Revision ID: f8a9b0c1d2e3
Revises: e6f7a8b9c0d1
Create Date: 2026-08-14 10:00:00.000000

Una migración de blueprint puede necesitar VERIFICAR algo en la BD destino con un
``SELECT`` (filas sin backfill, duplicados que bloquean un UNIQUE…). Hasta ahora ese
resultado se descartaba. Esta tabla lo persiste, con el payload CIFRADO por la DEK del
gateway y bajo opt-in explícito por migración (``capture_selects``, default false, que
además obliga a ``reviewed=true`` y al flag ``allow_result_capture`` en el ``apply``).

Ver ``app/models/migration_select_result.py`` y
``app/services/db_admin/migration_results.py``.

``downgrade()`` REVISADO A MANO: NO se emiten ``drop_index``/``drop_constraint`` antes del
``drop_table``. En MySQL/MariaDB soltar un índice que respalda una FK de la propia tabla
falla ("Cannot drop index …: needed in a foreign key constraint"), y ``drop_table`` ya
elimina índices y FKs junto con la tabla — es el mismo bug que ya se encontró y corrigió en
la migración de ``schema_comparisons`` contra MariaDB real.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f8a9b0c1d2e3'
down_revision: Union[str, None] = 'e6f7a8b9c0d1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# El payload cifrado (Fernet + base64 sobre el JSON) puede pasar los 64 KB del TEXT de
# MySQL: LONGTEXT allí, TEXT en PostgreSQL/SQLite (ya ilimitado).
_BIG_TEXT = sa.Text().with_variant(mysql.LONGTEXT(), 'mysql').with_variant(mysql.LONGTEXT(), 'mariadb')


def upgrade() -> None:
    """Aplica los cambios de esta migración (alembic upgrade)."""
    op.create_table(
        'migration_select_results',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único de la captura'),
        sa.Column('managed_database_id', sa.Integer(), nullable=False, comment='BD gestionada sobre la que se ejecutó la sentencia'),
        sa.Column('model_migration_id', sa.Integer(), nullable=False, comment='Versión de blueprint que contenía la sentencia'),
        sa.Column('direction', sa.String(length=4), nullable=False, comment="'up' (apply) | 'down' (rollback)"),
        sa.Column('statement_index', sa.Integer(), nullable=False, comment='Índice 1-based ABSOLUTO de la sentencia (el mismo del checkpoint)'),
        sa.Column('migration_checksum', sa.String(length=64), nullable=False, comment='Checksum de la migración al capturar (gate de obsolescencia)'),
        sa.Column('sql_hash', sa.String(length=64), nullable=False, comment='SHA256 de la sentencia capturada'),
        sa.Column('sql_text', _BIG_TEXT, nullable=False, comment='Sentencia capturada, recortada y con contraseñas redactadas'),
        sa.Column('status', sa.String(length=10), server_default='ok', nullable=False, comment="'ok' | 'error' (la sentencia se ejecutó, su resultado no se pudo capturar)"),
        sa.Column('durability', sa.String(length=12), server_default='unknown', nullable=False, comment="'committed' | 'rolled_back' (PostgreSQL deshizo la migración) | 'unknown'"),
        sa.Column('columns_json', _BIG_TEXT, nullable=False, comment='JSON CIFRADO (DEK) de list[str] con los nombres de columna'),
        sa.Column('rows_json', _BIG_TEXT, nullable=False, comment='JSON CIFRADO (DEK) de list[list] con las filas (listas, no dicts)'),
        sa.Column('row_count', sa.Integer(), server_default='0', nullable=False, comment='Filas efectivamente capturadas tras aplicar los topes'),
        sa.Column('truncated', sa.Boolean(), server_default='0', nullable=False, comment='True si el resultado real excedía los topes de filas/bytes'),
        sa.Column('payload_bytes', sa.Integer(), server_default='0', nullable=False, comment='Tamaño del JSON en claro antes de cifrar'),
        sa.Column('error_message', sa.Text(), nullable=True, comment='Motivo acotado si status=error (nunca el mensaje crudo del motor)'),
        sa.Column('captured_at', sa.DateTime(), nullable=False, comment='Cuándo corrió la sentencia en el motor destino'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['managed_database_id'], ['managed_databases.id'], name=op.f('fk_migration_select_results_managed_database_id_managed_databases'), ondelete='CASCADE'),
        sa.ForeignKeyConstraint(['model_migration_id'], ['model_migrations.id'], name=op.f('fk_migration_select_results_model_migration_id_model_migrations'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_migration_select_results')),
        sa.UniqueConstraint('managed_database_id', 'model_migration_id', 'direction', 'statement_index', name='uq_msr_db_migration_direction_index'),
        comment='Resultados capturados de sentencias SELECT ejecutadas dentro de una migración de blueprint (payload cifrado; opt-in + TTL)',
    )
    with op.batch_alter_table('migration_select_results', schema=None) as batch_op:
        # Lectura CRUZADA de una misma sentencia en todas las BDs del blueprint (P1, aún sin
        # endpoint): se crea ahora para no migrar la tabla otra vez solo por el índice.
        batch_op.create_index(
            'ix_msr_migration_direction_index',
            ['model_migration_id', 'direction', 'statement_index'],
            unique=False,
        )

    op.add_column(
        'model_migrations',
        sa.Column(
            'capture_selects', sa.Boolean(), nullable=False, server_default='0',
            comment='Opt-in: capturar el resultado de los SELECT de esta versión al aplicarla',
        ),
    )


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade).

    Se sueltan primero la columna y después la tabla (no hay dependencia entre ambas, pero
    el orden mantiene la simetría con el upgrade). NO se emiten drop_index/drop_constraint:
    ``drop_table`` ya se lleva índices, UNIQUE y FKs, y hacerlo explícito rompe en
    MySQL/MariaDB con los índices que respaldan una FK.
    """
    op.drop_column('model_migrations', 'capture_selects')
    op.drop_table('migration_select_results')
