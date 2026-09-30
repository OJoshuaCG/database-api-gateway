"""Autoría de las versiones de blueprint (``model_migrations.created_by_*``) + backfill

Hasta acá una versión de blueprint no sabía quién la había escrito: la autoría vivía SOLO en
la fila ``migration.create`` de ``audit_log``, sin FK entre las dos tablas y con el id de la
versión enterrado en el texto libre de ``detail``. Responder "¿quién escribió la 0007?"
exigía parsear la auditoría.

**Tres columnas nullable, sin FK.** ``created_by_admin_id`` (``Integer``),
``created_by_username`` (``String(128)``, el ancho de ``audit_log.admin_username``) y
``created_by_actor_type`` (``String(16)``, el vocabulario de ``audit_log.actor_type``:
``admin`` | ``api_token``). Mismo molde que ``ExportJob.created_by_admin_id`` y los lotes de
clonado y de conversión de collation: sin FK al admin para que borrarlo no arrastre ni
bloquee el historial, y el username desnormalizado para poder mostrar quién fue aunque la
fila del usuario ya no exista. NULL significa **autor desconocido**, y es la verdad para lo
que el backfill no alcanza: no se inventa un autor.

BACKFILL DESDE ``audit_log``
----------------------------
``create_migration`` audita ``action='migration.create'``, ``target_type='database_model'``,
``target_id=<model_id>`` y ``detail="migración 0007 creada (id=42)"``. Para cada fila así se
extrae el id de la versión con ``\\(id=(\\d+)\\)`` y se copian ``admin_id``,
``admin_username`` y ``actor_type`` a la fila de ``model_migrations`` con ese id, **solo si**:

- la versión existe y su ``model_id`` coincide con el ``target_id`` de la auditoría — un id
  que apunta a otro blueprint es una atribución que no se puede sostener, así que se descarta;
- sus tres ``created_by_*`` siguen en NULL — el backfill nunca pisa una autoría ya escrita,
  lo que además lo hace idempotente ante un reintento;
- la fila de auditoría identifica a alguien (``admin_id`` o ``admin_username`` no nulos).

Si varias filas apuntan al mismo id gana la MÁS ANTIGUA (``created_at``, y el ``id`` de la
auditoría como desempate): la creación es el primer evento; lo posterior no la describe.

La decisión vive en ``plan_author_backfill``, una función PURA (sin BD) para poder testearla
sin Alembic; ``upgrade`` solo lee, llama y escribe en lotes, con SQL portable (sin
``UPDATE … JOIN``, sin ``FILTER``, sin regex del motor) para MariaDB/MySQL, PostgreSQL y
SQLite.

LÍMITES — lo que queda en NULL, a propósito
-------------------------------------------
- Entradas de ``migration.create`` sin ``(id=N)`` en ``detail`` (formato anterior o editado):
  no hay a qué versión atribuirlas.
- Filas de auditoría purgadas o que nunca se escribieron (``audit.record`` es best-effort).
- Versiones creadas por caminos que no auditaban ``migration.create``: las N versiones de un
  blueprint creado desde snapshot auditan una sola fila ``database_model.from_snapshot`` sin
  ids de versión, así que quedan como desconocidas.
- Un id reutilizado tras borrar la versión original (posible en SQLite) se atribuye solo si
  además coincide el blueprint; es el mejor filtro disponible, no una garantía.

**Idempotente paso a paso**, como exige el repo: MySQL/MariaDB no tienen DDL transaccional, y
si esto muere a mitad el próximo arranque lo corre entero otra vez. Cada columna se agrega solo
si falta, y el backfill solo escribe sobre filas sin autoría.

Los ``comment=`` replican los del modelo: sin ellos ``alembic check`` reporta drift.

Revision ID: c5e7a9b1d3f6
Revises: b3c4d5e6f7a8
"""

import re
from collections.abc import Iterable, Mapping
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "c5e7a9b1d3f6"
down_revision: Union[str, None] = "b3c4d5e6f7a8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLE = "model_migrations"
_BATCH = 500

#: El id de la versión dentro de ``detail``: ``"migración 0007 creada (id=42)"``.
DETAIL_ID_RE = re.compile(r"\(id=(\d+)\)")


def _columnas_nuevas() -> list[sa.Column]:
    return [
        sa.Column(
            "created_by_admin_id",
            sa.Integer(),
            nullable=True,
            comment="Admin o token que creó la versión (sin FK: historial desacoplado). NULL = desconocido",
        ),
        sa.Column(
            "created_by_username",
            sa.String(length=128),
            nullable=True,
            comment="Nombre del actor al crear la versión (mismo ancho que audit_log)",
        ),
        sa.Column(
            "created_by_actor_type",
            sa.String(length=16),
            nullable=True,
            comment="'admin' | 'api_token' (vocabulario de audit_log.actor_type). NULL = desconocido",
        ),
    ]


