"""integration_tokens (+ listas de servidores y blueprints) y audit_log.integration_token_id

Revision ID: b7d9f1a3c5e8
Revises: f5b7d9e1a3c6

UNA sola revisión para las tres tablas y la columna de auditoría, porque son una sola decisión:
la tabla de tokens sin sus listas de permitidos emitiría credenciales sin destino acotado, y la
columna de auditoría sin la tabla no tendría a quién atribuir.

- ``integration_tokens``: la credencial bearer de la API de integración, atada a un usuario
  emisor. ``expires_at`` NOT NULL (no hay tokens perpetuos) y ``created_by_admin_id`` /
  ``revoked_by_admin_id`` SIN FK (la fila sobrevive al usuario).
- ``integration_token_servers`` / ``integration_token_blueprints``: listas de permitidos con FK
  ``CASCADE`` y PK compuesta.
- ``audit_log.integration_token_id``: qué token de integración originó la fila. Columna propia y
  no ``api_token_id``: son dos tablas con PK independientes.
- El COMMENT de ``audit_log.actor_type`` pasa a mencionar ``integration`` (sin él ``alembic check``
  reporta drift contra el modelo). El valor nuevo cabe en ``String(16)``: no cambia el tipo.

**Idempotente paso a paso** (MySQL/MariaDB no tienen DDL transaccional: una migración que muere a
mitad deja aplicado lo que ya corrió con ``alembic_version`` atrasado, y el reintento no puede
chocar consigo mismo). Cada tabla, columna e índice se crea solo si falta, con un inspector NUEVO
en cada consulta (el inspector cachea y entre pasos el esquema cambia).

**Downgrade** introspectivo: borra solo lo que existe y NUNCA nombra una constraint a mano (los
nombres de la convención superan el límite del motor y se guardan truncados con un hash). Bajar
esto **borra los tokens de integración emitidos**: toda integración configurada deja de
autenticarse y no hay forma de recrear los secretos (solo se guarda su HMAC).
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7d9f1a3c5e8"
down_revision: Union[str, None] = "f5b7d9e1a3c6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TOKENS_TABLE = "integration_tokens"
_SERVERS_TABLE = "integration_token_servers"
_BLUEPRINTS_TABLE = "integration_token_blueprints"
_AUDIT_TABLE = "audit_log"
_AUDIT_COLUMN = "integration_token_id"
_AUDIT_INDEX = "ix_audit_log_integration_token_id"

_TOKEN_PUBLIC_ID_LENGTH = 24
_SECRET_HMAC_HEX_LENGTH = 64
_SCOPES_LENGTH = 512
_ACTOR_TYPE_LENGTH = 16

_ACTOR_TYPE_COMMENT_NEW = (
    "admin | api_token | integration. Las filas históricas son todas 'admin', que es la verdad"
)
_ACTOR_TYPE_COMMENT_OLD = "admin | api_token. Las filas históricas son todas 'admin', que es la verdad"


def _tables(bind) -> set[str]:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return set(sa.inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in sa.inspect(bind).get_columns(table)}


def _indexes(bind, table: str) -> set[str]:
    return {index["name"] for index in sa.inspect(bind).get_indexes(table)}


def _create_index_if_missing(
    bind, index_name: str, table: str, columns: list[str], *, unique: bool = False
) -> None:
    if index_name not in _indexes(bind, table):
        op.create_index(index_name, table, columns, unique=unique)


def _created_at_column() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.DateTime(),
        server_default=sa.text("(CURRENT_TIMESTAMP)"),
        nullable=False,
        comment="Fecha y hora de creación del registro",
    )


def _updated_at_column() -> sa.Column:
    return sa.Column(
        "updated_at",
        sa.DateTime(),
        server_default=sa.text("(CURRENT_TIMESTAMP)"),
        nullable=False,
        comment="Fecha y hora de última actualización del registro",
    )


def _create_tokens_table() -> None:
    op.create_table(
        _TOKENS_TABLE,
        sa.Column(
            "id",
            sa.Integer(),
            autoincrement=True,
            nullable=False,
            comment="ID único del token de integración",
        ),
        sa.Column(
            "token_id",
            sa.String(length=_TOKEN_PUBLIC_ID_LENGTH),
            nullable=False,
            comment=(
                "Identificador público del token: la parte indexable de 'datumint.<id>.<secreto>'"
            ),
        ),
        sa.Column(
            "secret_hmac",
            sa.String(length=_SECRET_HMAC_HEX_LENGTH),
            nullable=False,
            comment="HMAC-SHA256(pepper de integración, secreto) en hex. El secreto NUNCA se guarda",
        ),
        sa.Column(
            "name",
            sa.String(length=128),
            nullable=False,
            comment=(
                "Para qué proyecto o pipeline es. La revocación granular depende de que sea uno "
                "por uno"
            ),
        ),
        sa.Column(
            "scopes",
            sa.String(length=_SCOPES_LENGTH),
            nullable=False,
            comment=(
                "Scopes de integración separados por comas (vocabulario cerrado de "
                "integration_scope_catalog). Lo efectivo es esta lista recortada por el rol "
                "actual del emisor"
            ),
        ),
        sa.Column(
            "created_by_admin_id",
            sa.Integer(),
            nullable=False,
            comment=(
                "Usuario del gateway que emitió el token y cuyo rol ejerce. Sin FK: la fila "
                "sobrevive al usuario"
            ),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(),
            nullable=True,
            comment=(
                "UTC. NULL = token sin expiración: solo se emite si "
                "INTEGRATION_ALLOW_NON_EXPIRING_TOKENS está encendido y nunca con scopes "
                "destructivos"
            ),
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(),
            nullable=True,
            comment="Último uso (UTC), con escritura amortiguada a una por minuto como máximo",
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(),
            nullable=True,
            comment="Cuándo se revocó (UTC). NULL = vivo",
        ),
        sa.Column(
            "revoked_by_admin_id",
            sa.Integer(),
            nullable=True,
            comment="Usuario del gateway que revocó el token. NULL = no revocado. Sin FK",
        ),
        sa.Column("note", sa.Text(), nullable=True, comment="Nota libre del operador"),
        _created_at_column(),
        _updated_at_column(),
        sa.PrimaryKeyConstraint("id"),
        comment=(
            "Tokens bearer de la API de integración. Atados a un usuario emisor: ejercen su "
            "rol real recortado por los scopes del token"
        ),
    )


def _create_servers_table() -> None:
    op.create_table(
        _SERVERS_TABLE,
        sa.Column(
            "token_pk",
            sa.Integer(),
            nullable=False,
            comment="Token de integración al que pertenece la entrada",
        ),
        sa.Column(
            "server_id",
            sa.Integer(),
            nullable=False,
            comment="Servidor permitido. Si el servidor se borra, la entrada se borra",
        ),
        sa.ForeignKeyConstraint(["token_pk"], [f"{_TOKENS_TABLE}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["server_id"], ["servers.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_pk", "server_id"),
        comment=(
            "Servidores sobre los que un token de integración puede operar (lista de permitidos)"
        ),
    )


def _create_blueprints_table() -> None:
    op.create_table(
        _BLUEPRINTS_TABLE,
        sa.Column(
            "token_pk",
            sa.Integer(),
            nullable=False,
            comment="Token de integración al que pertenece la entrada",
        ),
        sa.Column(
            "model_id",
            sa.Integer(),
            nullable=False,
            comment=(
                "Blueprint (database_models.id) permitido. Si el blueprint se borra, la entrada "
                "se borra"
            ),
        ),
        sa.ForeignKeyConstraint(["token_pk"], [f"{_TOKENS_TABLE}.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["model_id"], ["database_models.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("token_pk", "model_id"),
        comment=(
            "Blueprints que un token de integración puede asignar o aplicar. Vacía = sin "
            "restricción, salvo en los tokens con scopes destructivos"
        ),
    )


def upgrade() -> None:
    bind = op.get_bind()

    if _TOKENS_TABLE not in _tables(bind):
        _create_tokens_table()
    # UN índice ÚNICO sobre token_id (no una constraint más un índice común): es lo que produce
    # `unique=True, index=True` en el modelo.
    _create_index_if_missing(
        bind, "ix_integration_tokens_token_id", _TOKENS_TABLE, ["token_id"], unique=True
    )
    _create_index_if_missing(
        bind,
        "ix_integration_tokens_created_by_admin_id",
        _TOKENS_TABLE,
        ["created_by_admin_id"],
    )
    _create_index_if_missing(
        bind, "ix_integration_tokens_revoked_at", _TOKENS_TABLE, ["revoked_at"]
    )

    # Las tablas hijas van DESPUÉS: sus FK apuntan a `integration_tokens`.
    if _SERVERS_TABLE not in _tables(bind):
        _create_servers_table()
    if _BLUEPRINTS_TABLE not in _tables(bind):
        _create_blueprints_table()

    if _AUDIT_COLUMN not in _columns(bind, _AUDIT_TABLE):
        op.add_column(
            _AUDIT_TABLE,
            sa.Column(
                _AUDIT_COLUMN,
                sa.Integer(),
                nullable=True,
                comment=(
                    "Token de integración que originó la operación, si el actor fue uno. Solo el id"
                ),
            ),
        )
    _create_index_if_missing(bind, _AUDIT_INDEX, _AUDIT_TABLE, [_AUDIT_COLUMN])

    # Reafirmar el COMMENT es idempotente por naturaleza. MySQL/MariaDB reescriben la definición
    # completa de la columna, así que hay que repetir tipo, nulidad y default tal como están.
    op.alter_column(
        _AUDIT_TABLE,
        "actor_type",
        existing_type=sa.String(length=_ACTOR_TYPE_LENGTH),
        existing_nullable=False,
        existing_server_default="admin",
        comment=_ACTOR_TYPE_COMMENT_NEW,
    )


def downgrade() -> None:
    bind = op.get_bind()

    op.alter_column(
        _AUDIT_TABLE,
        "actor_type",
        existing_type=sa.String(length=_ACTOR_TYPE_LENGTH),
        existing_nullable=False,
        existing_server_default="admin",
        comment=_ACTOR_TYPE_COMMENT_OLD,
    )

    if _AUDIT_INDEX in _indexes(bind, _AUDIT_TABLE):
        op.drop_index(_AUDIT_INDEX, table_name=_AUDIT_TABLE)
    if _AUDIT_COLUMN in _columns(bind, _AUDIT_TABLE):
        op.drop_column(_AUDIT_TABLE, _AUDIT_COLUMN)

    # Hijas primero: sus FK dependen de `integration_tokens`. `drop_table` se lleva sus propias
    # constraints e índices, así que no hace falta (ni conviene) nombrar ninguna a mano.
    for table in (_BLUEPRINTS_TABLE, _SERVERS_TABLE, _TOKENS_TABLE):
        if table in _tables(bind):
            op.drop_table(table)
