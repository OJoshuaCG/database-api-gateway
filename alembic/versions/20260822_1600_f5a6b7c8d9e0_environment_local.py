"""environments: sumar 'local' como cuarto entorno

Revision ID: f5a6b7c8d9e0
Revises: e4f5a6b7c8d9
Create Date: 2026-08-22 16:00:00.000000

Los entornos pasan a ser un conjunto FIJO de cuatro: ``local`` (nuevo), ``development``,
``staging`` y ``production``. El default sigue siendo ``development``: esta migración NO lo
mueve, así que ninguna base existente ni nueva cambia de comportamiento.

**Por qué hace falta una migración y no alcanza con el servicio.** ``seed_environments()``
siembra únicamente si la tabla está VACÍA (regla deliberada del módulo: cada fila es política,
y un top-up puede resucitar un entorno que el operador borró a propósito o duplicar el
default). En una instalación que ya corrió la migración anterior la tabla tiene tres filas, así
que agregar ``local`` a ``environment_seed_rows()`` no lo insertaría nunca. El servicio se
actualiza igual, para que una instalación NUEVA —o un esquema hecho con
``Base.metadata.create_all`` en tests— nazca con los cuatro; ``tests/test_api_environments.py``
compara las dos fuentes fila por fila.

PRIMERA MIGRACIÓN SOLO-DE-DATOS DEL REPO, y conviene dejarlo dicho porque sienta precedente:
las únicas dos que insertan filas (``c4d5e6f7a8b9`` charsets y ``e4f5a6b7c8d9`` entornos) son
las que CREAN la tabla, y sus ``downgrade`` se limitan a dropearla.

El INSERT es idempotente por ``slug``: si alguien ya creó ``local`` por API, esta migración no
hace nada en vez de chocar el índice único.

EL ``downgrade`` ES UN NO-OP DELIBERADO, no un olvido. Tres motivos:
    1. Un ``DELETE`` a ciegas FALLA. La FK ``managed_databases.environment_id`` es
       ``ON DELETE RESTRICT``, así que en cuanto una base quedó clasificada como ``local`` el
       borrado revienta y deja trabado el downgrade de toda la cadena.
    2. Un borrado CONDICIONAL ("solo si no está referenciada") es peor: se comporta distinto
       según el estado de los datos y no lo dice. Este repo trata los recortes silenciosos como
       defecto, no como comodidad.
    3. Una fila de catálogo extra es inocua —nadie la apunta si el downgrade se hizo porque el
       código nuevo no se usa— y si de verdad hay que borrar la tabla, la revisión de abajo
       (``e4f5a6b7c8d9``) se la lleva entera.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'f5a6b7c8d9e0'
down_revision: Union[str, None] = 'e4f5a6b7c8d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# Espejo de la fila 'local' de ``environment_catalog.environment_seed_rows()``. Se declara
# literalmente acá y no se importa el servicio: una migración tiene que seguir aplicando igual
# dentro de un año, cuando ese código haya cambiado.
_LOCAL_ROW = {
    'name': 'Local',
    'slug': 'local',
    'rank': 5,  # el más temprano: por debajo de development (10)
    'color': 'neutral',
    'is_default': False,  # el default sigue siendo development
    'is_active': True,
    'blocks_destructive_migrations': False,
}


def upgrade() -> None:
    bind = op.get_bind()
    already = bind.execute(
        sa.text("SELECT 1 FROM environments WHERE slug = :slug"),
        {'slug': _LOCAL_ROW['slug']},
    ).first()
    if already is not None:
        return  # ya existe (creado por API, o por el seed de una instalación nueva)

    # Tabla ligera solo para el INSERT, con TIPOS: los booleanos y el Integer pasan por el bind
    # processor del dialecto en vez de depender de que el driver acepte el bool de Python.
    environments = sa.table(
        'environments',
        sa.column('name', sa.String),
        sa.column('slug', sa.String),
        sa.column('rank', sa.Integer),
        sa.column('color', sa.String),
        sa.column('is_default', sa.Boolean),
        sa.column('is_active', sa.Boolean),
        sa.column('blocks_destructive_migrations', sa.Boolean),
    )
    op.bulk_insert(environments, [_LOCAL_ROW])


def downgrade() -> None:
    """NO-OP deliberado. El motivo completo está en el docstring del módulo.

    Resumen: la FK es ``ON DELETE RESTRICT``, así que borrar la fila falla en cuanto una base
    quedó clasificada como ``local``; y un borrado condicional se comportaría distinto según los
    datos sin decirlo. Una fila de catálogo extra es inocua, y la revisión de abajo dropea la
    tabla entera si de verdad hay que deshacer el módulo.
    """
