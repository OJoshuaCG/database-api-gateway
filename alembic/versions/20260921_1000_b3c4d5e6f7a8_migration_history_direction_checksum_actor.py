"""Historial de migraciones: dirección, checksum aplicado, versión congelada y actor

Seis cambios sobre ``database_migration_history``, todos por el mismo motivo: la tabla
registraba QUÉ migración corrió y CUÁNTO tardó, pero no alcanzaba para responder las
preguntas por las que existe un historial.

**``direction``.** ``_record_history`` se llama IGUAL desde ``apply`` que desde ``rollback``,
y ni la tabla ni ``MigrationResult`` traían la dirección: una fila ``applied`` de un rollback
era indistinguible de un apply. No es cosmético — es la causa raíz de que exista el módulo
``migration_freeze_catalog`` entero. Como el historial no puede decir si una versión sigue
vigente, el guard del freeze tiene que abrir conexión a CADA motor y leerlo en vivo.

**``applied_checksum``.** El ``checksum`` de ``model_migrations`` es de la DEFINICIÓN, no de
la APLICACIÓN: se recalcula en cada edición, así que nada podía responder "¿qué texto corrió
realmente en la BD #7?". El repo ya documenta que editar el ``up_sql`` de una versión aplicada
deja una divergencia "real e irreversible" y que "lo único que se puede evitar es que quede en
SILENCIO"; hasta acá eso se evitaba solo en el instante de editar, con un rastro en
``audit_log`` que nombra la migración pero no el estado de cada base. Esta columna convierte
esa divergencia de "anunciada una vez" a "detectable siempre".

**``applied_version``.** Copia CONGELADA del número. ``history()`` hace outerjoin a
``model_migrations`` y devuelve su ``version`` ACTUAL, así que tras un renumerado un evento de
hace seis meses pasa a mostrar un número que nunca tuvo. Un log cuyo contenido cambia
retroactivamente no es un log.

**Actor** (``actor_type``, ``actor_id``, ``actor_username``, ``request_id``). ``_record_history``
ni siquiera recibía el ``admin``: la autoría vivía solo en ``audit_log``, sin FK entre las dos
tablas, y correlacionarlas exigía cruzar por tiempo. Se desnormaliza en vez de poner una FK a
``audit_log`` a propósito: ``audit.record`` es *best-effort* —si falla, solo loguea—, así que
una FK apuntaría a una fila que puede no existir justo cuando más se necesita. ``request_id``
es el que más rinde: une con ``audit_log`` **y** con los logs HTTP sin depender de que ninguna
de las dos escrituras haya sobrevivido.

**``ondelete`` pasa de CASCADE a SET NULL**, y sin esto todo lo anterior es decorativo:
``delete_migration`` hace ``session.delete(m)``, así que borrar una versión de blueprint
**borraba su historial de aplicación en las N bases**. La evidencia desaparecía con una
operación de mantenimiento rutinaria. Para permitir SET NULL la columna pasa a nullable, y por
eso las tres columnas de arriba importan todavía más: con la FK en NULL, ``applied_version`` y
``applied_checksum`` son lo ÚNICO que queda del evento.

**Todas las columnas nacen NULL y se quedan NULL para las filas existentes**, que es la
verdad: no hay dato histórico que inventar. Backfillear ``applied_version`` desde la FK
escribiría el número ACTUAL —el que un renumerado pudo haber movido—, o sea un valor plausible
y posiblemente falso en la única columna cuyo valor entero es ser un hecho. Para las filas
viejas, la versión se sigue leyendo por la FK como siempre.

Los ``comment=`` no son adorno: sin ellos ``alembic check`` reporta drift permanente contra el
modelo, que los declara.

Revision ID: b3c4d5e6f7a8
Revises: a2b3c4d5e6f7
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "b3c4d5e6f7a8"
down_revision: Union[str, None] = "a2b3c4d5e6f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "database_migration_history"
_FK_MIGRATION = "fk_database_migration_history_model_migration_id_model_migrations"


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column(
            "direction",
            sa.String(length=4),
            nullable=True,
            comment="'up' (apply) | 'down' (rollback). NULL en filas previas a esta columna",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "applied_version",
            sa.String(length=10),
            nullable=True,
            comment="Versión al momento del intento (congelada: el renumerado no la mueve)",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "applied_checksum",
            sa.String(length=64),
            nullable=True,
            comment="Checksum del SQL que REALMENTE corrió, no el vigente de la definición",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "actor_type",
            sa.String(length=16),
            nullable=True,
            comment="'admin' | 'api_token'. NULL en filas previas a esta columna",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "actor_id",
            sa.Integer(),
            nullable=True,
            comment="ID del admin o del token que ejecutó (sin FK: el actor puede borrarse)",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "actor_username",
            sa.String(length=128),
            nullable=True,
            comment="Nombre del actor al momento del intento (desnormalizado a propósito)",
        ),
    )
    op.add_column(
        _TABLE,
        sa.Column(
            "request_id",
            sa.String(length=32),
            nullable=True,
            comment="Request ID: une con audit_log y con los logs HTTP sin depender de FKs",
        ),
    )

    # SET NULL exige que la columna admita NULL. El orden importa: primero se afloja la
    # columna, después se rehace la FK. Al revés, MySQL rechaza la FK por incompatibilidad.
    op.alter_column(
        _TABLE,
        "model_migration_id",
        existing_type=sa.Integer(),
        nullable=True,
        comment=(
            "Migración aplicada/revertida. NULL si la versión se borró del blueprint: el "
            "evento histórico sobrevive en applied_version/applied_checksum"
        ),
    )
    op.drop_constraint(_FK_MIGRATION, _TABLE, type_="foreignkey")
    op.create_foreign_key(
        _FK_MIGRATION,
        _TABLE,
        "model_migrations",
        ["model_migration_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    # La vuelta a CASCADE es DESTRUCTIVA por diseño del esquema viejo: cualquier fila con
    # model_migration_id NULL viola el NOT NULL, así que hay que descartarla antes. Son
    # exactamente las que sobrevivieron al borrado de su versión — el dato que esta migración
    # existe para conservar.
    op.execute(f"DELETE FROM {_TABLE} WHERE model_migration_id IS NULL")
    op.drop_constraint(_FK_MIGRATION, _TABLE, type_="foreignkey")
    op.alter_column(
        _TABLE,
        "model_migration_id",
        existing_type=sa.Integer(),
        nullable=False,
        comment="Migración aplicada/revertida",
    )
    op.create_foreign_key(
        _FK_MIGRATION,
        _TABLE,
        "model_migrations",
        ["model_migration_id"],
        ["id"],
        ondelete="CASCADE",
    )
    for columna in (
        "request_id",
        "actor_username",
        "actor_id",
        "actor_type",
        "applied_checksum",
        "applied_version",
        "direction",
    ):
        op.drop_column(_TABLE, columna)
