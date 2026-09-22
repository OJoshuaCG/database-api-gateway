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

#: Nombre LÓGICO de la FK según la convención del modelo. **Nunca se usa para buscarla**: tiene
#: 65 caracteres, más que el límite de MySQL/MariaDB (64) y de PostgreSQL (63), así que cada
#: motor guardó una versión TRUNCADA con sufijo hash (en MariaDB,
#: ``fk_database_migration_history_model_migration_id_model_m_be07``). La primera versión de
#: esta migración hacía ``drop_constraint`` con el nombre largo y producción quedó en loop de
#: reinicios con ``(1091, "Can't DROP FOREIGN KEY ...; check that it exists")``. La FK se
#: DESCUBRE por introspección; este nombre solo se pasa por ``op.f`` al crearla de cero, que es
#: el camino que sí trunca igual que la migración original.
_FK_MIGRATION = "fk_database_migration_history_model_migration_id_model_migrations"


def _columnas_nuevas() -> list[sa.Column]:
    return [
        sa.Column(
            "direction",
            sa.String(length=4),
            nullable=True,
            comment="'up' (apply) | 'down' (rollback). NULL en filas previas a esta columna",
        ),
        sa.Column(
            "applied_version",
            sa.String(length=10),
            nullable=True,
            comment="Versión al momento del intento (congelada: el renumerado no la mueve)",
        ),
        sa.Column(
            "applied_checksum",
            sa.String(length=64),
            nullable=True,
            comment="Checksum del SQL que REALMENTE corrió, no el vigente de la definición",
        ),
        sa.Column(
            "actor_type",
            sa.String(length=16),
            nullable=True,
            comment="'admin' | 'api_token'. NULL en filas previas a esta columna",
        ),
        sa.Column(
            "actor_id",
            sa.Integer(),
            nullable=True,
            comment="ID del admin o del token que ejecutó (sin FK: el actor puede borrarse)",
        ),
        sa.Column(
            "actor_username",
            sa.String(length=128),
            nullable=True,
            comment="Nombre del actor al momento del intento (desnormalizado a propósito)",
        ),
        sa.Column(
            "request_id",
            sa.String(length=32),
            nullable=True,
            comment="Request ID: une con audit_log y con los logs HTTP sin depender de FKs",
        ),
    ]


def _columnas(bind) -> dict[str, dict]:
    # Inspector NUEVO en cada llamada: el inspector cachea, y entre pasos de esta misma
    # migración el esquema cambia.
    return {c["name"]: c for c in sa.inspect(bind).get_columns(_TABLE)}


def _fk_a_model_migrations(bind) -> dict | None:
    """La FK REAL de ``model_migration_id`` → ``model_migrations``, con el nombre que tenga."""
    for fk in sa.inspect(bind).get_foreign_keys(_TABLE):
        if fk.get("referred_table") == "model_migrations" and fk.get(
            "constrained_columns"
        ) == ["model_migration_id"]:
            return fk
    return None


def _ondelete(fk: dict) -> str:
    return str((fk.get("options") or {}).get("ondelete") or "").upper()


def upgrade() -> None:
    # **Idempotente paso a paso, y no es prolijidad.** MySQL/MariaDB no tienen DDL
    # transaccional: si esta migración muere a mitad, lo que ya corrió queda aplicado y
    # ``alembic_version`` sigue en la revisión anterior, así que el próximo arranque la corre
    # entera otra vez. Sin estos chequeos, el reintento choca con lo que el intento anterior ya
    # hizo (``Duplicate column name``) y el contenedor queda en loop. Cada paso pregunta el
    # estado real antes de actuar, así que retoma desde donde haya quedado.
    bind = op.get_bind()

    existentes = _columnas(bind)
    for columna in _columnas_nuevas():
        if columna.name not in existentes:
            op.add_column(_TABLE, columna)

    # Orden seguro en cualquier motor: soltar la FK, aflojar la columna, recrear la FK.
    # Modificar una columna mientras la cubre una FK es lo que MySQL 8 rechaza con 1832.
    fk = _fk_a_model_migrations(bind)
    if fk is not None and _ondelete(fk) == "SET NULL":
        nombre_fk = None  # ya está como se quiere: no se toca
    else:
        nombre_fk = fk["name"] if fk is not None else None
        if fk is not None:
            op.drop_constraint(fk["name"], _TABLE, type_="foreignkey")

    if not _columnas(bind)["model_migration_id"]["nullable"]:
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

    if _fk_a_model_migrations(bind) is None:
        # Se recrea con el MISMO nombre que tenía, así el esquema no cambia de identidad. Si
        # no había ninguna (un intento anterior la soltó y murió), ``op.f`` produce el mismo
        # nombre truncado que la migración que la creó originalmente.
        op.create_foreign_key(
            nombre_fk or op.f(_FK_MIGRATION),
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
    bind = op.get_bind()
    op.execute(sa.text(f"DELETE FROM {_TABLE} WHERE model_migration_id IS NULL"))

    fk = _fk_a_model_migrations(bind)
    nombre_fk = fk["name"] if fk is not None else None
    if fk is not None:
        op.drop_constraint(fk["name"], _TABLE, type_="foreignkey")

    if _columnas(bind)["model_migration_id"]["nullable"]:
        op.alter_column(
            _TABLE,
            "model_migration_id",
            existing_type=sa.Integer(),
            nullable=False,
            comment="Migración aplicada/revertida",
        )

    op.create_foreign_key(
        nombre_fk or op.f(_FK_MIGRATION),
        _TABLE,
        "model_migrations",
        ["model_migration_id"],
        ["id"],
        ondelete="CASCADE",
    )

    existentes = _columnas(bind)
    for columna in reversed(_columnas_nuevas()):
        if columna.name in existentes:
            op.drop_column(_TABLE, columna.name)
