"""
Tests de la API de LOTES de clonación (``/database-clone-batches``).

Mismo andamiaje que ``test_api_database_clones.py``: SQLite como BD de metadatos, adapter
falso para el plano en vivo, y los DOS workers ejecutados de forma SÍNCRONA en el test
(``clone_batch_runner.enqueue`` → ``run_batch`` inline, que a su vez llama a ``run_job``
directo). Así se ejercita el recorrido completo del lote sin motores reales ni esperas.

Lo que estos tests cuidan, por encima de la cobertura:
  - que el lote NO pueda borrar el destino por ningún camino;
  - que el token ate el CONJUNTO y no cada fila;
  - que una fila fallida no arrastre a las demás;
  - que el estado de una fila salga de UNA sola fuente (el job si existe, el ítem si no);
  - que el reintento no toque un destino con datos parciales.
"""

from contextlib import contextmanager
from datetime import datetime, timezone

import app.controllers.clone_batch_controller as cbc
import app.controllers.clone_controller as cc
import app.services.clone_batch_runner as clone_batch_runner
import app.services.clone_runner as clone_runner
from app.services.db_admin.data_copy import TableCopyResult
from app.services.db_admin.dtos import ColumnInfo, SchemaSnapshot, TableSchema, TableStat
from app.services.db_admin.migrations import StatementResult
from app.services.db_admin.schema_diff import RenderedStatement

BASE = "/api/v1/database-clone-batches"


