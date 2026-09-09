"""Traza de autenticación en users: previous_login_at, last_login_at y last_failed_at

Tres columnas nullable sobre ``users``. Es la migración más simple posible a propósito, y por eso
vale anotar lo que NO trae.

**No trae el contador de fallos.** El throttling por cuenta lo necesita, pero llega en otra
entrega: una columna sin escritor ni lector es un flag inerte, y con la peor forma —la que hace
creer que hay un control de fuerza bruta donde no hay ninguno—. Entra con su throttling o no
entra.

Nacen NULL y se quedan NULL para las filas existentes, que es la verdad: el gateway no registraba
autenticaciones, así que **no hay dato histórico que inventar**. Un `DEFAULT CURRENT_TIMESTAMP`
habría escrito un "último login" que nunca ocurrió, y eso es peor que un hueco: la pantalla que
lo muestre está para que alguien detecte un acceso que no hizo.

Los ``comment=`` no son adorno: sin ellos ``alembic check`` reporta drift permanente contra el
modelo, que los declara.

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "d9e0f1a2b3c4"
down_revision: Union[str, None] = "c8d9e0f1a2b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "last_login_at",
            sa.DateTime(),
            nullable=True,
            comment="Último login EXITOSO (UTC)",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "previous_login_at",
            sa.DateTime(),
            nullable=True,
            comment="Login exitoso ANTERIOR al último (UTC)",
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "last_failed_at",
            sa.DateTime(),
            nullable=True,
            comment="Último intento de login FALLIDO (UTC)",
        ),
    )


def downgrade() -> None:
    # Reversible sin condiciones: son columnas nullable sin FK ni índice, y lo único que se
    # pierde es la traza — que es dato de diagnóstico, no de negocio. `batch_alter_table` porque
    # SQLite no soporta DROP COLUMN directo en versiones viejas.
    with op.batch_alter_table("users") as batch:
        batch.drop_column("last_failed_at")
        batch.drop_column("previous_login_at")
        batch.drop_column("last_login_at")
