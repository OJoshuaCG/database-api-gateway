"""clone_batches + clone_batch_items — clonar N bases de un servidor a otro

Revision ID: b7c8d9e0f1a2
Revises: a6b7c8d9e0f1
Create Date: 2026-08-31 10:00:00

Crea las dos tablas del LOTE de clonación. El porqué del modelo está en el docstring de
``app/models/clone_batch.py``; acá van solo las decisiones que se ven en el DDL.

Por qué NO se toca ``clone_jobs``
---------------------------------
La primera versión de este cambio agregaba ``batch_id``/``batch_seq`` a ``clone_jobs``. Se
descartó: ``clone_batch_items.clone_job_id`` ya expresa la relación, y tenerla de los dos
lados obliga a mantenerlas de acuerdo. Con el enlace en un solo lado no hay nada que
sincronizar, y "¿de qué lote es este job?" se responde con un lookup por un índice.

Por qué el ítem no tiene un ``status`` propio
----------------------------------------------
Tiene ``outcome``, que SOLO se usa mientras la fila no tenga job (``pending``/``blocked``/
``skipped``/``canceled``). En cuanto ``clone_job_id`` se puebla, el estado de la fila es el
del job y ``outcome`` queda NULL. Una columna que espejara el estado del job sería una
segunda copia del mismo dato, con su desincronización garantizada.

Downgrade
---------
Las dos tablas son NUEVAS y ninguna columna se agrega a tablas existentes, así que no aplica
el orden FK → índice → columna que sí necesitan las migraciones que hacen ``ALTER``. Alcanza
con soltar la hija antes que la madre (la FK ``batch_id`` la referencia).

Los ``comment=`` de cada columna no son adorno: sin ellos ``alembic check`` reporta drift
permanente contra el modelo, que los declara.
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b7c8d9e0f1a2"
down_revision: Union[str, None] = "a6b7c8d9e0f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "clone_batches",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False, comment="ID único del lote"),
        sa.Column(
            "source_server_id",
            sa.Integer(),
            nullable=False,
            comment="Servidor del que salen las bases",
        ),
        sa.Column(
            "target_server_id",
            sa.Integer(),
            nullable=False,
            comment="Servidor al que se copian",
        ),
        sa.Column(
            "copy_intent",
            sa.String(length=30),
            nullable=False,
            comment="structure_only | structure_and_data | data_only por defecto para las filas",
        ),
        sa.Column(
            "data_on_existing",
            sa.String(length=20),
            nullable=True,
            comment="append | upsert. Obligatorio con copy_intent='data_only'; NULL si no aplica",
        ),
        sa.Column(
            "structure_spec",
            sa.Text(),
            nullable=True,
            comment="JSON del CloneStructureSpec por defecto (NULL = clon completo de cada base)",
        ),
        sa.Column(
            "data_spec",
            sa.Text(),
            nullable=True,
            comment="JSON del CloneDataSpec por defecto",
        ),
        sa.Column(
            "target_charset",
            sa.String(length=50),
            nullable=True,
            comment="Charset de las BDs destino que el lote CREE",
        ),
        sa.Column(
            "target_collation",
            sa.String(length=100),
            nullable=True,
            comment="Collation de las BDs destino que el lote CREE",
        ),
        sa.Column(
            "total",
            sa.Integer(),
            server_default="0",
            nullable=False,
            comment=(
                "Cantidad de filas del lote, congelada al planear. Se persiste para que el "
                "polling pueda decir '4 de 12' sin contar filas en cada llamada"
            ),
        ),
        sa.Column(
            "confirm_token",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Hash del CONJUNTO resuelto (servidor destino + la lista ordenada de pares "
                "origen→destino con su modo). Se recomputa server-side al ejecutar: cambiar, "
                "agregar o quitar una sola fila lo invalida"
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
            comment="pending | running | done | partial | failed | interrupted | canceled",
        ),
        sa.Column(
            "cancel_requested",
            sa.Boolean(),
            server_default="0",
            nullable=False,
            comment="Cancelación COOPERATIVA: el worker la consulta entre filas",
        ),
        sa.Column(
            "error",
            sa.String(length=500),
            nullable=True,
            comment="Motivo acotado del fallo del LOTE (nunca el mensaje crudo del motor)",
        ),
        sa.Column(
            "started_at",
            sa.DateTime(),
            nullable=True,
            comment="Cuándo arrancó la primera fila",
        ),
        sa.Column(
            "finished_at",
            sa.DateTime(),
            nullable=True,
            comment="Cuándo terminó la última fila",
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
            ["source_server_id"],
            ["servers.id"],
            name=op.f("fk_clone_batches_source_server_id_servers"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_server_id"],
            ["servers.id"],
            name=op.f("fk_clone_batches_target_server_id_servers"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Lote de clonaciones de N bases de datos de un servidor a otro",
    )
    op.create_index(
        op.f("ix_clone_batches_source_server_id"),
        "clone_batches",
        ["source_server_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_clone_batches_target_server_id"),
        "clone_batches",
        ["target_server_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_clone_batches_status"), "clone_batches", ["status"], unique=False
    )

    op.create_table(
        "clone_batch_items",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "batch_id", sa.Integer(), nullable=False, comment="Lote al que pertenece"
        ),
        sa.Column(
            "seq",
            sa.Integer(),
            nullable=False,
            comment="Orden de ejecución dentro del lote (1..N)",
        ),
        sa.Column(
            "source_database_name",
            sa.String(length=64),
            nullable=False,
            comment="Nombre de la BD origen en el servidor de origen",
        ),
        sa.Column(
            "source_database_id",
            sa.Integer(),
            nullable=True,
            comment="BD del inventario, si el origen está adoptado (NULL si es cruda)",
        ),
        sa.Column(
            "target_database_name",
            sa.String(length=64),
            nullable=False,
            comment="Nombre de la BD destino. Por defecto el del origen; editable por fila",
        ),
        sa.Column(
            "target_mode", sa.String(length=20), nullable=False, comment="new | existing"
        ),
        sa.Column(
            "overrides",
            sa.Text(),
            nullable=True,
            comment=(
                "JSON con lo que esta fila le pisa al perfil global del lote. NULL = usa el "
                "perfil tal cual, que es el caso mayoritario"
            ),
        ),
        sa.Column(
            "clone_job_id",
            sa.Integer(),
            nullable=True,
            comment=(
                "Job que ejecutó esta fila. Mientras sea NULL el estado de la fila es "
                "'outcome'; en cuanto se puebla, el estado ES el del job y 'outcome' queda NULL"
            ),
        ),
        sa.Column(
            "outcome",
            sa.String(length=20),
            nullable=True,
            comment=(
                "Estado de una fila SIN job: pending | blocked | skipped | canceled. NULL "
                "cuando hay job — para que no existan dos versiones del mismo estado"
            ),
        ),
        sa.Column(
            "error",
            sa.String(length=500),
            nullable=True,
            comment="Motivo por el que la fila quedó bloqueada, antes de que hubiera job",
        ),
        sa.Column(
            "error_code",
            sa.String(length=64),
            nullable=True,
            comment=(
                "Código estable del vocabulario 'clone.*' del motivo de bloqueo, para que el "
                "cliente lo mapee a su texto en vez de matchear prosa"
            ),
        ),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
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
            ["batch_id"],
            ["clone_batches.id"],
            name=op.f("fk_clone_batch_items_batch_id_clone_batches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_database_id"],
            ["managed_databases.id"],
            name=op.f("fk_clone_batch_items_source_database_id_managed_databases"),
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(
            ["clone_job_id"],
            ["clone_jobs.id"],
            name=op.f("fk_clone_batch_items_clone_job_id_clone_jobs"),
            ondelete="SET NULL",
        ),
        sa.PrimaryKeyConstraint("id"),
        comment="Una base dentro de un lote de clonación",
    )
    op.create_index(
        op.f("ix_clone_batch_items_batch_id"),
        "clone_batch_items",
        ["batch_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_clone_batch_items_clone_job_id"),
        "clone_batch_items",
        ["clone_job_id"],
        unique=False,
    )


def downgrade() -> None:
    # La hija primero: su FK ``batch_id`` referencia a ``clone_batches``.
    op.drop_index(op.f("ix_clone_batch_items_clone_job_id"), table_name="clone_batch_items")
    op.drop_index(op.f("ix_clone_batch_items_batch_id"), table_name="clone_batch_items")
    op.drop_table("clone_batch_items")

    op.drop_index(op.f("ix_clone_batches_status"), table_name="clone_batches")
    op.drop_index(op.f("ix_clone_batches_target_server_id"), table_name="clone_batches")
    op.drop_index(op.f("ix_clone_batches_source_server_id"), table_name="clone_batches")
    op.drop_table("clone_batches")
