"""collation_conversion_batches + pertenencia y totales en collation_conversions

Revision ID: a6b7c8d9e0f1
Revises: f5a6b7c8d9e0
Create Date: 2026-08-24 18:00:00

Crea la tabla de LOTES de conversión de collation (una conversión por cada BD de un
blueprint, en un solo gesto del operador) y agrega a ``collation_conversions`` la pertenencia
al lote más los totales congelados del preview.

Por qué una tabla y no un ``batch_id`` suelto
---------------------------------------------
Ver el docstring de ``app/models/collation_conversion_batch.py``. En resumen: ``capped``, la
autoría (el worker NO hereda ContextVars de la request) y el desenlace del lote son estado
DEL LOTE, no de un job, y sin fila donde vivir habría que derivarlos con una carrera entre
workers.

Dos trampas de esta migración, ambas ya documentadas en el repo
---------------------------------------------------------------
1. **El orden del ``downgrade()`` es FK → índice → columna.** ``batch_id`` es una FK CON
   índice: en MySQL/MariaDB un ``drop_index`` sobre el índice que respalda una FK viva falla
   con *"needed in a foreign key constraint"*, y un ``drop_column`` sobre una columna con FK
   viva falla con el errno 1828. Molde exacto:
   ``20260822_1200_e4f5a6b7c8d9_environments_and_managed_db_link.py``. Es el mismo patrón que
   ya estaba roto en migraciones anteriores del repo y que se corrigió al verificarlas contra
   MariaDB real.
2. **SQLite necesita ``batch_alter_table``**: no tiene ``ALTER TABLE ... DROP CONSTRAINT`` y
   recrea la tabla entera. La FK vive en el ``CREATE TABLE``, así que el recreate sin la
   columna se la lleva; el índice, en cambio, hay que soltarlo DENTRO del mismo batch o
   intenta recrearse sobre una columna que ya no existe.

Los ``comment=`` de cada columna no son adorno: sin ellos ``alembic check`` reporta drift
permanente contra el modelo, que los declara.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "a6b7c8d9e0f1"
down_revision: Union[str, None] = "f5a6b7c8d9e0"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "collation_conversion_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, comment="ID único del lote"),
        sa.Column(
            "model_id",
            sa.Integer(),
            nullable=False,
            comment="Blueprint cuyas BDs se convierten",
        ),
        sa.Column(
            "target_charset",
            sa.String(length=64),
            nullable=True,
            comment="Charset objetivo (NULL en PostgreSQL: no tiene charset por tabla)",
        ),
        sa.Column(
            "target_collation",
            sa.String(length=100),
            nullable=False,
            comment="Collation objetivo del lote",
        ),
        sa.Column(
            "total",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment="Cantidad de BDs efectivamente incluidas en el lote",
        ),
        sa.Column(
            "max_databases",
            sa.Integer(),
            server_default="10",
            nullable=False,
            comment="Tope pedido al planear",
        ),
        sa.Column(
            "capped",
            sa.Boolean(),
            server_default="0",
            nullable=False,
            comment=(
                "True si el tope dejó BDs elegibles fuera. Se PERSISTE (y no solo se devuelve "
                "al planear) porque si no el polling no puede reportar el recorte y el "
                "operador cree que se convirtió todo el blueprint"
            ),
        ),
        sa.Column(
            "confirm_token",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Hash del lote RESUELTO (conjunto de BDs + objetivo + planes por BD). Se "
                "recomputa server-side al ejecutar: cualquier cambio de inventario, de "
                "objetivo o del conjunto de BDs lo invalida"
            ),
        ),
        sa.Column(
            "expires_at",
            sa.DateTime(),
            nullable=False,
            comment="Vencimiento del plan del lote (TTL)",
        ),
        sa.Column(
            "created_by_admin_id",
            sa.Integer(),
            nullable=True,
            comment="Admin que creó el lote (sin FK: historial desacoplado)",
        ),
        sa.Column(
            "created_by_username",
            sa.String(length=128),
            nullable=True,
            comment="Username del admin (mismo ancho que audit_log)",
        ),
        sa.Column(
            "origin_request_id",
            sa.String(length=32),
            nullable=True,
            comment=(
                "Request ID que autorizó el lote, para correlacionar la auditoría del worker"
            ),
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="pending",
            nullable=False,
            comment="pending | running | done | failed | canceled",
        ),
        sa.Column(
            "error",
            sa.String(length=500),
            nullable=True,
            comment="Motivo acotado del fallo del lote (nunca el mensaje crudo del motor)",
        ),
        sa.Column(
            "blueprint_version_id",
            sa.Integer(),
            nullable=True,
            comment=(
                "Versión de blueprint creada DESPUÉS del lote como contabilidad de la "
                "conversión. Se stampea en las N BDs, nunca se aplica. SET NULL: borrar la "
                "versión no debe borrar el historial del lote"
            ),
        ),
        sa.Column(
            "started_at",
            sa.DateTime(),
            nullable=True,
            comment="Cuándo arrancó el primer job del lote",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(),
            nullable=True,
            comment="Cuándo terminó el último job del lote",
        ),
        # ``server_default`` + ``comment`` EXACTOS a los de ``TimestampMixin``: sin ellos
        # ``alembic check`` reporta un ``modify_default`` permanente contra el modelo.
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
            ["model_id"],
            ["database_models.id"],
            name=op.f("fk_ccb_model_id_database_models"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["blueprint_version_id"],
            ["model_migrations.id"],
            name=op.f("fk_ccb_blueprint_version_id_model_migrations"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Lote de conversiones de collation sobre las BDs de un blueprint",
    )
    op.create_index(
        op.f("ix_collation_conversion_batches_model_id"),
        "collation_conversion_batches",
        ["model_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_collation_conversion_batches_blueprint_version_id"),
        "collation_conversion_batches",
        ["blueprint_version_id"],
        unique=False,
    )

    # ---- Pertenencia y totales en el job -------------------------------------------- #
    op.add_column(
        "collation_conversions",
        sa.Column(
            "batch_id",
            sa.Integer(),
            nullable=True,
            comment="Lote al que pertenece este job (NULL si es una conversión suelta)",
        ),
    )
    op.add_column(
        "collation_conversions",
        sa.Column(
            "batch_seq",
            sa.Integer(),
            nullable=True,
            comment=(
                "Posición 1-based dentro del lote. NO es cosmético: los jobs corren EN SERIE "
                "(COLLATION_CONVERSION_MAX_WORKERS default 1) y sin esto la UI no puede decir "
                "'la 4 de 12' ni ordenar la tabla de forma estable"
            ),
        ),
    )
    op.add_column(
        "collation_conversions",
        sa.Column(
            "tables_total",
            sa.Integer(),
            nullable=True,
            comment="Tablas a convertir según el plan confirmado",
        ),
    )
    op.add_column(
        "collation_conversions",
        sa.Column(
            "objects_total",
            sa.Integer(),
            nullable=True,
            comment="Objetos a recrear según el plan confirmado",
        ),
    )

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("collation_conversions", schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f("ix_collation_conversions_batch_id"), ["batch_id"], unique=False
            )
            batch_op.create_foreign_key(
                batch_op.f("fk_cc_batch_id_collation_conversion_batches"),
                "collation_conversion_batches",
                ["batch_id"],
                ["id"],
                ondelete="SET NULL",
            )
    else:
        op.create_index(
            op.f("ix_collation_conversions_batch_id"),
            "collation_conversions",
            ["batch_id"],
            unique=False,
        )
        op.create_foreign_key(
            op.f("fk_cc_batch_id_collation_conversion_batches"),
            "collation_conversions",
            "collation_conversion_batches",
            ["batch_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        # El recreate refleja la tabla resultante: el índice se suelta DENTRO del mismo batch
        # en que se va la columna, o intenta re-crearse sobre una columna inexistente. La FK
        # no se suelta aparte: en SQLite vive en el CREATE TABLE y el recreate sin la columna
        # la deja fuera.
        with op.batch_alter_table("collation_conversions", schema=None) as batch_op:
            batch_op.drop_index(batch_op.f("ix_collation_conversions_batch_id"))
            batch_op.drop_column("objects_total")
            batch_op.drop_column("tables_total")
            batch_op.drop_column("batch_seq")
            batch_op.drop_column("batch_id")
    else:
        # ORDEN OBLIGATORIO: la FK ANTES del índice que la respalda (si no, MySQL/MariaDB
        # rechazan el drop_index con "needed in a foreign key constraint"), y el índice antes
        # de la columna (drop_column con FK viva da el errno 1828).
        op.drop_constraint(
            op.f("fk_cc_batch_id_collation_conversion_batches"),
            "collation_conversions",
            type_="foreignkey",
        )
        op.drop_index(
            op.f("ix_collation_conversions_batch_id"), table_name="collation_conversions"
        )
        op.drop_column("collation_conversions", "objects_total")
        op.drop_column("collation_conversions", "tables_total")
        op.drop_column("collation_conversions", "batch_seq")
        op.drop_column("collation_conversions", "batch_id")

    # Al final: mientras la FK de collation_conversions viva, el DROP TABLE de la referenciada
    # falla. Sin drop_index previos — drop_table ya se lleva sus índices, y emitirlos a mano
    # vuelve a chocar con el problema del índice que respalda una FK.
    op.drop_table("collation_conversion_batches")
