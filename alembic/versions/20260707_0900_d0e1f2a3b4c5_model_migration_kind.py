"""model_migrations: columna kind (esquema vs datos-semilla del snapshot selectivo)

Revision ID: d0e1f2a3b4c5
Revises: c9d0e1f2a3b4
Create Date: 2026-07-07 09:00:00.000000

Snapshot selectivo: un blueprint generado desde un snapshot puede dividirse en varias
migraciones y, opcionalmente, incluir datos-semilla (catálogos/tipos) con INSERT
idempotente. ``kind`` distingue el contenido:

- ``schema`` (default): DDL. Cubre todo lo existente y lo escrito a mano (retrocompat
  vía ``server_default='schema'``).
- ``data``: datos-semilla upsert. Atado a ``source_engine`` (sintaxis upsert por motor)
  y con rollback por PK.

``kind`` NO forma parte del checksum de integridad (incluirlo invalidaría el checksum de
todas las filas existentes): solo describe el contenido para la UI, los guards y la
generación del rollback, no el SQL ejecutable ni la identidad de la versión.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd0e1f2a3b4c5'
down_revision: Union[str, None] = 'c9d0e1f2a3b4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'model_migrations',
        sa.Column(
            'kind', sa.String(length=10), nullable=False, server_default='schema',
            comment=(
                "Naturaleza: 'schema' (DDL, default) | 'data' (datos-semilla upsert). "
                "'data' está atado a source_engine (sintaxis upsert por motor)"
            ),
        ),
    )


def downgrade() -> None:
    op.drop_column('model_migrations', 'kind')
