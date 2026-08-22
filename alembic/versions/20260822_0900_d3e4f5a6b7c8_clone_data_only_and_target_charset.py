"""clone_jobs: modo solo datos, selección de datos, charset/owner del destino y fingerprint

Revision ID: d3e4f5a6b7c8
Revises: c7d8e9f0a1b2
Create Date: 2026-08-22 09:00:00.000000

Habilita en el clon lo que el §4 del plan 11 pedía y no era representable:

- ``copy_intent`` reemplaza a ``include_data`` como fuente de verdad de QUÉ se copia. Se
  persiste la INTENCIÓN (``structure_only``/``structure_and_data``/``data_only``) y no el
  ``EntityDdl`` derivado: los dos primeros derivan al mismo DDL por objeto, así que guardar
  solo eso perdería la diferencia que el preview tiene que devolver.
- ``data_selection`` / ``data_on_existing``: la selección de datos pasa a ser un eje propio.
- ``target_charset`` / ``target_collation`` / ``target_owner*``: el destino nuevo deja de
  heredar a ciegas los valores del origen.
- ``is_full_clone``: predicado EXPLÍCITO de "clon completo". Antes se infería de
  ``selection IS NULL``, y con la selección declarativa resuelta a lista explícita esa
  inferencia dejaba ``will_adopt`` en False **para siempre sin que nada falle**.
- ``target_fingerprint``: en ``data_only`` la validez del plan depende del esquema del
  DESTINO tanto como del origen, y hasta ahora nadie lo fijaba.

Tres decisiones que importan:

1. **Se rellenan los datos, no se deja un default arbitrario.** ``copy_intent`` y
   ``data_on_existing`` se derivan fila por fila de ``include_data``/``clean_mode``/
   ``target_mode``, e ``is_full_clone`` de ``selection IS NULL``, reproduciendo EXACTAMENTE
   la semántica con la que se creó cada job. Un ``server_default`` uniforme habría hecho que
   un job histórico de estructura+datos se leyera como uno de solo estructura.

2. **Los planes ``pending`` se expiran.** Este cambio agrega ejes al hash del
   ``confirm_token``, así que el token de un plan previsualizado antes del deploy ya no
   coincide. Expirarlos convierte eso en un 410 "volvé a previsualizar" —un fallo evidente y
   accionable— en vez de un 422 de token que parece un bug. Un clon silenciosamente distinto
   del que se confirmó sería mucho peor que replanear.

3. ``include_data`` **se conserva**: sigue siendo el campo que lee la SPA y ahora es un
   valor DERIVADO de ``copy_intent``. Quitarlo rompería el cliente sin ganar nada.

Solo ``add_column``/``drop_column`` sobre una tabla existente: el patrón de ``downgrade()``
roto que hay en otras migraciones del repo (soltar un índice FK-backed antes del
``drop_table``, que MySQL/MariaDB rechaza) no aplica acá. El ``drop_column`` va en orden
inverso, y el de ``target_owner_user_id`` suelta primero su FK en los motores que la nombran.
"""
from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = 'd3e4f5a6b7c8'
down_revision: Union[str, None] = 'c7d8e9f0a1b2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        'clone_jobs',
        sa.Column(
            'is_full_clone',
            sa.Boolean(),
            nullable=False,
            server_default='1',
            comment=(
                'True = la selección cubre TODO el origen. Predicado EXPLÍCITO: antes se '
                "infería de 'selection IS NULL', y con la selección declarativa resuelta a "
                'lista explícita esa inferencia apagaba el auto-adopt sin que nada falle'
            ),
        ),
    )
    op.add_column(
        'clone_jobs',
        sa.Column(
            'copy_intent',
            sa.String(length=30),
            nullable=False,
            server_default='structure_only',
            comment='structure_only | structure_and_data | data_only (solo filas, sin DDL)',
        ),
    )
    op.add_column(
        'clone_jobs',
        sa.Column(
            'data_selection',
            sa.Text(),
            nullable=True,
            comment=(
                'JSON de las tablas que reciben datos (ya con cierre por FK); NULL = '
                'derivar de la selección de estructura, que es el comportamiento histórico'
            ),
        ),
    )
    op.add_column(
        'clone_jobs',
        sa.Column(
            'data_on_existing',
            sa.String(length=20),
            nullable=True,
            comment=(
                'append | upsert cuando la tabla destino ya tiene filas. Obligatorio en '
                "copy_intent='data_only'; NULL en los planes que crean la estructura"
            ),
        ),
    )
    op.add_column(
        'clone_jobs',
        sa.Column(
            'target_charset',
            sa.String(length=50),
            nullable=True,
            comment=(
                'Charset CANÓNICO del catálogo para el CREATE DATABASE del destino; '
                'NULL = heredar del origen (mismo motor) o el default del motor'
            ),
        ),
    )
    op.add_column(
        'clone_jobs',
        sa.Column(
            'target_collation',
            sa.String(length=100),
            nullable=True,
            comment='Collation CANÓNICA del catálogo para el CREATE DATABASE del destino',
        ),
    )
    op.add_column(
        'clone_jobs',
        sa.Column(
            'target_owner_user_id',
            sa.Integer(),
            nullable=True,
            comment=(
                'ServerUser del servidor destino que será OWNER de la BD creada '
                '(solo PostgreSQL)'
            ),
        ),
    )
    op.add_column(
        'clone_jobs',
        sa.Column(
            'target_owner',
            sa.String(length=64),
            nullable=True,
            comment=(
                'Username resuelto del owner, para que el worker no dependa de que la fila '
                'de inventario siga existiendo (mismo criterio que el resto del módulo)'
            ),
        ),
    )
    op.add_column(
        'clone_jobs',
        sa.Column(
            'target_fingerprint',
            sa.String(length=64),
            nullable=True,
            comment=(
                'SHA256 del snapshot del DESTINO al previsualizar. NULL si el plan no '
                "depende del esquema del destino. En 'data_only' la validez del plan "
                'depende del destino tanto como del origen, y sin esto nadie la fija'
            ),
        ),
    )
    # SQLite no soporta ADD CONSTRAINT: hay que recrear la tabla (mismo patrón que
    # ``f7a8b9c0d1e2``, que es el precedente del repo para agregar una FK a una tabla que
    # ya existe).
    if op.get_bind().dialect.name == 'sqlite':
        with op.batch_alter_table('clone_jobs', schema=None) as b:
            b.create_index(
                b.f('ix_clone_jobs_target_owner_user_id'),
                ['target_owner_user_id'],
                unique=False,
            )
            b.create_foreign_key(
                b.f('fk_clone_jobs_target_owner_user_id_server_users'),
                'server_users',
                ['target_owner_user_id'],
                ['id'],
                ondelete='SET NULL',
            )
    else:
        op.create_index(
            op.f('ix_clone_jobs_target_owner_user_id'),
            'clone_jobs',
            ['target_owner_user_id'],
            unique=False,
        )
        op.create_foreign_key(
            op.f('fk_clone_jobs_target_owner_user_id_server_users'),
            'clone_jobs',
            'server_users',
            ['target_owner_user_id'],
            ['id'],
            ondelete='SET NULL',
        )

    # ---- Relleno de datos: derivar la semántica EXACTA de cada job histórico ---- #
    # Se usa SQL portable (sin dialecto): booleanos comparados contra 1/0 y ``IS NULL``.
    clone_jobs = sa.table(
        'clone_jobs',
        sa.column('include_data', sa.Boolean),
        sa.column('clean_mode', sa.String),
        sa.column('target_mode', sa.String),
        sa.column('selection', sa.Text),
        sa.column('copy_intent', sa.String),
        sa.column('data_on_existing', sa.String),
        sa.column('is_full_clone', sa.Boolean),
    )
    bind = op.get_bind()
    # 1) La intención: había datos => estructura + datos; si no, solo estructura.
    bind.execute(
        clone_jobs.update()
        .where(clone_jobs.c.include_data == sa.true())
        .values(copy_intent='structure_and_data')
    )
    # 2) ``on_existing`` solo tiene sentido donde se copiaban filas, y reproduce la
    #    derivación histórica: upsert al preservar un destino existente, append si nacía
    #    limpio o nuevo.
    bind.execute(
        clone_jobs.update()
        .where(
            sa.and_(
                clone_jobs.c.include_data == sa.true(),
                clone_jobs.c.clean_mode == 'none',
                clone_jobs.c.target_mode == 'existing',
            )
        )
        .values(data_on_existing='upsert')
    )
    bind.execute(
        clone_jobs.update()
        .where(
            sa.and_(
                clone_jobs.c.include_data == sa.true(),
                clone_jobs.c.data_on_existing.is_(None),
            )
        )
        .values(data_on_existing='append')
    )
    # 3) Clon completo == no había selección parcial (la inferencia que se elimina).
    bind.execute(
        clone_jobs.update()
        .where(clone_jobs.c.selection.isnot(None))
        .values(is_full_clone=sa.false())
    )

    # ---- Expirar los planes pendientes (ver la nota 2 del docstring) ------------- #
    expired = sa.table(
        'clone_jobs',
        sa.column('status', sa.String),
        sa.column('expires_at', sa.DateTime),
    )
    bind.execute(
        expired.update()
        .where(expired.c.status == 'pending')
        .values(expires_at=sa.func.current_timestamp())
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == 'sqlite':
        # En el batch de SQLite la tabla se RECREA, así que la FK desaparece con su columna
        # y no hay que soltarla. El ÍNDICE sí hay que soltarlo explícitamente: el recreate
        # reflejaría y re-crearía un índice sobre una columna ya eliminada ("no such
        # column"). Mismo matiz documentado en ``f7a8b9c0d1e2``.
        with op.batch_alter_table('clone_jobs', schema=None) as b:
            b.drop_index(b.f('ix_clone_jobs_target_owner_user_id'))
            b.drop_column('target_fingerprint')
            b.drop_column('target_owner')
            b.drop_column('target_owner_user_id')
            b.drop_column('target_collation')
            b.drop_column('target_charset')
            b.drop_column('data_on_existing')
            b.drop_column('data_selection')
            b.drop_column('copy_intent')
            b.drop_column('is_full_clone')
        return

    # MySQL/MariaDB: la FK ANTES del índice que la respalda (al revés falla con "needed in a
    # foreign key constraint" — el defecto que este repo ya se comió una vez).
    op.drop_constraint(
        op.f('fk_clone_jobs_target_owner_user_id_server_users'),
        'clone_jobs',
        type_='foreignkey',
    )
    op.drop_index(op.f('ix_clone_jobs_target_owner_user_id'), table_name='clone_jobs')
    op.drop_column('clone_jobs', 'target_fingerprint')
    op.drop_column('clone_jobs', 'target_owner')
    op.drop_column('clone_jobs', 'target_owner_user_id')
    op.drop_column('clone_jobs', 'target_collation')
    op.drop_column('clone_jobs', 'target_charset')
    op.drop_column('clone_jobs', 'data_on_existing')
    op.drop_column('clone_jobs', 'data_selection')
    op.drop_column('clone_jobs', 'copy_intent')
    op.drop_column('clone_jobs', 'is_full_clone')
