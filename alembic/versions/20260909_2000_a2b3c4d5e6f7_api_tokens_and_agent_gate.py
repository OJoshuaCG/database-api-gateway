"""api_tokens + las columnas del gate de agentes

UNA sola revisión para la tabla y las cinco columnas, porque son **una sola decisión**: sin el
gate, la tabla emite credenciales que alcanzan todo; sin la tabla, las columnas no tienen quién
las lea. Es la regla de "cero flags inertes" aplicada a una migración.

LOS DEFAULTS SON EL CONTROL, NO UNA COMODIDAD
---------------------------------------------
``allows_agent_access``, ``agent_access_allowed`` y ``agent_access_blocked`` nacen en **false**, y
la asimetría está puesta acá —en el DDL— y no solo en el código del gate. Con default permisivo,
el momento en que alguien habilita el MCP dejaría legible **todo lo ya clasificado** sin que nadie
lo haya decidido, y las bases que se creen después nacerían abiertas.

Y son DOS ejes por base, no uno: ``agent_access_allowed`` es el **opt-in** (el que decide el
alcance) y ``agent_access_blocked`` el **veto de emergencia** que gana sobre el permiso. Con solo
el veto, habilitar un entorno abriría de golpe todas sus bases y "activar una" obligaría a ir a
bloquear N a mano — el default-deny existiría una sola vez y de ahí en adelante el sistema sería
default-allow.

``project_id`` de ``api_tokens`` es NOT NULL desde el principio: un token sin proyecto no tiene
ninguna base alcanzable, así que lo único que un NULL podría significar es "token global", que es
justo el radio de explosión que este diseño existe para no tener. La tabla es nueva, así que el
caso **no existe nunca**.

``actor_type`` de ``audit_log`` nace en ``'admin'`` para las filas existentes, y eso **es la
verdad**: hasta hoy no había otra clase de actor.

Los ``comment=`` no son adorno: sin ellos ``alembic check`` reporta drift permanente contra el
modelo, que los declara.

Revision ID: a2b3c4d5e6f7
Revises: f1a2b3c4d5e6
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a2b3c4d5e6f7"
down_revision: Union[str, None] = "f1a2b3c4d5e6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "api_tokens",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, comment="ID único del token"),
        sa.Column(
            "token_id",
            sa.String(length=24),
            nullable=False,
            comment="Identificador público del token: la parte indexable de 'dbgw.<id>.<secreto>'",
        ),
        sa.Column(
            "secret_hmac",
            sa.String(length=64),
            nullable=False,
            comment="HMAC-SHA256(pepper, secreto) en hex. El secreto NUNCA se guarda",
        ),
        sa.Column(
            "name",
            sa.String(length=128),
            nullable=False,
            comment=(
                "Para qué máquina o repo es. La revocación granular depende de que sea uno por uno"
            ),
        ),
        sa.Column(
            "scopes",
            sa.String(length=255),
            nullable=False,
            comment="Vocabulario cerrado separado por comas. En v1 solo 'blueprints.read'",
        ),
        sa.Column(
            "project_id",
            sa.Integer(),
            nullable=False,
            comment="Proyecto que el token alcanza. NOT NULL: un token sin proyecto sería global",
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(),
            nullable=False,
            comment=(
                "NOT NULL: no hay tokens perpetuos. Un token de agente vive en un .mcp.json del "
                "repo de otra gente, o sea es la credencial con más chance de terminar commiteada"
            ),
        ),
        sa.Column(
            "last_used_at",
            sa.DateTime(),
            nullable=True,
            comment=(
                "Último uso (UTC), con escritura amortiguada: un UPDATE por request es gratis de más"
            ),
        ),
        sa.Column(
            "revoked_at",
            sa.DateTime(),
            nullable=True,
            comment="Cuándo se revocó (UTC). NULL = vivo",
        ),
        sa.Column(
            "created_by_admin_id",
            sa.Integer(),
            nullable=True,
            comment="Quién lo emitió. Sin FK: la fila sobrevive al usuario",
        ),
        sa.Column("note", sa.Text(), nullable=True, comment="Nota libre del operador"),
        sa.Column(
            "created_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
            comment="Fecha de creación del registro",
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(),
            server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False,
            comment="Fecha de última actualización del registro",
        ),
        # RESTRICT y no CASCADE: borrar un proyecto no puede destruir en silencio la evidencia
        # de qué tokens lo alcanzaban.
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        comment="Tokens de agente (servidor MCP), acotados a un proyecto",
    )
    # UN índice ÚNICO, no una constraint más un índice común: `unique=True, index=True` en el
    # modelo produce exactamente esto, y la diferencia la encontró `alembic check` — que es
    # justo para lo que existe.
    op.create_index("ix_api_tokens_token_id", "api_tokens", ["token_id"], unique=True)
    op.create_index("ix_api_tokens_project_id", "api_tokens", ["project_id"])
    op.create_index("ix_api_tokens_revoked_at", "api_tokens", ["revoked_at"])

    op.add_column(
        "environments",
        sa.Column(
            "allows_agent_access",
            sa.Boolean(),
            nullable=False,
            server_default="0",
            comment=(
                "Si los agentes (MCP) pueden inspeccionar bases de este entorno. Nace en false"
            ),
        ),
    )
    op.add_column(
        "managed_databases",
        sa.Column(
            "agent_access_allowed",
            sa.Boolean(),
            nullable=False,
            server_default="0",
            comment=(
                "Opt-in POR BASE para agentes (MCP). Nace en false: cada activación es explícita"
            ),
        ),
    )
    op.add_column(
        "managed_databases",
        sa.Column(
            "agent_access_blocked",
            sa.Boolean(),
            nullable=False,
            server_default="0",
            comment=(
                "Veto de emergencia para agentes. Gana sobre agent_access_allowed. Sin override"
            ),
        ),
    )
    op.add_column(
        "audit_log",
        sa.Column(
            "actor_type",
            sa.String(length=16),
            nullable=False,
            server_default="admin",
            comment="admin | api_token. Las filas históricas son todas 'admin', que es la verdad",
        ),
    )
    op.add_column(
        "audit_log",
        sa.Column(
            "api_token_id",
            sa.Integer(),
            nullable=True,
            comment="Token de agente que originó la operación, si el actor fue un token",
        ),
    )
    op.create_index("ix_audit_log_api_token_id", "audit_log", ["api_token_id"])


def downgrade() -> None:
    # Bajar esto **borra los tokens emitidos**, no solo su tabla: todo agente configurado deja
    # de autenticarse y no hay forma de recrear los secretos (solo se guarda su HMAC). Y las
    # columnas del gate se van con su default seguro, así que volver a subir deja todo negando
    # otra vez — que es el lado correcto, pero exige rehacer las activaciones base por base.
    op.drop_index("ix_audit_log_api_token_id", table_name="audit_log")
    with op.batch_alter_table("audit_log") as batch:
        batch.drop_column("api_token_id")
        batch.drop_column("actor_type")
    with op.batch_alter_table("managed_databases") as batch:
        batch.drop_column("agent_access_blocked")
        batch.drop_column("agent_access_allowed")
    with op.batch_alter_table("environments") as batch:
        batch.drop_column("allows_agent_access")
    op.drop_index("ix_api_tokens_revoked_at", table_name="api_tokens")
    op.drop_index("ix_api_tokens_project_id", table_name="api_tokens")
    op.drop_index("ix_api_tokens_token_id", table_name="api_tokens")
    op.drop_table("api_tokens")
