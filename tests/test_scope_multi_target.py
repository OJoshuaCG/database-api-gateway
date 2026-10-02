"""
Operaciones multi-destino con capa 2: se evalúa POR ÍTEM y los prohibidos se omiten.

Regla (decisión de negocio, vinculante):

- ``database_ids`` / filas / nombres EXPLÍCITOS con alguno prohibido: 403 en todo el pedido y no
  corre nada;
- conjunto IMPLÍCITO: los prohibidos se omiten y vuelven SOLO con el id que mandó el cliente y
  ``error_code = "access.forbidden"`` (sin nombre), no se persisten ni consumen el tope;
- ninguno permitido: 403.

El actor es ``owner`` de base con un grant ``viewer`` sobre producción: dueño donde hay desarrollo,
lector donde hay producción. Los lotes se PLANIFICAN antes de otorgar el grant (el plan de un
actor restringido ya es 403 si nombra una fila de producción) y se confirman después.

Las bases se identifican por nombre en un servidor: la clasificación sale de las filas del
inventario sembradas a mano, así que ningún caso conecta a un motor real.
"""

import pytest
from sqlalchemy import text

import app.controllers.clone_batch_controller as cbc
import app.services.clone_batch_runner as clone_batch_runner
from app.core.database import Database
from app.models.clone_batch import CLONE_BATCH_ITEM_PENDING
from app.core.scope import (
    ScopePoint,
    ScopeTarget,
    assert_layer2,
    partition_by_scope,
    partition_for_batch,
)
from app.exceptions import AppHttpException
from app.services.capability_catalog import Capability, GatewayRole
from app.services.db_admin.migrations import MigrationRunner
from tests.scope_helpers import actor_con, env_id, otorgar, sembrar_bd
from tests.test_api_clone_batches import BASE as CLONE_BATCHES
from tests.test_api_clone_batches import _execute, _install, _plan, _server, _server_name

_API = "/api/v1"
_COLLATION = "/api/v1/database-models"


def _forbidden(r) -> bool:
    return (
        r.status_code == 403
        and r.json()["detail"]["public_context"]["code"] == "access.forbidden"
    )


# --------------------------------------------------------------------------- #
# Mecanismo: partición por ítem, sin BD ni HTTP                                #
# --------------------------------------------------------------------------- #


def _restringido():
    """Dueño de base, lector en el entorno 2."""
    return actor_con(GatewayRole.OWNER, [("environment", 2, GatewayRole.VIEWER)])


def _punto(item_id, entorno):
    return ScopePoint(environment_id=entorno, server_id=None, item_id=item_id)


def test_an_item_with_two_points_is_forbidden_if_either_is():
    """Una fila de clonación se permite solo si se permiten el origen Y el destino."""
    puntos = [_punto(1, 1), _punto(1, 1), _punto(2, 1), _punto(2, 2)]
    part = partition_by_scope(
        actor=_restringido(), capability=Capability.CLONES_EXECUTE, points=puntos
    )
    assert part.permitted == (1,)
    assert part.forbidden == (2,)


def test_any_quantifier_groups_the_points_of_each_item(monkeypatch):
    """
    ``any`` prueba que haya al menos UN ítem completo permitido: dos puntos sueltos, uno permitido
    y otro prohibido, de un MISMO ítem, no alcanzan.
    """
    import app.core.scope as scope_mod

    destino = ScopeTarget("x", quantifier="any")
    monkeypatch.setattr(
        scope_mod, "resolve_points", lambda t: [_punto(7, 1), _punto(7, 2)]
    )
    with pytest.raises(AppHttpException) as exc:
        assert_layer2(_restringido(), Capability.CLONES_EXECUTE, destino)
    assert exc.value.status_code == 403

    monkeypatch.setattr(
        scope_mod, "resolve_points", lambda t: [_punto(7, 1), _punto(7, 2), _punto(8, 1)]
    )
    assert_layer2(_restringido(), Capability.CLONES_EXECUTE, destino)


def _explotar():
    raise AssertionError("se evaluaron puntos sin necesidad")


