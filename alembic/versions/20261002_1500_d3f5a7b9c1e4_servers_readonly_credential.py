"""servers — credencial de SOLO LECTURA para el MCP (plan 12 §5.2, §8)

Revision ID: d3f5a7b9c1e4
Revises: c2e4a6b8d0f1

Tres columnas, todas nullable y sin default:

- ``readonly_username`` (``String(128)``).
- ``readonly_password_encrypted`` (``Text``, Fernet, la misma DEK que la pseudo-root).
- ``readonly_verified_at`` (``DateTime``): cuándo la sonda negativa observó que la credencial NO
  puede escribir. El gate del MCP exige que sea reciente; sin ella, ninguna tool que lea el motor
  corre, y **nunca** cae a la pseudo-root.

Nullable porque un servidor sin credencial de solo lectura es el estado normal y correcto: queda
fuera del alcance del MCP. Las tres entran con escritor (``PUT/DELETE
/servers/{id}/readonly-credential`` y la sonda de ``test-connection``) y lector (el gate) en la
misma entrega.

**Idempotente paso a paso** (MySQL/MariaDB no tienen DDL transaccional): cada columna se agrega
solo si falta, con un inspector nuevo por paso. **Downgrade** las borra si están; un servidor
queda simplemente fuera del MCP, que es el estado anterior.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d3f5a7b9c1e4"
down_revision: Union[str, None] = "c2e4a6b8d0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "servers"


def _columnas(bind) -> set[str]:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def _definiciones() -> list[sa.Column]:
    return [
        sa.Column(
            "readonly_username",
            sa.String(128),
            nullable=True,
            comment="Usuario de SOLO LECTURA para el MCP. NULL = servidor fuera del MCP",
        ),
        sa.Column(
            "readonly_password_encrypted",
            sa.Text(),
            nullable=True,
            comment="Password de solo lectura CIFRADO (Fernet). Nunca se expone ni se loguea",
        ),
        sa.Column(
            "readonly_verified_at",
            sa.DateTime(),
            nullable=True,
            comment="Última sonda negativa exitosa: el motor rechazó escribir con esta credencial",
        ),
    ]


def upgrade() -> None:
    bind = op.get_bind()
    for columna in _definiciones():
        if columna.name not in _columnas(bind):
            op.add_column(_TABLE, columna)


def downgrade() -> None:
    bind = op.get_bind()
    for columna in reversed(_definiciones()):
        if columna.name in _columnas(bind):
            op.drop_column(_TABLE, columna.name)
