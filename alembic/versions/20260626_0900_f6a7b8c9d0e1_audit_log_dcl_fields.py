"""audit_log: campos granulares de DCL (Plan 07 — grants)

Revision ID: f6a7b8c9d0e1
Revises: e5f6a7b8c9d0
Create Date: 2026-06-26 09:00:00.000000

Agrega a ``audit_log`` los campos específicos de operaciones GRANT/REVOKE para que la
auditoría DCL sea consultable por beneficiario, privilegio, objeto y grantor (antes solo
quedaba el resumen en ``detail``). Todos NULL en acciones no-DCL.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f6a7b8c9d0e1'
down_revision: Union[str, None] = 'e5f6a7b8c9d0'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'audit_log',
        sa.Column('grantee', sa.String(length=255), nullable=True,
                  comment='Beneficiario del grant (usuario@host)'),
    )
    op.add_column(
        'audit_log',
        sa.Column('privilege', sa.String(length=512), nullable=True,
                  comment='Privilegio(s) afectado(s), separados por coma'),
    )
    op.add_column(
        'audit_log',
        sa.Column('object_level', sa.String(length=32), nullable=True,
                  comment='Nivel del objeto: database|table|column|...'),
    )
    op.add_column(
        'audit_log',
        sa.Column('object_name', sa.String(length=512), nullable=True,
                  comment="Objeto destino, p. ej. 'db.tabla'"),
    )
    op.add_column(
        'audit_log',
        sa.Column('with_grant_option', sa.Boolean(), nullable=True,
                  comment='True si el GRANT incluyó WITH GRANT OPTION'),
    )
    op.add_column(
        'audit_log',
        sa.Column('grantor', sa.String(length=255), nullable=True,
                  comment='Credencial del gateway que ejecutó el DCL'),
    )


def downgrade() -> None:
    op.drop_column('audit_log', 'grantor')
    op.drop_column('audit_log', 'with_grant_option')
    op.drop_column('audit_log', 'object_name')
    op.drop_column('audit_log', 'object_level')
    op.drop_column('audit_log', 'privilege')
    op.drop_column('audit_log', 'grantee')
