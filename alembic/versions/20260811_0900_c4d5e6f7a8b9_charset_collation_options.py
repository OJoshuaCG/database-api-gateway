"""charset/collation catalog: combinaciones habilitadas para crear BDs

Revision ID: c4d5e6f7a8b9
Revises: a3b4c5d6e7f8
Create Date: 2026-08-11 09:00:00.000000

Notas:
- Las filas del seed se declaran LITERALMENTE acá (no se importa
  ``app.services.charset_catalog``): una migración debe seguir aplicando igual dentro de
  cinco versiones del código, y una migración que importa código de la app se rompe en
  cuanto ese código cambia. El mismo seed vive en el servicio para el arranque idempotente;
  divergir no hace daño (el seed del lifespan solo AGREGA lo que falte y nunca pisa toggles).
- ``collation`` es NOT NULL con centinela ``''``: con NULL, la UNIQUE de la allowlist no
  impediría duplicados (MySQL/MariaDB y PostgreSQL tratan cada NULL como distinto).
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c4d5e6f7a8b9'
down_revision: Union[str, None] = 'a3b4c5d6e7f8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# (engine_family, charset, collation, enabled, is_default)
_SEED = [
    ('mysql', 'utf8mb4', 'utf8mb4_unicode_ci', True, True),
    ('mysql', 'utf8mb4', 'utf8mb4_general_ci', True, False),
    ('mysql', 'utf8mb4', 'utf8mb4_0900_ai_ci', False, False),
    ('mysql', 'utf8mb3', 'utf8mb3_general_ci', False, False),
    ('mysql', 'latin1', 'latin1_swedish_ci', False, False),
    ('postgresql', 'UTF8', 'en_US.UTF-8', True, True),
    ('postgresql', 'UTF8', 'C', False, False),
    ('postgresql', 'UTF8', 'C.UTF-8', False, False),
]


def upgrade() -> None:
    table = op.create_table(
        'charset_collation_options',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('engine_family', sa.String(length=16), nullable=False,
                  comment='Familia de motor: mysql (cubre MySQL y MariaDB) | postgresql'),
        sa.Column('charset', sa.String(length=64), nullable=False,
                  comment='MySQL/MariaDB: CHARACTER SET. PostgreSQL: ENCODING (p. ej. UTF8)'),
        sa.Column('collation', sa.String(length=128), server_default='', nullable=False,
                  comment="MySQL/MariaDB: COLLATE. PostgreSQL: LOCALE. '' = sin collation específica"),
        sa.Column('enabled', sa.Boolean(), server_default='0', nullable=False,
                  comment='Si el gateway permite elegir esta combinación al crear una BD'),
        sa.Column('is_default', sa.Boolean(), server_default='0', nullable=False,
                  comment='Sugerencia por defecto de la familia (a lo sumo una True por engine_family)'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'),
                  nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'),
                  nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_charset_collation_options')),
        sa.UniqueConstraint('engine_family', 'charset', 'collation',
                            name='uq_charset_collation_options_family_charset_collation'),
        comment='Catálogo global de charsets/collations habilitados para crear BDs',
    )
    with op.batch_alter_table('charset_collation_options', schema=None) as batch_op:
        batch_op.create_index(
            batch_op.f('ix_charset_collation_options_engine_family'),
            ['engine_family'],
            unique=False,
        )

    op.bulk_insert(
        table,
        [
            {
                'engine_family': family,
                'charset': charset,
                'collation': collation,
                'enabled': enabled,
                'is_default': is_default,
            }
            for (family, charset, collation, enabled, is_default) in _SEED
        ],
    )


def downgrade() -> None:
    with op.batch_alter_table('charset_collation_options', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_charset_collation_options_engine_family'))
    op.drop_table('charset_collation_options')
