"""collation conversion: modo columns (PostgreSQL)

Revision ID: e6f7a8b9c0d1
Revises: d5e6f7a8b9c0
Create Date: 2026-08-13 15:00:00.000000

Cambio ADITIVO (expand) para el modo ``columns`` de PostgreSQL:

1. ``collation_conversions.target_charset`` pasa a NULLABLE. PostgreSQL no tiene charset
   por columna ni por tabla y el ``ENCODING`` de la BD es INMUTABLE tras el
   ``CREATE DATABASE``: en ese modo no existe un "charset objetivo" que pedir. Ampliar la
   nulabilidad es compatible hacia atrás (ninguna fila existente cambia y el modo
   ``universal`` lo sigue exigiendo en el controller, no en la BD).
2. ``collation_conversion_items.columns_affected`` (nueva, NULLABLE): cuántas columnas
   cambió el paso. En el modo ``columns`` el paso es por TABLA (todas sus columnas viajan
   en UN solo ``ALTER TABLE``, una sola pasada y un solo lock), así que este conteo es la
   única forma de saber cuántas cambiaron sin releer el ``sql``.

CAVEAT DE DOWNGRADE: volver ``target_charset`` a NOT NULL FALLA si existe alguna fila del
modo ``columns`` (target_charset NULL). El downgrade las borra primero — son jobs de
PostgreSQL, que en el esquema anterior no podían existir.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = 'e6f7a8b9c0d1'
down_revision: Union[str, None] = 'd5e6f7a8b9c0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Aplica los cambios de esta migración (alembic upgrade)."""
    with op.batch_alter_table('collation_conversions', schema=None) as batch_op:
        batch_op.alter_column(
            'target_charset',
            existing_type=sa.String(length=64),
            nullable=True,
            existing_comment='Charset objetivo (forma CANÓNICA del catálogo)',
            comment=(
                'Charset objetivo (forma CANÓNICA del catálogo). NULL en el modo columns: '
                'PostgreSQL no tiene charset por columna.'
            ),
        )
        batch_op.alter_column(
            'mode',
            existing_type=sa.String(length=20),
            existing_nullable=False,
            existing_server_default='universal',
            existing_comment='universal (MySQL/MariaDB: BD + tablas + los 5 tipos de objeto)',
            comment=(
                'universal (MySQL/MariaDB: BD + tablas + los 5 tipos de objeto) | '
                'columns (PostgreSQL: solo ALTER COLUMN ... COLLATE)'
            ),
        )

    with op.batch_alter_table('collation_conversion_items', schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                'columns_affected',
                sa.Integer(),
                nullable=True,
                comment='Columnas cambiadas por el paso (modo columns); NULL en el modo universal',
            )
        )


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade).

    Borra primero los jobs del modo ``columns`` (PostgreSQL): sus filas tienen
    ``target_charset`` NULL y el esquema anterior lo declara NOT NULL, así que sin este
    barrido el ``ALTER`` fallaría. Los ítems caen por ``ON DELETE CASCADE``.
    """
    bind = op.get_bind()
    op.execute(
        sa.text("DELETE FROM collation_conversions WHERE target_charset IS NULL")
    )
    if bind.dialect.name == 'sqlite':
        # SQLite no aplica ON DELETE CASCADE salvo con PRAGMA foreign_keys=ON.
        op.execute(
            sa.text(
                "DELETE FROM collation_conversion_items WHERE job_id NOT IN "
                "(SELECT id FROM collation_conversions)"
            )
        )

    with op.batch_alter_table('collation_conversion_items', schema=None) as batch_op:
        batch_op.drop_column('columns_affected')

    with op.batch_alter_table('collation_conversions', schema=None) as batch_op:
        batch_op.alter_column(
            'mode',
            existing_type=sa.String(length=20),
            existing_nullable=False,
            existing_server_default='universal',
            comment='universal (MySQL/MariaDB: BD + tablas + los 5 tipos de objeto)',
        )
        batch_op.alter_column(
            'target_charset',
            existing_type=sa.String(length=64),
            nullable=False,
            comment='Charset objetivo (forma CANÓNICA del catálogo)',
        )
