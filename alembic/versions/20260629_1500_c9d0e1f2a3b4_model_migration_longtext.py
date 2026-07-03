"""model_migrations: columnas SQL a LONGTEXT (snapshots grandes superan TEXT de MySQL)

Revision ID: c9d0e1f2a3b4
Revises: b8c9d0e1f2a3
Create Date: 2026-06-29 15:00:00.000000

El TEXT de MySQL/MariaDB tope a 64 KB. Un baseline de SNAPSHOT (Plan 09) de una BD real
puede superarlo fácilmente (DataError 1406 "Data too long for column 'up_sql'"). Se
amplían a LONGTEXT (hasta 4 GB) las columnas de SQL de ``model_migrations``. En PostgreSQL
y SQLite, TEXT ya es ilimitado: la variante deja esos motores intactos.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from sqlalchemy.dialects import mysql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'c9d0e1f2a3b4'
down_revision: Union[str, None] = 'b8c9d0e1f2a3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Solo aplica un cambio real en MySQL/MariaDB (TEXT -> LONGTEXT); en PG/SQLite es no-op.
_LONGTEXT = sa.Text().with_variant(mysql.LONGTEXT(), "mysql", "mariadb")
_COLUMNS = [
    ("up_sql", False),
    ("up_sql_mysql", True),
    ("up_sql_postgresql", True),
    ("down_sql_suggested", True),
    ("down_sql", True),
]


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in ("mysql", "mariadb"):
        return  # TEXT ya es ilimitado en PostgreSQL/SQLite
    for col, nullable in _COLUMNS:
        op.alter_column(
            "model_migrations", col,
            existing_type=sa.Text(), type_=_LONGTEXT, existing_nullable=nullable,
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name not in ("mysql", "mariadb"):
        return
    for col, nullable in _COLUMNS:
        op.alter_column(
            "model_migrations", col,
            existing_type=mysql.LONGTEXT(), type_=sa.Text(), existing_nullable=nullable,
        )
