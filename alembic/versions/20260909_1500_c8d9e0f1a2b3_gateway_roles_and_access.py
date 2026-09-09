"""gateway_role + access_grants + user_global_capabilities — autorización del gateway

Revision ID: c8d9e0f1a2b3
Revises: b7c8d9e0f1a2
Create Date: 2026-09-09 15:00:00

Fase 0 del plan 13: la maquinaria de autorización, con **comportamiento idéntico**. Después de
esta migración el administrador existente conserva exactamente lo que tenía, así que ningún 403
es posible el día del deploy. El porqué del modelo está en el docstring de
``app/models/access_grant.py``; acá van solo las decisiones que se ven en el DDL.

EL DEFAULT DE ``gateway_role`` NUNCA ES PERMISIVO — y por eso son TRES pasos
---------------------------------------------------------------------------
La variante ingenua es de dos: agregar la columna con ``server_default='owner'`` para que las
filas viejas hereden lo que ya tenían de facto, y después bajar el default a ``'viewer'`` para
que las nuevas nazcan mínimas. **Esa variante tiene una ventana real, no teórica:** en
MySQL/MariaDB el DDL es auto-commit, así que los pasos son transacciones separadas con la app
posiblemente sirviendo en un rolling deploy — y hay un insertador concreto en ese intervalo,
``bootstrap_admin()``, que corre en el ``lifespan`` de **cada pod que arranca**. Un pod que
bootea entre los dos pasos insertaría con el default ``'owner'`` vigente.

Con tres pasos —columna nullable, ``UPDATE``, y un solo ``ALTER`` que fija NOT NULL y el
default ``'viewer'``— **nunca existe un instante en el que el default de la columna sea
permisivo**. La ventana no se mitiga: se elimina.

``CASE WHEN is_superuser THEN`` Y NO ``WHERE is_superuser = 1``
--------------------------------------------------------------
``is_superuser`` es ``Boolean``, y **PostgreSQL no coerciona ``integer`` a ``boolean``**:
``WHERE is_superuser = 1`` es ``operator does not exist: boolean = integer``. La BD de
metadatos del gateway puede ser Postgres.

POR QUÉ SE DROPEA ``is_superuser``
----------------------------------
Se escribía en tres lugares y **no se leía en ninguno** para autorizar. No era "todavía no hay
permisos": era un sistema multiusuario sin puerta, y un flag inerte con la peor forma posible
—la que hace creer que algo está protegido—. Retirarlo no rompe el contrato con la SPA, porque
``AdminOut`` es solo ``{id, username}``.

DOS TABLAS Y NO UNA
-------------------
``access_grants`` guarda la cadena ordenada (``viewer ⊆ operator ⊆ owner``) con su alcance;
``user_global_capabilities`` las ortogonales (``access_admin``, ``security_officer``). En la
misma columna, ``max({owner@dev, access_admin@global})`` no tendría valor y la resolución de
alcance del actor —que toma el máximo— dejaría de estar definida.

``scope_id`` es NOT NULL con sentinela ``0`` y un ``CHECK`` porque en los tres motores un
``UNIQUE`` no considera dos ``NULL`` iguales: con nullable, un usuario acumularía N filas
``('global', NULL)`` con roles distintos.

DOWNGRADE
---------
**Se niega a correr si hay más de un usuario o algún grant que no sea del admin original.**
Un downgrade "inverso" borraría la autorización completa del sistema de forma irreversible, y
restaurar ``is_superuser`` no restauraría ningún comportamiento porque nadie la lee. Además
exige rollback de código simultáneo: con el código de la fase 1 desplegado, el binario
consultaría tablas que ya no existen.

Los ``comment=`` de cada columna no son adorno: sin ellos ``alembic check`` reporta drift
permanente contra el modelo, que los declara.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c8d9e0f1a2b3"
down_revision: Union[str, None] = "b7c8d9e0f1a2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── Paso 1: la columna nace NULLABLE y SIN default ────────────────────── #
    op.add_column(
        "users",
        sa.Column(
            "gateway_role",
            sa.String(length=16),
            nullable=True,
            comment="Rol base del usuario en el gateway: viewer | operator | owner",
        ),
    )

    # ── Paso 2: las filas existentes heredan lo que ya tenían de facto ────── #
    # El admin sembrado nace con `is_superuser=1`, y hasta hoy CUALQUIER usuario autenticado
    # podía todo. `owner` es lo que preserva el comportamiento; el resto (que no existe hoy)
    # cae en `viewer`, que es el lado seguro.
    op.execute(
        sa.text(
            "UPDATE users SET gateway_role = "
            "CASE WHEN is_superuser THEN 'owner' ELSE 'viewer' END"
        )
    )

    # ── Paso 3: NOT NULL y default MÍNIMO, en un solo ALTER ───────────────── #
    with op.batch_alter_table("users") as batch:
        batch.alter_column(
            "gateway_role",
            existing_type=sa.String(length=16),
            nullable=False,
            server_default="viewer",
        )
    op.create_index("ix_users_gateway_role", "users", ["gateway_role"])

    # ── La cadena ordenada, con su alcance ────────────────────────────────── #
    op.create_table(
        "access_grants",
        sa.Column(
            "id", sa.Integer(), autoincrement=True, nullable=False, comment="ID único del grant"
        ),
        sa.Column(
            "user_id",
            sa.Integer(),
            nullable=False,
            comment="Usuario del gateway al que se le otorga",
        ),
        sa.Column(
            "role",
            sa.String(length=16),
            nullable=False,
            comment="Rol de la cadena: viewer | operator | owner",
        ),
        sa.Column(
            "scope_type",
            sa.String(length=16),
            nullable=False,
            comment="Eje del alcance: global | environment | server",
        ),
        sa.Column(
            "scope_id",
            sa.Integer(),
            nullable=False,
            server_default="0",
            comment="Id del entorno o servidor; 0 cuando scope_type='global' (ver docstring)",
        ),
        # `server_default` y `comment` explícitos, con el criterio que ya documenta la migración
        # de `clone_batches`: sin ellos `alembic check` reporta un `modify_default` permanente
        # contra el modelo, que los declara en `TimestampMixin`.
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
            ["user_id"], ["users.id"], name="fk_access_grants_user_id_users", ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_access_grants"),
        sa.UniqueConstraint("user_id", "scope_type", "scope_id", name="uq_access_grants_scope"),
        sa.CheckConstraint(
            "(scope_type <> 'global') OR (scope_id = 0)",
            name="ck_access_grants_global_scope_id",
        ),
        comment="Roles del gateway por usuario y alcance (plano de CONTROL)",
    )
    op.create_index("ix_access_grants_user_id", "access_grants", ["user_id"])

    # ── Las ortogonales ──────────────────────────────────────────────────── #
    op.create_table(
        "user_global_capabilities",
        sa.Column("user_id", sa.Integer(), nullable=False, comment="Usuario del gateway"),
        sa.Column(
            "capability",
            sa.String(length=32),
            nullable=False,
            comment="access_admin | security_officer",
        ),
        # `server_default` y `comment` explícitos, con el criterio que ya documenta la migración
        # de `clone_batches`: sin ellos `alembic check` reporta un `modify_default` permanente
        # contra el modelo, que los declara en `TimestampMixin`.
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
            ["user_id"],
            ["users.id"],
            name="fk_user_global_capabilities_user_id_users",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("user_id", "capability", name="pk_user_global_capabilities"),
        comment="Capacidades globales ortogonales a la cadena de roles",
    )

    # ── Comportamiento idéntico: el admin conserva TODO ──────────────────── #
    # `owner` cubre lo operativo, pero `servers.admin`, `catalogs.write` y `gateway.admin`
    # viven SOLO en las globales. Sin estas dos filas, el administrador existente perdería el
    # alta de servidores, los catálogos y la rotación de crypto — o sea la migración NO sería
    # de comportamiento idéntico, que es el requisito de esta fase.
    # `CURRENT_TIMESTAMP` va INLINE y no como parámetro bindeado: SQLite no puede bindear un
    # `sa.func.current_timestamp()` ("type not supported"), y la forma literal es portable en
    # los tres motores. Lo encontró el ciclo `upgrade` en SQLite, que es justo para lo que la
    # verificación local existe.
    for capability in ("access_admin", "security_officer"):
        op.execute(
            sa.text(
                "INSERT INTO user_global_capabilities "
                "(user_id, capability, created_at, updated_at) "
                "SELECT id, :cap, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP "
                "FROM users WHERE gateway_role = 'owner'"
            ).bindparams(cap=capability)
        )

    op.drop_column("users", "is_superuser")


def downgrade() -> None:
    bind = op.get_bind()

    # Ver el docstring: un downgrade "inverso" borra la autorización completa de forma
    # irreversible. Se niega antes de tocar nada si hay algo que perder.
    usuarios = bind.execute(sa.text("SELECT COUNT(*) FROM users")).scalar() or 0
    grants = bind.execute(sa.text("SELECT COUNT(*) FROM access_grants")).scalar() or 0
    if usuarios > 1 or grants > 0:
        raise RuntimeError(
            f"downgrade abortado: hay {usuarios} usuario(s) y {grants} grant(s). Bajar esta "
            "revisión borraría la autorización del gateway de forma irreversible, y "
            "`is_superuser` no restaura ningún comportamiento porque nadie la lee. Este "
            "downgrade exige rollback de código simultáneo y una decisión explícita."
        )

    op.add_column(
        "users",
        sa.Column(
            "is_superuser",
            sa.Boolean(),
            nullable=False,
            server_default="0",
            comment="Indica si el usuario tiene privilegios de superusuario",
        ),
    )
    # Informativamente inverso, aunque el dato sea inerte: se restaura desde el rol.
    op.execute(
        sa.text(
            "UPDATE users SET is_superuser = CASE WHEN gateway_role = 'owner' THEN 1 ELSE 0 END"
        )
    )

    op.drop_table("user_global_capabilities")
    op.drop_index("ix_access_grants_user_id", table_name="access_grants")
    op.drop_table("access_grants")
    op.drop_index("ix_users_gateway_role", table_name="users")
    op.drop_column("users", "gateway_role")
