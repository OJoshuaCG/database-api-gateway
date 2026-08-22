"""environments + managed_databases.environment_id (clasificación por entorno)

Revision ID: e4f5a6b7c8d9
Revises: d8e9f0a1b2c3
Create Date: 2026-08-22 12:00:00.000000

Crea el catálogo de entornos de despliegue y la FK que clasifica cada BD gestionada.

``down_revision`` apunta al head vigente (``d8e9f0a1b2c3``) y NO a un padre más arriba.
Regla dura del repo, con cicatriz: el 2026-08-22 dos migraciones escritas el mismo día en
ramas distintas colgaron del mismo padre, el DAG quedó con dos heads, y
``alembic upgrade head`` del ``entrypoint.sh`` dejó de poder resolver a qué revisión ir — el
contenedor no arrancaba. Nunca se dejan dos hijas del mismo padre.
``scripts/check_migration_graph.py`` lo verifica en cada push (pero NO verifica el contenido
de estas funciones: eso lo caza el ciclo upgrade/downgrade/upgrade).

RAMA POR DIALECTO, y no ``batch_alter_table`` incondicional. Es el precedente explícito del
repo para "agregar una FK nullable a una tabla que ya existe" (``f7a8b9c0d1e2`` y
``d3e4f5a6b7c8``, que lo cita como tal): en MySQL/MariaDB/PostgreSQL el batch es un
passthrough, así que envolver todo no gana nada y divide el criterio.

EL ``downgrade`` NECESITA ``drop_index`` EXPLÍCITO, al contrario de lo que hacen las
migraciones que solo crean tablas. Acá la columna se elimina con ``drop_column``, no con
``drop_table``, y el recreate del batch de SQLite reflejaría y re-crearía un índice sobre una
columna ya eliminada:

    OperationalError: no such column: environment_id
    [SQL: CREATE INDEX ix_managed_databases_environment_id ON managed_databases (...)]

Y en MySQL/MariaDB el orden importa al revés: la **FK antes** del índice que la respalda, o
falla con "Cannot drop index ...: needed in a foreign key constraint". Los dos matices están
documentados en ``f7a8b9c0d1e2`` y ``d3e4f5a6b7c8``.

``environments`` se suelta AL FINAL: mientras la FK de ``managed_databases`` viva, el
``DROP TABLE`` de la tabla referenciada falla.

Las filas del seed se declaran LITERALMENTE acá y no se importa
``app/services/environment_catalog.py``: una migración tiene que seguir aplicando igual
dentro de un año, cuando ese código haya cambiado. El mismo seed vive en el servicio para el
arranque de un esquema hecho con ``create_all`` (que no pasa por Alembic), y
``tests/test_api_environments.py`` compara las dos listas fila por fila — a diferencia del
catálogo de charsets, acá divergir SÍ hace daño: la fila es política, y las dos vías de
provisión quedarían con políticas distintas.

En modo OFFLINE (``alembic upgrade --sql``) ``op.bulk_insert`` no emite nada salvo
``multiinsert=False``. El ``entrypoint.sh`` corre online, así que no aplica hoy; queda dicho
para que nadie genere un script offline creyendo que trae el seed.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'e4f5a6b7c8d9'
down_revision: Union[str, None] = 'd8e9f0a1b2c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Mismas filas que ``environment_catalog.environment_seed_rows()``. Todas las claves en
# TODAS las filas: ``op.bulk_insert`` va por ``executemany`` y un dict que omita una clave
# desalinea los parámetros del lote.
_SEED_ROWS = [
    {
        'name': 'Desarrollo',
        'slug': 'development',
        'rank': 10,
        'color': 'info',
        'is_default': True,
        'is_active': True,
        'blocks_destructive_migrations': False,
    },
    {
        'name': 'Staging',
        'slug': 'staging',
        'rank': 20,
        'color': 'warning',
        'is_default': False,
        'is_active': True,
        'blocks_destructive_migrations': False,
    },
    {
        'name': 'Producción',
        'slug': 'production',
        'rank': 30,
        'color': 'error',
        'is_default': False,
        'is_active': True,
        'blocks_destructive_migrations': True,
    },
]


def upgrade() -> None:
    # La tabla padre PRIMERO: el batch de SQLite que crea la FK más abajo la necesita ya
    # existente. Se conserva el objeto que devuelve create_table para el bulk_insert: trae
    # los TIPOS, así que los booleanos y el Integer nullable pasan por el bind processor del
    # dialecto en vez de depender de que el driver acepte el bool de Python.
    environments = op.create_table(
        'environments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False, comment='ID único del entorno'),
        sa.Column('name', sa.String(length=60), nullable=False, comment="Nombre legible del entorno (p. ej. 'Producción'), único"),
        sa.Column('slug', sa.String(length=60), nullable=False, comment="Identificador estable en minúsculas (p. ej. 'production'). Es lo que se audita"),
        sa.Column('rank', sa.Integer(), server_default='0', nullable=False, comment='Orden de promoción: MENOR = MÁS TEMPRANO (desarrollo antes que producción). No es único; el desempate es por id'),
        sa.Column('color', sa.String(length=20), nullable=True, comment='Color de presentación. Uno de: neutral|primary|success|error|warning|info. Solo presentación: no participa de ninguna decisión'),
        sa.Column('is_default', sa.Boolean(), server_default='0', nullable=False, comment="Entorno que se asigna a una BD nueva que no lo especifica (a lo sumo uno True). OJO: el default es el entorno más permisivo, así que 'nace clasificada' no equivale a 'nace protegida'"),
        sa.Column('is_active', sa.Boolean(), server_default='1', nullable=False, comment='Si se puede asignar a una BD. Es la vía de retiro de un entorno con BDs'),
        sa.Column('blocks_destructive_migrations', sa.Boolean(), server_default='0', nullable=False, comment='POLÍTICA APLICADA: rechaza aplicar versiones con sentencias destructivas (DROP/TRUNCATE/DELETE sin WHERE/ALTER DROP COLUMN) a las BDs de este entorno'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de creación del registro'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('(CURRENT_TIMESTAMP)'), nullable=False, comment='Fecha y hora de última actualización del registro'),
        sa.PrimaryKeyConstraint('id', name=op.f('pk_environments')),
        comment='Entornos de despliegue: clasifican las BDs gestionadas y su política',
    )
    # Los tres como ÍNDICE (no UniqueConstraint), para que el DDL emitido coincida con el
    # `index=True` del modelo: declarar uno y emitir el otro deja drift de `alembic check`
    # permanente. `rank` va sin unique a propósito — ver el docstring de app/models/environment.py.
    with op.batch_alter_table('environments', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_environments_name'), ['name'], unique=True)
        batch_op.create_index(batch_op.f('ix_environments_slug'), ['slug'], unique=True)
        batch_op.create_index(batch_op.f('ix_environments_rank'), ['rank'], unique=False)

    op.bulk_insert(environments, _SEED_ROWS)

    # `add_column` PLANO, fuera de cualquier batch (precedente f7a8b9c0d1e2): en los tres
    # motores reales es un ALTER normal, y en SQLite agregar una columna nullable tampoco
    # necesita recreate. El batch se usa solo para el índice y la FK, que en SQLite sí lo
    # exigen.
    op.add_column(
        'managed_databases',
        sa.Column(
            'environment_id',
            sa.Integer(),
            nullable=True,
            comment='Entorno de despliegue que clasifica esta BD (opcional). RESTRICT: reasignar antes de borrar',
        ),
    )
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('managed_databases', schema=None) as batch_op:
            batch_op.create_index(
                batch_op.f('ix_managed_databases_environment_id'), ['environment_id'], unique=False
            )
            batch_op.create_foreign_key(
                batch_op.f('fk_managed_databases_environment_id_environments'),
                'environments',
                ['environment_id'],
                ['id'],
                ondelete='RESTRICT',
            )
    else:
        op.create_index(
            op.f('ix_managed_databases_environment_id'),
            'managed_databases',
            ['environment_id'],
            unique=False,
        )
        op.create_foreign_key(
            op.f('fk_managed_databases_environment_id_environments'),
            'managed_databases',
            'environments',
            ['environment_id'],
            ['id'],
            ondelete='RESTRICT',
        )


def downgrade() -> None:
    """Revierte los cambios de esta migración (alembic downgrade).

    A diferencia de las migraciones que solo crean tablas, acá los ``drop_index`` /
    ``drop_constraint`` explícitos son OBLIGATORIOS: la columna se va con ``drop_column`` y no
    con ``drop_table``, así que nada se lleva el índice por delante. Ver el docstring del
    módulo para los dos modos de fallo (el recreate de SQLite y el orden FK↔índice de MySQL).
    """
    if op.get_bind().dialect.name == 'sqlite':
        # El recreate refleja la tabla resultante: hay que soltar el índice DENTRO del mismo
        # batch en que se elimina la columna, o intenta re-crearlo sobre una columna que ya no
        # existe. La FK no se suelta aparte: en SQLite vive en el CREATE TABLE, y el recreate
        # sin la columna la deja fuera.
        with op.batch_alter_table('managed_databases', schema=None) as batch_op:
            batch_op.drop_index(batch_op.f('ix_managed_databases_environment_id'))
            batch_op.drop_column('environment_id')
    else:
        # La FK ANTES del índice que la respalda: al revés, MySQL/MariaDB rechazan el
        # drop_index con "needed in a foreign key constraint".
        op.drop_constraint(
            op.f('fk_managed_databases_environment_id_environments'),
            'managed_databases',
            type_='foreignkey',
        )
        op.drop_index(
            op.f('ix_managed_databases_environment_id'), table_name='managed_databases'
        )
        op.drop_column('managed_databases', 'environment_id')

    # Al final: mientras la FK de managed_databases viva, el DROP TABLE de la referenciada
    # falla. Sin drop_index previos: drop_table ya se lleva los índices con la tabla, y
    # emitirlos a mano vuelve a chocar con el problema del índice que respalda una FK.
    op.drop_table('environments')
