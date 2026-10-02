"""audit_log — índices para la lectura de auditoría (D, F-25)

Revision ID: c2e4a6b8d0f1
Revises: b1d3f5a7c9e2

``GET /audit-log`` (``policy.admin``) filtra por rango de fechas, por actor y por objeto. Hasta
acá nadie leía la tabla desde la API, así que solo tenía índices para los cruces internos
(``request_id``, ``action``, ``server_id``, ``api_token_id``). Esta migración agrega:

- ``ix_audit_log_created_at`` (``created_at``): rango ``from``/``to``.
- ``ix_audit_log_admin_id`` (``admin_id``): "qué hizo esta persona".
- ``ix_audit_log_target`` (``target_type``, ``target_id``): "qué le hicieron a este objeto".

El orden de la lectura es ``id DESC`` (la PK), así que no necesita índice propio.

**Idempotente paso a paso** (MySQL/MariaDB no tienen DDL transaccional): cada índice se crea solo
si falta, buscándolo por nombre con un inspector nuevo.

**Downgrade** borra los tres si están. La lectura sigue funcionando sin ellos, más lenta.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c2e4a6b8d0f1"
down_revision: Union[str, None] = "b1d3f5a7c9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "audit_log"
_INDEXES: tuple[tuple[str, list[str]], ...] = (
    ("ix_audit_log_created_at", ["created_at"]),
    ("ix_audit_log_admin_id", ["admin_id"]),
    ("ix_audit_log_target", ["target_type", "target_id"]),
)


def _indices(bind) -> set[str]:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}


def upgrade() -> None:
    bind = op.get_bind()
    for nombre, columnas in _INDEXES:
        if nombre not in _indices(bind):
            op.create_index(nombre, _TABLE, columnas)


def downgrade() -> None:
    bind = op.get_bind()
    for nombre, _ in reversed(_INDEXES):
        if nombre in _indices(bind):
            op.drop_index(nombre, table_name=_TABLE)
