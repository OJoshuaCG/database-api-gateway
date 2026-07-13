"""schema_comparisons: referencias a BDs crudas (server_id + nombre) + relajar managed ids

Revision ID: f7a8b9c0d1e2
Revises: eb3aa0df3d42
Create Date: 2026-07-13 09:30:00.000000

Migración de SEGUIMIENTO de ``eb3aa0df3d42`` (add schema comparison tables). NO edita
la migración inicial (ya en git); sigue el patrón del repo de agregar deltas aparte
(p. ej. ``model_migration_reviewed``).

Cambio: una comparación puede referirse a CUALQUIER BD de un servidor dado de alta,
aunque nunca se haya registrado en el inventario. Por eso:

- Se agregan ``source_server_id``/``target_server_id`` (NOT NULL, FK a ``servers``,
  ON DELETE CASCADE) y ``source_database_name``/``target_database_name`` (NOT NULL):
  identifican SIEMPRE la BD física de cada lado.
- ``source_database_id``/``target_database_id`` pasan a NULLABLE (siguen siendo el
  ``managed_database_id`` cuando la BD está en el inventario; ``NULL`` si es cruda).

Estrategia (segura con o sin filas existentes): se agregan las 4 columnas como NULLABLE,
se BACKFILLEA desde ``managed_databases`` (las filas previas siempre tienen ``*_id`` no
nulo) y recién entonces se ponen NOT NULL. Ramas por dialecto: SQLite recrea la tabla
(``batch_alter_table``); MySQL/MariaDB/PostgreSQL usan ALTER directos.

Verificada con el ciclo ``upgrade head`` → ``downgrade -1`` → ``upgrade head`` contra
MariaDB 11 real y contra SQLite (motor de la BD del gateway en dev/tests).
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a8b9c0d1e2"
down_revision: Union[str, None] = "eb3aa0df3d42"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Comentarios de columna (deben coincidir con el modelo para no generar drift en un
# futuro autogenerate; en MySQL el MODIFY re-emite el COMMENT o lo perdería).
_C_SRC_SRV = "Servidor de la BD source (siempre poblado, aun si la BD no está en inventario)"
_C_TGT_SRV = "Servidor de la BD target (siempre poblado, aun si la BD no está en inventario)"
_C_SRC_NAME = "Nombre de la BD source en el motor (siempre poblado)"
_C_TGT_NAME = "Nombre de la BD target en el motor (siempre poblado)"
_C_SRC_ID_NEW = (
    "managed_database_id del source si está en el inventario; NULL si es una BD cruda no registrada"
)
_C_TGT_ID_NEW = (
    "managed_database_id del target si está en el inventario; NULL si es una BD cruda no registrada"
)
# Comentarios originales (para restaurar en el downgrade).
_C_SRC_ID_OLD = (
    "BD de referencia (estado deseado). El diff describe cómo llevar TARGET a este estado"
)
_C_TGT_ID_OLD = "BD que se modificaría (recibe el DDL derivado)"

_BACKFILL = [
    (
        "UPDATE schema_comparisons SET "
        "source_server_id = (SELECT server_id FROM managed_databases "
        "WHERE managed_databases.id = schema_comparisons.source_database_id), "
        "source_database_name = (SELECT name FROM managed_databases "
        "WHERE managed_databases.id = schema_comparisons.source_database_id) "
        "WHERE source_database_id IS NOT NULL"
    ),
    (
        "UPDATE schema_comparisons SET "
        "target_server_id = (SELECT server_id FROM managed_databases "
        "WHERE managed_databases.id = schema_comparisons.target_database_id), "
        "target_database_name = (SELECT name FROM managed_databases "
        "WHERE managed_databases.id = schema_comparisons.target_database_id) "
        "WHERE target_database_id IS NOT NULL"
    ),
]


def upgrade() -> None:
    # Paso 1: columnas nuevas como NULLABLE (ADD COLUMN funciona directo en los 3 motores).
    op.add_column("schema_comparisons", sa.Column("source_server_id", sa.Integer(), nullable=True, comment=_C_SRC_SRV))
    op.add_column("schema_comparisons", sa.Column("target_server_id", sa.Integer(), nullable=True, comment=_C_TGT_SRV))
    op.add_column("schema_comparisons", sa.Column("source_database_name", sa.String(length=64), nullable=True, comment=_C_SRC_NAME))
    op.add_column("schema_comparisons", sa.Column("target_database_name", sa.String(length=64), nullable=True, comment=_C_TGT_NAME))

    # Paso 2: backfill de las filas existentes (que siempre tienen *_database_id no nulo).
    for stmt in _BACKFILL:
        op.execute(stmt)

    # Paso 3: NOT NULL + índices + FKs + relajar *_database_id a NULLABLE.
    if op.get_bind().dialect.name == "sqlite":
        # SQLite no soporta ALTER de nullability ni ADD CONSTRAINT: recrea la tabla.
        with op.batch_alter_table("schema_comparisons", schema=None) as b:
            b.alter_column("source_server_id", existing_type=sa.Integer(), nullable=False)
            b.alter_column("target_server_id", existing_type=sa.Integer(), nullable=False)
            b.alter_column("source_database_name", existing_type=sa.String(length=64), nullable=False)
            b.alter_column("target_database_name", existing_type=sa.String(length=64), nullable=False)
            b.alter_column("source_database_id", existing_type=sa.Integer(), nullable=True)
            b.alter_column("target_database_id", existing_type=sa.Integer(), nullable=True)
            b.create_index(b.f("ix_schema_comparisons_source_server_id"), ["source_server_id"], unique=False)
            b.create_index(b.f("ix_schema_comparisons_target_server_id"), ["target_server_id"], unique=False)
            b.create_foreign_key(b.f("fk_schema_comparisons_source_server_id_servers"), "servers", ["source_server_id"], ["id"], ondelete="CASCADE")
            b.create_foreign_key(b.f("fk_schema_comparisons_target_server_id_servers"), "servers", ["target_server_id"], ["id"], ondelete="CASCADE")
    else:
        op.alter_column("schema_comparisons", "source_server_id", existing_type=sa.Integer(), nullable=False, existing_comment=_C_SRC_SRV)
        op.alter_column("schema_comparisons", "target_server_id", existing_type=sa.Integer(), nullable=False, existing_comment=_C_TGT_SRV)
        op.alter_column("schema_comparisons", "source_database_name", existing_type=sa.String(length=64), nullable=False, existing_comment=_C_SRC_NAME)
        op.alter_column("schema_comparisons", "target_database_name", existing_type=sa.String(length=64), nullable=False, existing_comment=_C_TGT_NAME)
        op.create_index(op.f("ix_schema_comparisons_source_server_id"), "schema_comparisons", ["source_server_id"], unique=False)
        op.create_index(op.f("ix_schema_comparisons_target_server_id"), "schema_comparisons", ["target_server_id"], unique=False)
        op.create_foreign_key(op.f("fk_schema_comparisons_source_server_id_servers"), "schema_comparisons", "servers", ["source_server_id"], ["id"], ondelete="CASCADE")
        op.create_foreign_key(op.f("fk_schema_comparisons_target_server_id_servers"), "schema_comparisons", "servers", ["target_server_id"], ["id"], ondelete="CASCADE")
        # Relajar a NULLABLE (managed_database_id si está en inventario; NULL si es cruda).
        op.alter_column("schema_comparisons", "source_database_id", existing_type=sa.Integer(), nullable=True, comment=_C_SRC_ID_NEW, existing_comment=_C_SRC_ID_OLD)
        op.alter_column("schema_comparisons", "target_database_id", existing_type=sa.Integer(), nullable=True, comment=_C_TGT_ID_NEW, existing_comment=_C_TGT_ID_OLD)


def downgrade() -> None:
    # El downgrade es LOSSY por diseño: al reintroducir NOT NULL en *_database_id, no se
    # pueden conservar las comparaciones de BDs CRUDAS (managed id NULL). Se eliminan.
    op.execute(
        "DELETE FROM schema_comparisons "
        "WHERE source_database_id IS NULL OR target_database_id IS NULL"
    )

    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("schema_comparisons", schema=None) as b:
            # Los índices deben soltarse EXPLÍCITAMENTE antes de recrear la tabla: el batch
            # recreate de SQLite reflejaría y re-crearía un índice sobre una columna ya
            # eliminada ("no such column"). Las FKs, en cambio, desaparecen con la columna.
            b.drop_index(b.f("ix_schema_comparisons_source_server_id"))
            b.drop_index(b.f("ix_schema_comparisons_target_server_id"))
            b.alter_column("source_database_id", existing_type=sa.Integer(), nullable=False)
            b.alter_column("target_database_id", existing_type=sa.Integer(), nullable=False)
            b.drop_column("target_database_name")
            b.drop_column("source_database_name")
            b.drop_column("target_server_id")
            b.drop_column("source_server_id")
    else:
        # Orden en MySQL/MariaDB: soltar la FK ANTES del índice que la respalda (soltar el
        # índice primero falla: "needed in a foreign key constraint").
        op.drop_constraint(op.f("fk_schema_comparisons_source_server_id_servers"), "schema_comparisons", type_="foreignkey")
        op.drop_constraint(op.f("fk_schema_comparisons_target_server_id_servers"), "schema_comparisons", type_="foreignkey")
        op.drop_index(op.f("ix_schema_comparisons_source_server_id"), table_name="schema_comparisons")
        op.drop_index(op.f("ix_schema_comparisons_target_server_id"), table_name="schema_comparisons")
        op.alter_column("schema_comparisons", "source_database_id", existing_type=sa.Integer(), nullable=False, comment=_C_SRC_ID_OLD, existing_comment=_C_SRC_ID_NEW)
        op.alter_column("schema_comparisons", "target_database_id", existing_type=sa.Integer(), nullable=False, comment=_C_TGT_ID_OLD, existing_comment=_C_TGT_ID_NEW)
        op.drop_column("schema_comparisons", "target_database_name")
        op.drop_column("schema_comparisons", "source_database_name")
        op.drop_column("schema_comparisons", "target_server_id")
        op.drop_column("schema_comparisons", "source_server_id")