@pytest.mark.parametrize(
    "admin",
    [
        None,
        {"id": 1, "username": "worker"},
        actor_con(GatewayRole.OWNER),  # sin grants por alcance
    ],
)
def test_partition_for_batch_is_a_no_op_when_layer2_does_not_apply(admin):
    assert (
        partition_for_batch(
            admin=admin,
            capability=Capability.CLONES_EXECUTE,
            points_fn=_explotar,
            explicit=True,
        )
        is None
    )


def test_partition_for_batch_implicit_returns_the_split_and_explicit_raises():
    puntos = lambda: [_punto(1, 1), _punto(2, 2)]  # noqa: E731
    part = partition_for_batch(
        admin=_restringido(),
        capability=Capability.CLONES_EXECUTE,
        points_fn=puntos,
        explicit=False,
    )
    assert (part.permitted, part.forbidden) == ((1,), (2,))

    with pytest.raises(AppHttpException) as exc:
        partition_for_batch(
            admin=_restringido(),
            capability=Capability.CLONES_EXECUTE,
            points_fn=puntos,
            explicit=True,
        )
    assert exc.value.public_context["code"] == "access.forbidden"


def test_partition_for_batch_with_zero_permitted_raises():
    with pytest.raises(AppHttpException) as exc:
        partition_for_batch(
            admin=_restringido(),
            capability=Capability.CLONES_EXECUTE,
            points_fn=lambda: [_punto(1, 2), _punto(2, 2)],
            explicit=False,
        )
    assert exc.value.status_code == 403


# --------------------------------------------------------------------------- #
# apply-all                                                                    #
# --------------------------------------------------------------------------- #

_SAFE_SQL = "CREATE TABLE t1 (id INT PRIMARY KEY)"


def _bp(admin_client, slug: str) -> int:
    r = admin_client.post(f"{_COLLATION}", json={"name": slug, "slug": slug})
    assert r.status_code == 201, r.text
    model_id = r.json()["data"]["id"]
    r = admin_client.post(
        f"{_COLLATION}/{model_id}/migrations",
        json={"version": "0001", "name": "t", "up_sql": _SAFE_SQL},
    )
    assert r.status_code == 201, r.text
    return model_id


