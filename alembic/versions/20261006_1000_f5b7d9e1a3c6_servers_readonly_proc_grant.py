"""servers — bandera ``readonly_proc_grant`` (SELECT ON mysql.proc server-wide)

Revision ID: f5b7d9e1a3c6
Revises: e4a6c8f0b2d5

Una columna booleana ``NOT NULL`` con ``server_default`` falso:

- ``readonly_proc_grant``: habilita ``SELECT ON mysql.proc`` para la credencial de solo lectura
  del MCP en MariaDB < 11.3 / MySQL 5.7, la única forma de leer el código de las rutinas ahí.
  ``mysql.proc`` es SERVER-WIDE: el grant expone las rutinas de TODAS las bases del servidor, así
  que nace apagada en cada fila existente (el default cierra, nunca abre) y solo la enciende
  ``PUT /servers/{id}/readonly-credential/routine-bodies``.

**Idempotente paso a paso** (MySQL/MariaDB no tienen DDL transaccional): la columna se agrega
solo si falta, con un inspector nuevo en cada consulta. **Downgrade** la borra si está. Antes de
bajar de versión conviene apagar la bandera en cada servidor: apagarla re-converge la cuenta del
motor (``REVOKE ALL`` y re-grant sin ``mysql.proc``). Borrar solo la columna NO revoca el grant
que ya esté en el motor.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f5b7d9e1a3c6"
down_revision: Union[str, None] = "e4a6c8f0b2d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "servers"
_COLUMN = "readonly_proc_grant"


def _columnas(bind) -> set[str]:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def _definicion() -> sa.Column:
    return sa.Column(
        _COLUMN,
        sa.Boolean(),
        nullable=False,
        server_default=sa.false(),
        comment=(
            "Permite SELECT ON mysql.proc a la credencial de solo lectura (cuerpos de rutinas "
            "en MariaDB <11.3 / MySQL 5.7). Expone rutinas de TODAS las bases del servidor"
        ),
    )


def upgrade() -> None:
    bind = op.get_bind()
    if _COLUMN not in _columnas(bind):
        op.add_column(_TABLE, _definicion())


def downgrade() -> None:
    bind = op.get_bind()
    if _COLUMN in _columnas(bind):
        op.drop_column(_TABLE, _COLUMN)