# --------------------------------------------------------------------------- #
# Inventario                                                                   #
# --------------------------------------------------------------------------- #
def _server(admin_client, port, name=None, engine="mysql") -> int:
    r = admin_client.post(
        "/api/v1/servers",
        json={
            "name": name or f"srv{port}",
            "host": "10.0.0.5",
            "port": port,
            "engine": engine,
            "root_username": "root",
            "root_password": "pw",
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _server_name(admin_client, sid) -> str:
    r = admin_client.get(f"/api/v1/servers/{sid}")
    assert r.status_code == 200, r.text
    return r.json()["data"]["name"]


def _snapshot(db: str) -> SchemaSnapshot:
    tabla = TableSchema(
        database=db,
        table="productos",
        columns=[
            ColumnInfo(name="id", type="int", nullable=False),
            ColumnInfo(name="nombre", type="varchar(50)", nullable=True),
        ],
        primary_key=["id"],
        foreign_keys=[],
        indexes=[],
    )
    return SchemaSnapshot(database=db, source_engine="mysql", tables=[tabla])


class _FakeAdapter:
    """Adapter en memoria compartido por los dos lados del lote."""

    dialect = "mysql"

    def __init__(self, snaps: dict, existing: set):
        self.snaps = snaps
        self.existing = set(existing)
        self.created: list[str] = []
        self.dropped: list[str] = []

    def structural_snapshot(self, database):
        return self.snaps.get(database, SchemaSnapshot(database=database, source_engine="mysql"))

    def list_databases(self):
        return sorted(self.existing)

    def create_database(self, db_name, charset=None, collation=None, owner=None):
        self.existing.add(db_name)
        self.created.append(db_name)

    def drop_database(self, db_name):
        self.existing.discard(db_name)
        self.dropped.append(db_name)

    def external_fk_dependents(self, database):
        return []

    def list_table_stats(self, database, *, conn=None):
        snap = self.snaps.get(database)
        if snap is None:
            return []
        return [
            TableStat(
                table=t.table,
                estimated_rows=5,
                estimated_rows_known=True,
                has_primary_key=bool(t.primary_key),
            )
            for t in snap.tables
        ]

    def supports_charset_combination(self, charset, collation):
        return True

    def render_diff(self, diff):
        return [
            RenderedStatement(
                sql=f"-- {it.change_type} {it.object_type} {it.object_name}",
                object_type=it.object_type,
                object_name=it.object_name,
                change_type=it.change_type,
                phase=it.phase,
                risk=it.risk,
                down_sql=None,
                down_confirmed=False,
            )
            for it in diff.items
        ]


class _FakeRunner:
    @contextmanager
    def advisory_lock(self, target, *, engine, lock_key):
        yield

    def execute_adhoc(
        self,
        target,
        *,
        db_name,
        engine,
        lock_key,
        statements,
        already_locked=False,
        stop_on_error=True,
        disable_fk_checks=False,
    ):
        return [
            StatementResult(
                index=i,
                status="applied",
                error=None,
                execution_ms=1,
                executed_at=datetime.now(timezone.utc),
            )
            for i in range(len(statements))
        ]


def _fake_copy_tables(*, specs, **kwargs):
    return [TableCopyResult(table=s.table, status="applied", rows_copied=10) for s in specs]


def _install(
    monkeypatch, *, source_server_id, target_server_id, sources=("db_a", "db_b"),
    target_existing=(),
):
    """
    Instala DOS adapters fake —uno por servidor— y los dos workers en modo síncrono.

    Que sean dos y no uno importa: con un solo adapter compartido, una base del origen
    aparecería también en el destino y todo ``target_mode='new'`` con el mismo nombre se
    bloquearía. El despacho es por ``target.server_id``, igual que el real.

    Devuelve ``(origen, destino)``.
    """
    src_snaps = {name: _snapshot(name) for name in sources}
    dst_snaps = {name: _snapshot(name) for name in target_existing}
    origen = _FakeAdapter(src_snaps, set(sources))
    destino = _FakeAdapter(dst_snaps, set(target_existing))

    def _dispatch(target):
        return origen if target.server_id == source_server_id else destino

    monkeypatch.setattr(cc, "get_adapter", _dispatch)
    monkeypatch.setattr(cbc, "get_adapter", _dispatch)
    monkeypatch.setattr(cc, "MigrationRunner", _FakeRunner)
    monkeypatch.setattr(cc, "copy_tables", _fake_copy_tables)
    monkeypatch.setattr(
        clone_runner, "enqueue", lambda job_id: cc.CloneController().run_job(job_id)
    )
    monkeypatch.setattr(
        clone_batch_runner,
        "enqueue",
        lambda batch_id: cbc.CloneBatchController().run_batch(batch_id),
    )
    return origen, destino


def _plan(admin_client, src, dst, rows, **extra):
    body = {
        "source_server_id": src,
        "target_server_id": dst,
        "copy_intent": "structure_and_data",
        "rows": rows,
        **extra,
    }
    return admin_client.post(BASE, json=body)


def _pc(resp) -> dict:
    """``public_context`` de un error, que es donde vive el código estable."""
    return (resp.json().get("detail") or {}).get("public_context") or {}


def _execute(admin_client, batch, server_name):
    return admin_client.post(
        f"{BASE}/{batch['id']}/execute",
        json={"confirm_server_name": server_name, "confirm_token": batch["confirm_token"]},
    )


# --------------------------------------------------------------------------- #
# Plan: lo que el lote rechaza de entrada                                      #
# --------------------------------------------------------------------------- #
def test_plan_rechaza_clean_mode_destructivo(admin_client, monkeypatch):
    """Ningún camino del lote puede borrar el destino: ni el perfil ni un override."""
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst)

    r = _plan(
        admin_client, src, dst, [{"source_database_name": "db_a"}], clean_mode="drop_database"
    )
    # ``clean_mode`` ni siquiera existe en el schema del lote: extra="forbid" lo rechaza antes.
    assert r.status_code == 422, r.text

    r = _plan(
        admin_client,
        src,
        dst,
        [{"source_database_name": "db_a", "overrides": {"clean_mode": "objects"}}],
    )
    assert r.status_code == 422, r.text
    assert _pc(r)["code"] == "clone.batch_destructive_not_allowed"


def test_plan_rechaza_destinos_duplicados(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst)
    r = _plan(
        admin_client,
        src,
        dst,
        [
            {"source_database_name": "db_a", "target_database_name": "copia"},
            {"source_database_name": "db_b", "target_database_name": "copia"},
        ],
    )
    assert r.status_code == 422, r.text


def test_plan_rechaza_lote_por_encima_del_tope(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",))
    monkeypatch.setattr(cbc, "CLONE_BATCH_MAX_DATABASES", 2)
    rows = [
        {"source_database_name": "db_a", "target_database_name": f"c{i}"} for i in range(3)
    ]
    r = _plan(admin_client, src, dst, rows)
    assert r.status_code == 422, r.text
    assert _pc(r)["code"] == "clone.batch_too_large"


def test_plan_marca_filas_bloqueadas_sin_rebotar_el_lote(admin_client, monkeypatch):
    """
    Los motivos que dependen del estado del servidor se marcan POR FILA y todos de una vez.
    Rebotar la petición por el primero obligaría a corregir un lote de 12 bases de a una.
    """
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a", "db_b"), target_existing=("ya_existe",))

    r = _plan(
        admin_client,
        src,
        dst,
        [
            {"source_database_name": "db_a", "target_database_name": "copia_a"},
            # 'new' contra una base que ya existe → bloqueada, pero el lote se crea igual.
            {"source_database_name": "db_b", "target_database_name": "ya_existe"},
        ],
    )
    assert r.status_code == 201, r.text
    batch = r.json()["data"]

    items = admin_client.get(f"{BASE}/{batch['id']}/items").json()["data"]
    por_destino = {i["target_database_name"]: i for i in items}
    assert por_destino["copia_a"]["status"] == "pending"
    assert por_destino["ya_existe"]["status"] == "blocked"
    assert por_destino["ya_existe"]["error_code"] == "clone.batch_target_exists"


def test_plan_bloquea_destino_existente_que_no_sea_solo_datos(admin_client, monkeypatch):
    """
    Con destino existente el lote solo puede copiar filas: no borra, así que la estructura
    tiene que estar ya creada. Se dice al planear, no dentro del worker.
    """
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",), target_existing=("destino",))
    r = _plan(
        admin_client,
        src,
        dst,
        [
            {
                "source_database_name": "db_a",
                "target_database_name": "destino",
                "target_mode": "existing",
            }
        ],
    )
    assert r.status_code == 422, r.text
    # Ninguna fila quedó ejecutable → el lote entero se rechaza, con el motivo por fila.
    assert _pc(r)["code"] == "clone.batch_empty"
    assert _pc(r)["blocked"][0]["code"] == "clone.batch_existing_requires_data_only"


def test_plan_admite_destino_existente_en_solo_datos(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",), target_existing=("destino",))
    r = _plan(
        admin_client,
        src,
        dst,
        [
            {
                "source_database_name": "db_a",
                "target_database_name": "destino",
                "target_mode": "existing",
            }
        ],
        copy_intent="data_only",
        data_on_existing="append",
    )
    assert r.status_code == 201, r.text
    items = admin_client.get(f"{BASE}/{r.json()['data']['id']}/items").json()["data"]
    assert items[0]["status"] == "pending"


def test_plan_data_only_exige_on_existing(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",), target_existing=("destino",))
    r = _plan(
        admin_client,
        src,
        dst,
        [{"source_database_name": "db_a", "target_database_name": "destino",
          "target_mode": "existing"}],
        copy_intent="data_only",
    )
    assert r.status_code == 422, r.text
    assert _pc(r)["code"] == "clone.on_existing_required"


def test_el_nombre_destino_por_defecto_es_el_del_origen(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",))
    r = _plan(admin_client, src, dst, [{"source_database_name": "db_a"}])
    assert r.status_code == 201, r.text
    items = admin_client.get(f"{BASE}/{r.json()['data']['id']}/items").json()["data"]
    assert items[0]["target_database_name"] == "db_a"


# --------------------------------------------------------------------------- #
# Confirmación                                                                 #
# --------------------------------------------------------------------------- #
def test_execute_exige_el_nombre_del_servidor_destino(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",))
    batch = _plan(admin_client, src, dst, [{"source_database_name": "db_a"}]).json()["data"]

    r = admin_client.post(
        f"{BASE}/{batch['id']}/execute",
        json={"confirm_server_name": "otro-servidor", "confirm_token": batch["confirm_token"]},
    )
    assert r.status_code == 422, r.text
    assert _pc(r)["code"] == "clone.batch_confirm_server_mismatch"


def test_el_token_muere_si_cambia_una_sola_fila(admin_client, monkeypatch):
    """
    El token ata el CONJUNTO ordenado, no cada fila: con firmas por fila, agregar o quitar
    una base entre planificar y confirmar no invalidaría nada.
    """
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst)
    uno = _plan(admin_client, src, dst, [{"source_database_name": "db_a"}]).json()["data"]
    r_dos = _plan(
        admin_client,
        src,
        dst,
        [{"source_database_name": "db_a"}, {"source_database_name": "db_b"}],
    )
    assert r_dos.status_code == 201, r_dos.text
    dos = r_dos.json()["data"]
    assert uno["confirm_token"] != dos["confirm_token"]

    # El token de un lote no sirve para otro.
    r = admin_client.post(
        f"{BASE}/{dos['id']}/execute",
        json={
            "confirm_server_name": _server_name(admin_client, dst),
            "confirm_token": uno["confirm_token"],
        },
    )
    assert r.status_code == 422, r.text
    assert _pc(r)["code"] == "clone.batch_token_mismatch"


def test_no_se_puede_ejecutar_dos_veces(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",))
    batch = _plan(admin_client, src, dst, [{"source_database_name": "db_a"}]).json()["data"]
    nombre = _server_name(admin_client, dst)

    assert _execute(admin_client, batch, nombre).status_code == 200
    segundo = _execute(admin_client, batch, nombre)
    assert segundo.status_code == 409, segundo.text
    assert _pc(segundo)["code"] == "clone.batch_not_pending"


# --------------------------------------------------------------------------- #
# Recorrido                                                                    #
# --------------------------------------------------------------------------- #
def test_recorrido_completo_clona_todas_las_bases_en_orden(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    origen, destino = _install(
        monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a", "db_b", "db_c")
    )
    batch = _plan(
        admin_client,
        src,
        dst,
        [
            {"source_database_name": "db_a", "target_database_name": "c_a"},
            {"source_database_name": "db_b", "target_database_name": "c_b"},
            {"source_database_name": "db_c", "target_database_name": "c_c"},
        ],
    ).json()["data"]

    r = _execute(admin_client, batch, _server_name(admin_client, dst))
    assert r.status_code == 200, r.text

    final = admin_client.get(f"{BASE}/{batch['id']}").json()["data"]
    assert final["status"] == "done"
    assert final["counts"]["succeeded"] == 3
    assert final["counts"]["total"] == 3
    # En SERIE y en orden de ``seq``: el destino recibe las bases en el orden declarado.
    assert destino.created == ["c_a", "c_b", "c_c"]

    items = admin_client.get(f"{BASE}/{batch['id']}/items").json()["data"]
    assert [i["seq"] for i in items] == [1, 2, 3]
    # Cada fila quedó ligada a un CloneJob real, con su pantalla de detalle de siempre.
    assert all(i["clone_job_id"] is not None for i in items)
    assert all(i["status"] == "succeeded" for i in items)
    detalle = admin_client.get(f"/api/v1/database-clones/{items[0]['clone_job_id']}")
    assert detalle.status_code == 200, detalle.text


def test_una_fila_que_falla_no_arrastra_a_las_demas(admin_client, monkeypatch):
    """El lote cierra ``partial`` y las otras bases se clonan igual."""
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(
        monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a", "db_b", "db_c")
    )

    original = _fake_copy_tables

    def _copia_que_falla_en_la_segunda(*, specs, **kwargs):
        # ``dest_db`` identifica la base destino de esta copia.
        if kwargs.get("dest_db") == "c_b":
            raise RuntimeError("fallo simulado del motor")
        return original(specs=specs, **kwargs)

    monkeypatch.setattr(cc, "copy_tables", _copia_que_falla_en_la_segunda)

    batch = _plan(
        admin_client,
        src,
        dst,
        [
            {"source_database_name": "db_a", "target_database_name": "c_a"},
            {"source_database_name": "db_b", "target_database_name": "c_b"},
            {"source_database_name": "db_c", "target_database_name": "c_c"},
        ],
    ).json()["data"]
    assert _execute(admin_client, batch, _server_name(admin_client, dst)).status_code == 200

    final = admin_client.get(f"{BASE}/{batch['id']}").json()["data"]
    assert final["status"] == "partial"
    assert final["counts"]["succeeded"] == 2
    assert final["counts"]["failed"] == 1

    items = {
        i["target_database_name"]: i
        for i in admin_client.get(f"{BASE}/{batch['id']}/items").json()["data"]
    }
    assert items["c_b"]["status"] == "failed"
    # La tercera se ejecutó DESPUÉS de la fallida: el lote no se cortó.
    assert items["c_c"]["status"] == "succeeded"


def test_el_estado_de_la_fila_sale_de_una_sola_fuente(admin_client, monkeypatch):
    """
    Mientras no hay job, manda ``outcome``; en cuanto lo hay, manda el job y ``outcome`` queda
    NULL. Es lo que hace imposible que las dos versiones del estado diverjan.
    """
    from app.models.clone_batch import CloneBatchItem

    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",))
    batch = _plan(admin_client, src, dst, [{"source_database_name": "db_a"}]).json()["data"]

    controller = cbc.CloneBatchController()
    session = controller._session()
    try:
        fila = session.query(CloneBatchItem).filter_by(batch_id=batch["id"]).one()
        assert fila.outcome == "pending" and fila.clone_job_id is None
    finally:
        session.close()

    assert _execute(admin_client, batch, _server_name(admin_client, dst)).status_code == 200

    session = controller._session()
    try:
        fila = session.query(CloneBatchItem).filter_by(batch_id=batch["id"]).one()
        assert fila.clone_job_id is not None
        assert fila.outcome is None, "con job, el outcome tiene que quedar NULL"
    finally:
        session.close()
    items = admin_client.get(f"{BASE}/{batch['id']}/items").json()["data"]
    assert items[0]["status"] == "succeeded"


# --------------------------------------------------------------------------- #
# Cancelación                                                                  #
# --------------------------------------------------------------------------- #
def test_cancelar_antes_de_arrancar_deja_las_filas_canceladas(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a", "db_b"))
    # El worker NO corre: se simula "cancelaron entre el execute y el arranque del pool".
    monkeypatch.setattr(clone_batch_runner, "enqueue", lambda batch_id: None)
    batch = _plan(
        admin_client,
        src,
        dst,
        [
            {"source_database_name": "db_a", "target_database_name": "c_a"},
            {"source_database_name": "db_b", "target_database_name": "c_b"},
        ],
    ).json()["data"]
    assert _execute(admin_client, batch, _server_name(admin_client, dst)).status_code == 200

    r = admin_client.post(f"{BASE}/{batch['id']}/cancel")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["cancel_requested"] is True

    # Ahora sí corre el worker: tiene que cortar en la primera fila sin clonar nada.
    cbc.CloneBatchController().run_batch(batch["id"])
    final = admin_client.get(f"{BASE}/{batch['id']}").json()["data"]
    assert final["status"] == "canceled"
    assert final["counts"].get("canceled") == 2
    assert final["counts"].get("succeeded", 0) == 0


def test_cancelar_un_lote_terminado_da_409(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",))
    batch = _plan(admin_client, src, dst, [{"source_database_name": "db_a"}]).json()["data"]
    assert _execute(admin_client, batch, _server_name(admin_client, dst)).status_code == 200

    r = admin_client.post(f"{BASE}/{batch['id']}/cancel")
    assert r.status_code == 409, r.text
    assert _pc(r)["code"] == "clone.batch_not_pending"


# --------------------------------------------------------------------------- #
# Reintento                                                                    #
# --------------------------------------------------------------------------- #
def test_el_reintento_excluye_las_filas_con_datos_parciales(admin_client, monkeypatch):
    """
    La regla del reintento es el DESTINO, no el estado del job: una fila que alcanzó a copiar
    filas dejó datos parciales commiteados (la copia no es reanudable) y el lote no puede
    limpiar. Reintentarla agregaría encima y duplicaría datos en silencio.
    """
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(
        monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a", "db_b", "db_c")
    )

    def _copias_mixtas(*, specs, **kwargs):
        destino = kwargs.get("dest_db")
        if destino == "c_b":
            # Falló DESPUÉS de escribir: el destino queda con filas parciales.
            return [TableCopyResult(table=s.table, status="failed", rows_copied=7) for s in specs]
        if destino == "c_c":
            # Falló SIN escribir una sola fila: el destino quedó intacto.
            return [TableCopyResult(table=s.table, status="failed", rows_copied=0) for s in specs]
        return [TableCopyResult(table=s.table, status="applied", rows_copied=10) for s in specs]

    monkeypatch.setattr(cc, "copy_tables", _copias_mixtas)

    batch = _plan(
        admin_client,
        src,
        dst,
        [
            {"source_database_name": "db_a", "target_database_name": "c_a"},
            {"source_database_name": "db_b", "target_database_name": "c_b"},
            {"source_database_name": "db_c", "target_database_name": "c_c"},
        ],
    ).json()["data"]
    assert _execute(admin_client, batch, _server_name(admin_client, dst)).status_code == 200

    r = admin_client.get(f"{BASE}/{batch['id']}/retry-candidates")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    manual = {i["target_database_name"]: i["reason"] for i in data["needs_manual"]}
    retryable = {i["target_database_name"] for i in data["retryable"]}

    # Las DOS fallidas requieren atención, por motivos DISTINTOS: ambas creaban el destino y
    # el intento anterior alcanzó a crearlo, así que ninguna quedó intacta.
    assert set(manual) == {"c_b", "c_c"}
    assert "datos parciales" in manual["c_b"], "la que escribió filas se informa por eso"
    assert "crear la base" in manual["c_c"], "la que solo creó la base, por lo otro"
    assert not retryable, "ninguna fila de este lote quedó con el destino intacto"
    assert "c_a" not in set(manual) | retryable, "la exitosa no entra en ningún grupo"


def test_retry_failed_crea_un_lote_nuevo_pendiente_de_confirmar(admin_client, monkeypatch):
    """
    El caso que el reintento existe para resolver: un reinicio dejó filas sin arrancar. Ésas
    tienen el destino intacto y se pueden relanzar; las que ya corrieron, no.
    """
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a", "db_b"))
    # El worker nunca arranca: las dos filas quedan sin job, como tras un reinicio.
    monkeypatch.setattr(clone_batch_runner, "enqueue", lambda batch_id: None)

    batch = _plan(
        admin_client,
        src,
        dst,
        [
            {"source_database_name": "db_a", "target_database_name": "c_a"},
            {"source_database_name": "db_b", "target_database_name": "c_b"},
        ],
    ).json()["data"]
    assert _execute(admin_client, batch, _server_name(admin_client, dst)).status_code == 200
    assert cbc.CloneBatchController().sweep_interrupted() == 1

    r = admin_client.post(f"{BASE}/{batch['id']}/retry-failed")
    assert r.status_code == 201, r.text
    nuevo = r.json()["data"]
    assert nuevo["id"] != batch["id"]
    # Vuelve a pasar por la confirmación agregada: no se reejecuta con un click.
    assert nuevo["status"] == "pending"
    assert nuevo["total"] == 2
    items = admin_client.get(f"{BASE}/{nuevo['id']}/items").json()["data"]
    assert [i["target_database_name"] for i in items] == ["c_a", "c_b"]


def test_retry_failed_sin_candidatos_da_422(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",))
    batch = _plan(
        admin_client, src, dst, [{"source_database_name": "db_a", "target_database_name": "c_a"}]
    ).json()["data"]
    assert _execute(admin_client, batch, _server_name(admin_client, dst)).status_code == 200

    r = admin_client.post(f"{BASE}/{batch['id']}/retry-failed")
    assert r.status_code == 422, r.text
    assert _pc(r)["code"] == "clone.batch_retry_not_eligible"


# --------------------------------------------------------------------------- #
# Barrido de arranque                                                          #
# --------------------------------------------------------------------------- #
def test_sweep_cierra_los_lotes_que_un_reinicio_dejo_corriendo(admin_client, monkeypatch):
    """
    Se simula "el proceso murió después de encolar": el worker nunca arranca y el lote queda
    ``running`` con sus filas sin tocar. El barrido lo cierra y NO re-encola nada.
    """
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a", "db_b"))
    monkeypatch.setattr(clone_batch_runner, "enqueue", lambda batch_id: None)

    batch = _plan(
        admin_client,
        src,
        dst,
        [
            {"source_database_name": "db_a", "target_database_name": "c_a"},
            {"source_database_name": "db_b", "target_database_name": "c_b"},
        ],
    ).json()["data"]
    assert _execute(admin_client, batch, _server_name(admin_client, dst)).status_code == 200
    assert admin_client.get(f"{BASE}/{batch['id']}").json()["data"]["status"] == "running"

    assert cbc.CloneBatchController().sweep_interrupted() == 1

    final = admin_client.get(f"{BASE}/{batch['id']}").json()["data"]
    assert final["status"] == "interrupted"
    assert final["error"]
    items = admin_client.get(f"{BASE}/{batch['id']}/items").json()["data"]
    assert all(i["status"] == "skipped" for i in items)


def test_sweep_no_toca_un_lote_pendiente(admin_client, monkeypatch):
    """Un lote ``pending`` es un plan legítimo que todavía nadie confirmó: no se cierra."""
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst, sources=("db_a",))
    batch = _plan(admin_client, src, dst, [{"source_database_name": "db_a"}]).json()["data"]

    assert cbc.CloneBatchController().sweep_interrupted() == 0
    assert admin_client.get(f"{BASE}/{batch['id']}").json()["data"]["status"] == "pending"


# --------------------------------------------------------------------------- #
# Historial                                                                    #
# --------------------------------------------------------------------------- #
def test_el_historial_lista_los_lotes_del_mas_nuevo_al_mas_viejo(admin_client, monkeypatch):
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst)
    primero = _plan(admin_client, src, dst, [{"source_database_name": "db_a"}]).json()["data"]
    segundo = _plan(admin_client, src, dst, [{"source_database_name": "db_b"}]).json()["data"]

    r = admin_client.get(BASE)
    assert r.status_code == 200, r.text
    ids = [b["id"] for b in r.json()["data"]]
    assert ids[:2] == [segundo["id"], primero["id"]]
    assert r.json()["pagination"]["total"] == 2
