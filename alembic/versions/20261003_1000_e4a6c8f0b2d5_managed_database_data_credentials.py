"""managed_database_data_credentials — credencial de DATOS SELECT-only por base (MCP)

Revision ID: e4a6c8f0b2d5
Revises: d3f5a7b9c1e4

Tabla ADITIVA 1:1 con ``managed_databases``: nace vacía, así que ningún comportamiento existente
cambia el día del deploy. Incluye desde ya las columnas del opt-in de datos (``data_access_*``)
con default CERRADO (design D13: una sola migración, no una segunda en la slice del opt-in).

Las FKs van SIN nombre a mano: los nombres de la convención superan los 64 caracteres de MySQL
(``fk_managed_database_data_credentials_managed_database_id_managed_databases``) y el motor los
asigna solo. Por eso el downgrade borra la TABLA entera y nunca llama ``drop_constraint`` con un
nombre escrito a mano. El ``UNIQUE`` sí lleva el nombre de la convención (cabe en 64).

**Idempotente paso a paso.** MySQL/MariaDB no tienen DDL transaccional: la tabla se crea solo si
falta, con un inspector NUEVO (cachea y el esquema cambia entre pasos). ``CREATE TABLE`` es una
sola sentencia con el ``UNIQUE`` y las FKs adentro, así que no hay estado intermedio con la tabla
sin su unicidad.

**Downgrade** borra la tabla si existe. Revocar antes las cuentas del motor
(``DELETE .../data-credential``): borrar la tabla pierde la contraseña cifrada y deja huérfanas
las cuentas ``mcp_d_<id>`` del motor.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e4a6c8f0b2d5"
down_revision: Union[str, None] = "d3f5a7b9c1e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "managed_database_data_credentials"


def _existe(bind) -> bool:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return sa.inspect(bind).has_table(_TABLE)


def upgrade() -> None:
    bind = op.get_bind()
    if _existe(bind):
        return
    op.create_table(
        _TABLE,
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
            comment="ID único de la credencial de datos",
        ),
        sa.Column(
            "managed_database_id",
            sa.Integer(),
            nullable=False,
            comment="Base gestionada a la que pertenece (1:1). CASCADE: muere con la base",
        ),
        sa.Column(
            "username",
            sa.String(length=128),
            nullable=False,
            comment="Cuenta del motor (mcp_d_<id>)",
        ),
        sa.Column(
            "account_host",
            sa.String(length=255),
            nullable=False,
            server_default="%",
            comment="Host del grantee (MySQL/MariaDB); se ignora en PostgreSQL",
        ),
        sa.Column(
            "password_encrypted",
            sa.Text(),
            nullable=False,
            comment="Password CIFRADO (Fernet). Nunca se expone ni se loguea",
        ),
        sa.Column(
            "verified_at",
            sa.DateTime(),
            nullable=True,
            comment="Última sonda exitosa sobre ESTA base. NULL = sin verificar (no usable)",
        ),
        sa.Column(
            "probed_at",
            sa.DateTime(),
            nullable=True,
            comment="Última vez que se corrió la sonda (pase o falle)",
        ),
        sa.Column(
            "probe_violations",
            sa.Text(),
            nullable=True,
            comment="JSON con códigos cortos de la última sonda; sin texto de grants",
        ),
        sa.Column(
            "probe_warnings",
            sa.Text(),
            nullable=True,
            comment="JSON con advertencias no bloqueantes de la última sonda",
        ),
        sa.Column(
            "data_access_allowed",
            sa.Boolean(),
            nullable=False,
            server_default="0",
            comment="Opt-in de LECTURA DE DATOS por base para agentes. Nace cerrado",
        ),
        sa.Column(
            "data_access_requested_by_id",
            sa.Integer(),
            nullable=True,
            comment="Usuario del gateway que pidió abrir el acceso a datos",
        ),
        sa.Column(
            "data_access_approved_by_id",
            sa.Integer(),
            nullable=True,
            comment="Segundo aprobador del acceso a datos (distinto del solicitante)",
        ),
        sa.Column(
            "data_access_approved_at",
            sa.DateTime(),
            nullable=True,
            comment="Cuándo se aprobó el acceso a datos",
        ),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
            comment="Fecha y hora de creación del registro",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
            comment="Fecha y hora de última actualización del registro",
        ),
        sa.ForeignKeyConstraint(
            ["managed_database_id"], ["managed_databases.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["data_access_requested_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.ForeignKeyConstraint(
            ["data_access_approved_by_id"], ["users.id"], ondelete="SET NULL"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_managed_database_data_credentials"),
        sa.UniqueConstraint(
            "managed_database_id",
            name="uq_managed_database_data_credentials_managed_database_id",
        ),
        comment="Credencial de datos SELECT-only por base gestionada (1:1, cifrada)",
    )


def downgrade() -> None:
    bind = op.get_bind()
    if _existe(bind):
        op.drop_table(_TABLE)
