"""add model_migration_statements + dependency columns on schema_comparison_items

Revision ID: e2f3a4b5c6d7
Revises: d1e2f3a4b5c6
Create Date: 2026-07-26 10:00:00.000000

Dos cambios, ambos al servicio de "que la versión de blueprint no falle al ejecutarse":

1. ``schema_comparison_items.op_group`` / ``.depends_on`` — persisten el grupo ATÓMICO y
   el grafo de dependencias que calcula ``schema_diff.build_dependency_graph``. Sin esto,
   una selección parcial (adopt / execute custom) no se puede validar: el admin podía
   adoptar "la vista" sin "la tabla que lee", o el ``ADD`` de un índice redefinido sin su
   ``DROP`` previo, y el error aparecía recién contra el motor.

2. ``model_migration_statements`` — manifiesto de sentencias de una versión, con el
   reverso EMPAREJADO por ``seq``. Habilita reconciliar una aplicación PARCIAL: deshacer
   exactamente las sentencias que sí commitearon (1..k según el checkpoint), en orden
   inverso. Ver el docstring del modelo para el porqué completo.

Ambas columnas nuevas son NULLABLE y la tabla nueva es OPCIONAL: los datos existentes
(comparaciones y migraciones ya creadas) siguen siendo válidos y degradan al
comportamiento anterior (sin cierre de dependencias, sin rollback parcial).

NO verificado contra un motor real en este entorno (sin Docker/MySQL disponibles) —
verificar el ciclo upgrade → downgrade → upgrade contra la BD del gateway real antes de
desplegar, igual que el resto de las migraciones del repo.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

# revision identifiers, used by Alembic.
revision: str = 'e2f3a4b5c6d7'
down_revision: Union[str, None] = 'd1e2f3a4b5c6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# TEXT en PG/SQLite, LONGTEXT en MySQL/MariaDB (un CREATE TABLE grande o un cuerpo
# procedural supera con facilidad los 64 KB de TEXT).
_LONGTEXT = sa.Text().with_variant(mysql.LONGTEXT(), "mysql", "mariadb")


def upgrade() -> None:
    """Aplica los cambios de esta migración (alembic upgrade)."""
    op.add_column(
        'schema_comparison_items',
        sa.Column(
            'op_group', sa.String(length=600), nullable=True,
            comment=(
                "Grupo ATÓMICO ('object_type|object_name|change_type'): las varias "
                "sentencias de un mismo cambio lógico lo comparten"
            ),
        ),
    )
    op.add_column(
        'schema_comparison_items',
        sa.Column(
            'depends_on', sa.Text(), nullable=True,
            comment=(
                'JSON: op_group que deben ejecutarse ANTES que esta sentencia (base del '
                'cierre de dependencias y del linter de orden)'
            ),
        ),
    )
    op.create_index(
        op.f('ix_schema_comparison_items_op_group'),
        'schema_comparison_items', ['op_group'], unique=False,
    )

    op.create_table(
        'model_migration_statements',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único de la sentencia'),
        sa.Column('model_migration_id', sa.Integer(), nullable=False, comment='Versión de blueprint a la que pertenece'),
        sa.Column('seq', sa.Integer(), nullable=False, comment='Índice 1-based dentro del up_sql (coincide con el checkpoint)'),
        sa.Column('engine', sa.String(length=20), nullable=False, comment='Motor para el que está renderizado este SQL'),
        sa.Column('up_sql', _LONGTEXT, nullable=False, comment='La sentencia tal como se ejecuta en el apply'),
        sa.Column('down_sql', _LONGTEXT, nullable=True, comment='Reverso EXACTO de esta sentencia (NULL = no reversible)'),
        sa.Column('down_confirmed', sa.Boolean(), server_default='0', nullable=False, comment='True si el reverso es demostrablemente seguro'),
        sa.Column('object_type', sa.String(length=40), nullable=True, comment='Tipo de objeto que toca (para reportes)'),
        sa.Column('object_name', sa.String(length=512), nullable=True, comment='Nombre del objeto que toca (para reportes)'),
        sa.Column('op_group', sa.String(length=600), nullable=True, comment='Grupo atómico del cambio lógico'),
        sa.Column('destructive', sa.Boolean(), server_default='0', nullable=False, comment='True si la sentencia puede perder datos'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.ForeignKeyConstraint(['model_migration_id'], ['model_migrations.id'], name=op.f('fk_model_migration_statements_model_migration_id_model_migrations'), ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_model_migration_statements')),
        sa.UniqueConstraint('model_migration_id', 'seq', name=op.f('uq_mms_migration_seq')),
        comment=(
            'Manifiesto de sentencias de una versión de blueprint: una fila por sentencia '
            'con su reverso emparejado (habilita el rollback parcial)'
        ),
    )


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade).

    ``model_migration_statements``: solo ``drop_table``. NO se sueltan antes el índice de
    la FK ni el UniqueConstraint — MySQL/MariaDB rechazan soltar un índice que respalda
    una FK todavía viva, y ambos caen con la tabla (mismo patrón que
    ``migration_statement_progress``).

    ``schema_comparison_items``: el índice de ``op_group`` NO respalda ninguna FK, así que
    se suelta explícitamente antes de la columna (necesario en MySQL/MariaDB, donde
    ``drop_column`` no siempre arrastra un índice nombrado).
    """
    op.drop_table('model_migration_statements')
    op.drop_index(
        op.f('ix_schema_comparison_items_op_group'), table_name='schema_comparison_items'
    )
    op.drop_column('schema_comparison_items', 'depends_on')
    op.drop_column('schema_comparison_items', 'op_group')