def _bd_api(admin_client, sid, oid, model_id, name, environment_id) -> int:
    r = admin_client.post(
        f"{_API}/managed-databases",
        json={
            "name": name,
            "server_id": sid,
            "owner_id": oid,
            "model_id": model_id,
            "environment_id": environment_id,
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


@pytest.fixture()
def apply_all_env(admin_client, server_payload, monkeypatch):
    """Blueprint con una BD de producción (id menor) y una de desarrollo; el motor se mockea."""
    aplicadas: list = []
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: None)
    monkeypatch.setattr(
        MigrationRunner, "apply", lambda self, *a, **k: aplicadas.append(1) or []
    )
    prod, dev = env_id("production"), env_id("development")
    sid = admin_client.post(f"{_API}/servers", json=server_payload()).json()["data"]["id"]
    oid = admin_client.post(
        f"{_API}/server-users", json={"server_id": sid, "username": "own1"}
    ).json()["data"]["id"]
    model_id = _bp(admin_client, "bp-multi")
    db_prod = _bd_api(admin_client, sid, oid, model_id, "dbprod", prod)
    db_dev = _bd_api(admin_client, sid, oid, model_id, "dbdev", dev)
    otorgar("environment", prod, "viewer")
    return {
        "model_id": model_id,
        "db_prod": db_prod,
        "db_dev": db_dev,
        "aplicadas": aplicadas,
        "url": f"{_COLLATION}/{model_id}/migrations/apply-all",
    }


def test_apply_all_implicit_set_skips_the_forbidden_item_without_a_name(
    admin_client, apply_all_env
):
    esc = apply_all_env
    r = admin_client.post(esc["url"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    assert data["processed"] == 1
    por_id = {i["managed_database_id"]: i for i in data["results"]}
    assert por_id[esc["db_dev"]]["ok"] is True
    omitido = por_id[esc["db_prod"]]
    assert omitido["ok"] is False
    assert omitido["error_code"] == "access.forbidden"
    # Sin nombre, servidor ni entorno de la base que el actor no puede tocar.
    assert omitido["database_name"] is None
    assert omitido["server_id"] is None
    assert omitido["environment_slug"] is None
    assert "dbprod" not in r.text
    assert len(esc["aplicadas"]) == 1


def test_apply_all_forbidden_items_do_not_consume_max_databases(admin_client, apply_all_env):
    """La BD de producción tiene el id menor: con tope 1 igual se procesa la de desarrollo."""
    esc = apply_all_env
    r = admin_client.post(esc["url"], params={"max_databases": 1})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["processed"] == 1
    assert data["matched_databases"] == 1
    assert [i["ok"] for i in data["results"] if i["managed_database_id"] == esc["db_dev"]] == [
        True
    ]


def test_apply_all_explicit_ids_with_a_forbidden_one_is_a_403_and_runs_nothing(
    admin_client, apply_all_env
):
    esc = apply_all_env
    r = admin_client.post(
        esc["url"], params={"database_ids": [esc["db_dev"], esc["db_prod"]]}
    )
    assert _forbidden(r), r.text
    assert esc["aplicadas"] == []


def test_apply_all_explicit_permitted_ids_run(admin_client, apply_all_env):
    esc = apply_all_env
    r = admin_client.post(esc["url"], params={"database_ids": [esc["db_dev"]]})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["processed"] == 1
    assert len(esc["aplicadas"]) == 1


def test_apply_all_with_zero_permitted_is_a_403(admin_client, server_payload, monkeypatch):
    monkeypatch.setattr(MigrationRunner, "get_current_version", lambda self, *a, **k: None)
    prod = env_id("production")
    sid = admin_client.post(f"{_API}/servers", json=server_payload()).json()["data"]["id"]
    oid = admin_client.post(
        f"{_API}/server-users", json={"server_id": sid, "username": "own1"}
    ).json()["data"]["id"]
    model_id = _bp(admin_client, "bp-solo-prod")
    _bd_api(admin_client, sid, oid, model_id, "dbprod", prod)
    otorgar("environment", prod, "viewer")

    r = admin_client.post(f"{_COLLATION}/{model_id}/migrations/apply-all")
    assert _forbidden(r), r.text


def test_apply_all_without_scope_grants_is_unchanged(admin_client, apply_all_env):
    """Sin grants por alcance la capa 2 no existe: se procesan las dos y no hay omitidos."""
    esc = apply_all_env
    with Database().engine.begin() as conn:
        conn.execute(text("DELETE FROM access_grants WHERE user_id = 1"))
    r = admin_client.post(esc["url"])
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["processed"] == 2
    assert all(i["error_code"] != "access.forbidden" for i in data["results"])


# --------------------------------------------------------------------------- #
# Lote de collation                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def collation_env(admin_client, monkeypatch):
    """Blueprint con dos BDs activas, ``db_a`` en producción (id menor) y ``db_b`` en desarrollo."""
    from tests import test_api_collation_batches as tcb

    tcb._install(monkeypatch)
    prod, dev = env_id("production"), env_id("development")
    model_id, _sid, ids = tcb._setup(
        admin_client, 3961, names=("db_a", "db_b"), environments={"db_a": prod, "db_b": dev}
    )
    return {"tcb": tcb, "model_id": model_id, "ids": ids, "prod": prod}


def test_collation_batch_create_skips_forbidden_databases(admin_client, collation_env):
    esc = collation_env
    otorgar("environment", esc["prod"], "viewer")

    r = esc["tcb"]._plan(admin_client, esc["model_id"])
    assert r.status_code == 201, r.text
    data = r.json()["data"]

    assert data["total_eligible"] == 1
    por_id = {d["managed_database_id"]: d for d in data["databases"]}
    assert por_id[esc["ids"]["db_b"]]["ok"] is True
    omitido = por_id[esc["ids"]["db_a"]]
    assert omitido["ok"] is False
    assert omitido["error_code"] == "access.forbidden"
    assert omitido["database_name"] is None
    assert omitido["server_id"] is None
    assert omitido["batch_seq"] is None
    assert omitido["job_id"] is None
    # Un solo job persistido: el de la BD permitida.
    assert len([d for d in data["databases"] if d["job_id"] is not None]) == 1


def test_collation_batch_create_with_zero_permitted_is_a_403(
    admin_client, monkeypatch, collation_env
):
    esc = collation_env
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE managed_databases SET environment_id = :e"), {"e": esc["prod"]}
        )
    otorgar("environment", esc["prod"], "viewer")
    assert _forbidden(esc["tcb"]._plan(admin_client, esc["model_id"]))


def test_collation_batch_execute_and_cancel_are_403_if_any_job_is_forbidden(
    admin_client, collation_env
):
    """El plan se hizo ANTES del grant e incluye la BD de producción: el lote entero se niega."""
    esc = collation_env
    tcb = esc["tcb"]
    plan = tcb._plan(admin_client, esc["model_id"]).json()["data"]
    otorgar("environment", esc["prod"], "viewer")

    assert _forbidden(tcb._exec(admin_client, esc["model_id"], plan))
    assert tcb._FakeRunner.calls == []
    r = admin_client.post(
        f"{_COLLATION}/{esc['model_id']}/collation-conversions/{plan['batch_id']}/cancel"
    )
    assert _forbidden(r)


# --------------------------------------------------------------------------- #
# Lote de clonación                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def clone_env(admin_client, monkeypatch):
    """
    Servidor origen con ``db_a`` (dev) y ``db_b`` (prod) inventariadas; servidor destino con
    ``copy_a`` y ``copy_b`` (dev) y ``copy_p`` (prod).
    """
    prod, dev = env_id("production"), env_id("development")
    src, dst = _server(admin_client, 3306), _server(admin_client, 3307)
    _install(monkeypatch, source_server_id=src, target_server_id=dst)
    sembrar_bd(server_id=src, environment_id=dev, name="db_a")
    sembrar_bd(server_id=src, environment_id=prod, name="db_b")
    sembrar_bd(server_id=dst, environment_id=dev, name="copy_a")
    sembrar_bd(server_id=dst, environment_id=dev, name="copy_b")
    sembrar_bd(server_id=dst, environment_id=prod, name="copy_p")
    # El worker recibe ahora la lista de filas autorizadas: el fake la acepta.
    monkeypatch.setattr(
        clone_batch_runner,
        "enqueue",
        lambda batch_id, item_ids=None: cbc.CloneBatchController().run_batch(
            batch_id, item_ids
        ),
    )
    return {"client": admin_client, "src": src, "dst": dst, "prod": prod}


_FILA_A = {"source_database_name": "db_a", "target_database_name": "copy_a"}
_FILA_B = {"source_database_name": "db_b", "target_database_name": "copy_b"}


def _lote(esc, rows) -> dict:
    r = _plan(esc["client"], esc["src"], esc["dst"], rows)
    assert r.status_code == 201, r.text
    return r.json()["data"]


def _items(esc, batch_id) -> dict[str, dict]:
    r = esc["client"].get(f"{CLONE_BATCHES}/{batch_id}/items")
    assert r.status_code == 200, r.text
    return {i["source_database_name"]: i for i in r.json()["data"]}


def test_clone_batch_create_with_a_forbidden_row_is_a_403_and_persists_nothing(clone_env):
    esc = clone_env
    otorgar("environment", esc["prod"], "viewer")
    r = _plan(esc["client"], esc["src"], esc["dst"], [_FILA_A, _FILA_B])
    assert _forbidden(r), r.text
    assert esc["client"].get(CLONE_BATCHES).json()["data"] == []


def test_clone_batch_execute_skips_forbidden_rows_and_runs_the_rest(clone_env):
    esc = clone_env
    lote = _lote(esc, [_FILA_A, _FILA_B])
    items = _items(esc, lote["id"])
    otorgar("environment", esc["prod"], "viewer")

    r = _execute(esc["client"], lote, _server_name(esc["client"], esc["dst"]))
    assert r.status_code == 200, r.text
    data = r.json()["data"]

    assert data["skipped"] == [
        {"id": items["db_b"]["id"], "ok": False, "error_code": "access.forbidden"}
    ]
    assert "db_b" not in str(data["skipped"])
    # La fila permitida corrió; la prohibida quedó EXACTAMENTE como estaba (no se persistió).
    despues = _items(esc, lote["id"])
    assert despues["db_a"]["status"] != CLONE_BATCH_ITEM_PENDING
    assert despues["db_b"]["clone_job_id"] is None
    assert despues["db_b"]["status"] == CLONE_BATCH_ITEM_PENDING
    assert despues["db_b"]["error_code"] is None


def test_clone_batch_execute_with_zero_permitted_rows_is_a_403(clone_env):
    esc = clone_env
    lote = _lote(esc, [_FILA_B])
    otorgar("environment", esc["prod"], "viewer")
    r = _execute(esc["client"], lote, _server_name(esc["client"], esc["dst"]))
    assert _forbidden(r), r.text
    assert _items(esc, lote["id"])["db_b"]["clone_job_id"] is None


def test_clone_batch_retry_failed_skips_forbidden_rows(clone_env):
    esc = clone_env
    lote = _lote(esc, [_FILA_A, _FILA_B])
    items = _items(esc, lote["id"])
    otorgar("environment", esc["prod"], "viewer")

    r = esc["client"].post(f"{CLONE_BATCHES}/{lote['id']}/retry-failed")
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["id"] != lote["id"]
    assert data["total"] == 1
    assert data["skipped"] == [
        {"id": items["db_b"]["id"], "ok": False, "error_code": "access.forbidden"}
    ]
    assert list(_items(esc, data["id"])) == ["db_a"]


def test_clone_batch_retry_failed_with_zero_permitted_rows_is_a_403(clone_env):
    esc = clone_env
    lote = _lote(esc, [_FILA_B])
    otorgar("environment", esc["prod"], "viewer")
    r = esc["client"].post(f"{CLONE_BATCHES}/{lote['id']}/retry-failed")
    assert _forbidden(r), r.text


def test_clone_batch_cancel_is_an_action_on_the_target(clone_env):
    """Origen de desarrollo, destino de producción: cancelar se niega por el DESTINO."""
    esc = clone_env
    lote = _lote(esc, [{"source_database_name": "db_a", "target_database_name": "copy_p"}])
    otorgar("environment", esc["prod"], "viewer")
    r = esc["client"].post(f"{CLONE_BATCHES}/{lote['id']}/cancel")
    assert _forbidden(r), r.text


def test_clone_batch_cancel_passes_layer2_when_the_target_is_development(clone_env):
    esc = clone_env
    lote = _lote(esc, [_FILA_A])
    otorgar("environment", esc["prod"], "viewer")
    r = esc["client"].post(f"{CLONE_BATCHES}/{lote['id']}/cancel")
    assert not _forbidden(r), r.text


# --------------------------------------------------------------------------- #
# Perfil en bloque                                                             #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def bulk_env(admin_client, server_payload):
    prod, dev = env_id("production"), env_id("development")
    sid = admin_client.post(f"{_API}/servers", json=server_payload()).json()["data"]["id"]
    uid = admin_client.post(
        f"{_API}/server-users", json={"server_id": sid, "username": "grantee"}
    ).json()["data"]["id"]
    sembrar_bd(server_id=sid, environment_id=dev, name="bd_dev")
    sembrar_bd(server_id=sid, environment_id=prod, name="bd_prod")
    otorgar("environment", prod, "viewer")
    return f"{_API}/server-users/{uid}/apply-profile/999999/bulk"


def test_bulk_profile_with_a_forbidden_database_is_a_403(admin_client, bulk_env):
    """``databases`` lo nombra el cliente: una base de producción niega el lote entero."""
    r = admin_client.post(bulk_env, json={"databases": ["bd_dev", "bd_prod"]})
    assert _forbidden(r), r.text


def test_bulk_profile_with_only_permitted_databases_passes_layer2(admin_client, bulk_env):
    """Pasa la capa 2: lo que sigue (perfil inexistente) nunca es un 403 de acceso."""
    r = admin_client.post(bulk_env, json={"databases": ["bd_dev"]})
    assert not _forbidden(r), r.text


def test_bulk_profile_with_an_uninventoried_name_resolves_by_server_rule(
    admin_client, bulk_env
):
    """Sin fila de inventario rige la regla de servidor: el servidor tiene producción."""
    r = admin_client.post(bulk_env, json={"databases": ["desconocida"]})
    assert _forbidden(r), r.text