def plan_author_backfill(
    audit_rows: Iterable[Mapping],
    migrations: Mapping[int, Mapping],
) -> dict[int, dict]:
    """
    Decide qué autoría escribir en cada versión. PURA: no toca la BD.

    ``audit_rows``: filas de ``audit_log`` ya filtradas por ``action='migration.create'`` y
    ``target_type='database_model'``, con las claves ``id``, ``created_at``, ``target_id``,
    ``detail``, ``admin_id``, ``admin_username`` y ``actor_type``.

    ``migrations``: ``{id: {"model_id", "created_by_admin_id", "created_by_username",
    "created_by_actor_type"}}`` de las versiones existentes.

    Devuelve ``{migration_id: {"created_by_admin_id", "created_by_username",
    "created_by_actor_type"}}`` solo para las versiones a completar. Las reglas y sus porqués
    están en el docstring del módulo.
    """

    def _orden(row: Mapping) -> tuple:
        # Más antigua primero; sin ``created_at`` va al final. El id de la auditoría desempata
        # (y es el orden real de inserción cuando dos filas caen en el mismo segundo).
        created_at = row.get("created_at")
        return (created_at is None, created_at, row.get("id") or 0)

    plan: dict[int, dict] = {}
    for row in sorted(audit_rows, key=_orden):
        match = DETAIL_ID_RE.search(row.get("detail") or "")
        if match is None:
            continue
        migration_id = int(match.group(1))
        if migration_id in plan:
            continue  # ya la atribuyó una fila más antigua
        migration = migrations.get(migration_id)
        if migration is None or migration.get("model_id") != row.get("target_id"):
            continue
        if any(
            migration.get(k) is not None
            for k in ("created_by_admin_id", "created_by_username", "created_by_actor_type")
        ):
            continue
        if row.get("admin_id") is None and row.get("admin_username") is None:
            continue  # la auditoría no identifica a nadie: sigue desconocido
        plan[migration_id] = {
            "created_by_admin_id": row.get("admin_id"),
            "created_by_username": row.get("admin_username"),
            "created_by_actor_type": row.get("actor_type") or "admin",
        }
    return plan


def _columnas(bind) -> set[str]:
    # Inspector NUEVO en cada llamada: cachea, y entre pasos de esta migración el esquema cambia.
    return {c["name"] for c in sa.inspect(bind).get_columns(_TABLE)}


def _leer_auditoria(bind) -> list[dict]:
    """Filas candidatas de ``audit_log``, paginadas por id (keyset: portable y sin OFFSET)."""
    audit_cols = {c["name"] for c in sa.inspect(bind).get_columns("audit_log")}
    actor_type = "actor_type" if "actor_type" in audit_cols else "NULL AS actor_type"
    sql = sa.text(
        "SELECT id, created_at, target_id, detail, admin_id, admin_username, "
        f"{actor_type} FROM audit_log "
        "WHERE action = :action AND target_type = :target_type AND id > :after "
        "ORDER BY id LIMIT :lim"
    )
    rows: list[dict] = []
    after = 0
    while True:
        page = bind.execute(
            sql,
            {
                "action": "migration.create",
                "target_type": "database_model",
                "after": after,
                "lim": _BATCH,
            },
        ).mappings().all()
        if not page:
            return rows
        rows.extend(dict(r) for r in page)
        after = page[-1]["id"]


def _leer_versiones(bind, ids: set[int]) -> dict[int, dict]:
    """Las versiones nombradas por la auditoría, en lotes (``IN`` acotado)."""
    sql = sa.text(
        "SELECT id, model_id, created_by_admin_id, created_by_username, "
        f"created_by_actor_type FROM {_TABLE} WHERE id IN :ids"
    ).bindparams(sa.bindparam("ids", expanding=True))
    ordenados = sorted(ids)
    out: dict[int, dict] = {}
    for i in range(0, len(ordenados), _BATCH):
        for r in bind.execute(sql, {"ids": ordenados[i : i + _BATCH]}).mappings():
            out[r["id"]] = dict(r)
    return out


def _backfill(bind) -> None:
    audit_rows = _leer_auditoria(bind)
    ids = {
        int(m.group(1))
        for r in audit_rows
        if (m := DETAIL_ID_RE.search(r.get("detail") or "")) is not None
    }
    if not ids:
        return
    plan = plan_author_backfill(audit_rows, _leer_versiones(bind, ids))
    if not plan:
        return
    # El guard de NULL se repite en el WHERE: si otro proceso escribió la autoría entre la
    # lectura y esta escritura, no se pisa.
    update = sa.text(
        f"UPDATE {_TABLE} SET created_by_admin_id = :admin_id, "
        "created_by_username = :username, created_by_actor_type = :actor_type "
        "WHERE id = :id AND created_by_admin_id IS NULL AND created_by_username IS NULL "
        "AND created_by_actor_type IS NULL"
    )
    params = [
        {
            "id": migration_id,
            "admin_id": a["created_by_admin_id"],
            "username": a["created_by_username"],
            "actor_type": a["created_by_actor_type"],
        }
        for migration_id, a in sorted(plan.items())
    ]
    for i in range(0, len(params), _BATCH):
        bind.execute(update, params[i : i + _BATCH])


def upgrade() -> None:
    bind = op.get_bind()
    existentes = _columnas(bind)
    for columna in _columnas_nuevas():
        if columna.name not in existentes:
            op.add_column(_TABLE, columna)
    _backfill(bind)


def downgrade() -> None:
    bind = op.get_bind()
    existentes = _columnas(bind)
    for columna in reversed(_columnas_nuevas()):
        if columna.name in existentes:
            op.drop_column(_TABLE, columna.name)
