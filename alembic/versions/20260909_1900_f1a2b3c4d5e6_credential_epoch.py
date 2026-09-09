"""credential_epoch en users: el mecanismo de un solo uso del token de invitación

El token de invitación tiene 48 h de TTL, y sin un contador de credenciales **se puede
reutilizar**: quien lo tenga podría reescribir la password de la cuenta las veces que quiera
dentro de esa ventana. Firmando el token sobre ``(user_id, credential_epoch)`` y subiendo el
contador al fijar la password, el token deja de validar en cuanto se usa.

Nace en 0 para las filas existentes, que es correcto: no había tokens emitidos que invalidar.

Los ``comment=`` no son adorno: sin ellos ``alembic check`` reporta drift permanente contra el
modelo, que los declara.

Revision ID: f1a2b3c4d5e6
Revises: e0f1a2b3c4d5
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f1a2b3c4d5e6"
down_revision: Union[str, None] = "e0f1a2b3c4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column(
            "credential_epoch",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Sube al fijar una password; invalida los tokens de invitación anteriores",
        ),
    )


def downgrade() -> None:
    # Bajar esto **invalida el mecanismo de un solo uso**, no solo la columna: cualquier token
    # de invitación vivo pasa a ser reutilizable durante lo que le quede de TTL. Si hay que
    # bajar con invitaciones pendientes, revocarlas primero (subir el epoch no alcanza si la
    # columna no existe).
    with op.batch_alter_table("users") as batch:
        batch.drop_column("credential_epoch")
