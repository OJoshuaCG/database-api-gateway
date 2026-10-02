"""sod_exceptions — excepciones a la separación de deberes, y herencia de las combinaciones

Revision ID: f8b0d2e4a6c9
Revises: e7a9c1d3f5b8

La regla (``app/core/separation_of_duties.py``): una cuenta con ``security_officer`` no puede
tener además ``owner`` —rol base, rol por alcance o una capacidad puntual exclusiva de
``owner``— ni ``access_admin``. El escritor la rechaza (409) y el LECTOR, sin una excepción viva
que la cubra, descarta ``security_officer``.

Por eso esta migración no solo crea la tabla: **HEREDA** cada combinación que ya existe
(``reason='grandfathered'``, ``expires_at`` NULL). Sin ese paso, el día del deploy el
administrador sembrado —``owner`` + ``access_admin`` + ``security_officer``— perdería
``security_officer`` y nadie podría escribir entornos ni catálogos. No se parte la cuenta sola:
puede dejar la instalación sin ``owner`` o sin ``security_officer`` y la persona no se lo puede
arreglar (la auto-modificación está prohibida). El arranque reporta cada heredada.

``_OWNER_ONLY`` es una FOTO de ``OWNER_ONLY_CAPABILITIES`` al escribir esto, a propósito: una
migración no importa código de la app, que cambia después de ella.

**Idempotente paso a paso.** MySQL/MariaDB no tienen DDL transaccional: la tabla se crea solo si
falta, el índice solo si su nombre no existe, y la herencia inserta solo donde no hay ya una
fila viva para ``(user_id, rule)``.

**Downgrade** borra la tabla. El código sin la tabla falla cerrado (ninguna combinación
exceptuada); ``audit_log`` conserva la historia.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f8b0d2e4a6c9"
down_revision: Union[str, None] = "e7a9c1d3f5b8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "sod_exceptions"
_INDEX = "ix_sod_exceptions_user_rule"

_RULE_OWNER = "owner_security_officer"
_RULE_ACCESS_ADMIN = "access_admin_security_officer"

#: Foto de ``OWNER_ONLY_CAPABILITIES`` (owner menos operator).
_OWNER_ONLY: tuple[str, ...] = (
    "blueprints.apply",
    "blueprints.captures",
    "clones.execute",
    "collation.execute",
    "databases.drop",
    "engine_users.credentials",
    "engine_users.drop",
    "engine_users.secrets",
    "exports.download",
    "schema_diff.execute",
    "sql_console.execute",
)


def _existe(bind, tabla: str = _TABLE) -> bool:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return sa.inspect(bind).has_table(tabla)


def _indices(bind) -> set[str]:
    return {i["name"] for i in sa.inspect(bind).get_indexes(_TABLE)}


def _crear_tabla() -> None:
    op.create_table(
        _TABLE,
        sa.Column(
            "id", sa.Integer(), autoincrement=True, nullable=False,
            comment="ID único de la excepción",
        ),
        sa.Column(
            "user_id", sa.Integer(), nullable=False,
            comment="Usuario del gateway cubierto por la excepción",
        ),
        sa.Column(
            "rule", sa.String(length=64), nullable=False,
            comment="Regla de separación de deberes que se exceptúa",
        ),
        sa.Column(
            "reason", sa.String(length=500), nullable=False,
            comment="Motivo declarado ('grandfathered' si es heredada)",
        ),
        sa.Column(
            "requested_by", sa.Integer(), nullable=True,
            comment="Administrador que pidió el override (NULL si es heredada)",
        ),
        sa.Column(
            "approved_by", sa.Integer(), nullable=True,
            comment="Segundo aprobador (NULL hasta que exista el flujo de aprobación)",
        ),
        sa.Column(
            "expires_at", sa.DateTime(), nullable=True,
            comment="Vencimiento (UTC); NULL = heredada, sin vencimiento",
        ),
        sa.Column(
            "closed_at", sa.DateTime(), nullable=True,
            comment="Cierre (UTC): la cuenta dejó de violar la regla",
        ),
        sa.Column(
            "closed_reason", sa.String(length=32), nullable=True,
            comment="Motivo del cierre (resolved)",
        ),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False, comment="Fecha y hora de creación del registro",
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("(CURRENT_TIMESTAMP)"),
            nullable=False, comment="Fecha y hora de última actualización del registro",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name="fk_sod_exceptions_user_id_users",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["requested_by"], ["users.id"], name="fk_sod_exceptions_requested_by_users",
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["approved_by"], ["users.id"], name="fk_sod_exceptions_approved_by_users",
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_sod_exceptions"),
        sa.CheckConstraint(
            f"rule IN ('{_RULE_OWNER}', '{_RULE_ACCESS_ADMIN}')", name="ck_sod_exceptions_rule"
        ),
        comment="Excepciones a la separación de deberes del gateway (plano de CONTROL)",
    )


def _ids(bind, sql: str, params: dict | None = None) -> set[int]:
    return {int(r[0]) for r in bind.execute(sa.text(sql), params or {}).fetchall()}


def _violadores(bind) -> dict[str, set[int]]:
    """``{regla: {user_id}}`` de las combinaciones que existen HOY."""
    oficiales = _ids(
        bind,
        "SELECT user_id FROM user_global_capabilities WHERE capability = 'security_officer'",
    )
    if not oficiales:
        return {_RULE_OWNER: set(), _RULE_ACCESS_ADMIN: set()}

    owner = _ids(bind, "SELECT id FROM users WHERE gateway_role = 'owner'")
    owner |= _ids(
        bind,
        "SELECT user_id FROM access_grants WHERE role = 'owner' AND scope_type <> 'global'",
    )
    if _existe(bind, "capability_grants"):
        marcadores = ", ".join(f":c{i}" for i in range(len(_OWNER_ONLY)))
        owner |= _ids(
            bind,
            "SELECT user_id FROM capability_grants "
            f"WHERE status IN ('pending', 'active') AND capability IN ({marcadores})",
            {f"c{i}": c for i, c in enumerate(_OWNER_ONLY)},
        )
    access_admin = _ids(
        bind,
        "SELECT user_id FROM user_global_capabilities WHERE capability = 'access_admin'",
    )
    return {
        _RULE_OWNER: oficiales & owner,
        _RULE_ACCESS_ADMIN: oficiales & access_admin,
    }


def _heredar(bind) -> None:
    """Una fila ``grandfathered`` por ``(usuario, regla)`` violada sin fila viva. Idempotente."""
    for regla, usuarios in _violadores(bind).items():
        for uid in sorted(usuarios):
            viva = bind.execute(
                sa.text(
                    "SELECT 1 FROM sod_exceptions WHERE user_id = :u AND rule = :r "
                    "AND closed_at IS NULL AND expires_at IS NULL"
                ),
                {"u": uid, "r": regla},
            ).first()
            if viva:
                continue
            bind.execute(
                sa.text(
                    "INSERT INTO sod_exceptions (user_id, rule, reason, created_at, updated_at) "
                    "VALUES (:u, :r, 'grandfathered', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
                ),
                {"u": uid, "r": regla},
            )


def upgrade() -> None:
    bind = op.get_bind()
    if not _existe(bind):
        _crear_tabla()
    if _INDEX not in _indices(bind):
        op.create_index(_INDEX, _TABLE, ["user_id", "rule"])
    _heredar(bind)


def downgrade() -> None:
    bind = op.get_bind()
    if not _existe(bind):
        return
    if _INDEX in _indices(bind):
        op.drop_index(_INDEX, table_name=_TABLE)
    op.drop_table(_TABLE)
