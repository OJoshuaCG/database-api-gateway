"""
Apply-profile MASIVO: mismo perfil + mismo usuario sobre N bases de datos.

``POST /server-users/{user_id}/apply-profile/{profile_id}/bulk``

Cubre sobre SQLite (sin motor real, adapter monkeypatcheado igual que
test_grant_guards.py): el fan-out por BD, que una BD que falla NO aborta el lote, que el
``database`` de la plantilla ``object_mappings`` se IGNORA (se sobreescribe con el de la
iteración), las validaciones de borde del schema (lista vacía, tope de 100) y que la
auditoría queda AGREGADA en una sola fila.
"""

import pytest

from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog
from app.services.db_admin.mysql_adapter import MySQLAdapter

# 'database' puesto a propósito a un valor que NUNCA está en la lista de BDs: el
# controller debe reemplazarlo, así que si aparece en una llamada al adapter, hay bug.
_TEMPLATE_DB = "PLANTILLA_IGNORADA"

_MAPPINGS = [
    {"level": "database", "object_ref": {"database": _TEMPLATE_DB}},
    {"level": "table", "object_ref": {"database": _TEMPLATE_DB, "table": "pedidos"}},
]


def _make_server(admin_client, **ov) -> int:
    payload = {
        "name": "srv-bulk",
        "host": "127.0.0.1",
        "port": 3306,
        "engine": "mysql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    payload.update(ov)
    r = admin_client.post("/api/v1/servers", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _make_user(admin_client, server_id: int, username: str = "alice") -> int:
    r = admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": username}
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _make_profile(admin_client, name: str = "rw", engine: str = "mysql", items=None) -> int:
    r = admin_client.post(
        "/api/v1/permission-profiles",
        json={
            "name": name,
            "engine": engine,
            "items": items
            or [
                {"level": "database", "privileges": ["SELECT"]},
                {"level": "table", "privileges": ["SELECT", "INSERT"]},
            ],
        },
    )
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


@pytest.fixture()
def ctx(admin_client):
    """Servidor + usuario + perfil de 2 niveles (database, table) sobre MySQL."""
    sid = _make_server(admin_client)
    return {
        "client": admin_client,
        "server_id": sid,
        "user_id": _make_user(admin_client, sid),
        "profile_id": _make_profile(admin_client),
    }


def _url(ctx, *, user_id: int | None = None, profile_id: int | None = None) -> str:
    uid = ctx["user_id"] if user_id is None else user_id
    pid = ctx["profile_id"] if profile_id is None else profile_id
    return f"/api/v1/server-users/{uid}/apply-profile/{pid}/bulk"


def _capture(monkeypatch, *, fail_dbs: set[str] | None = None) -> list[tuple]:
    """
    Monkeypatchea el adapter y devuelve la lista de llamadas registradas como
    ``(op, level, database, table)``. ``fail_dbs`` hace fallar el GRANT en esas BDs
    (simula el rechazo del motor, p.ej. una BD que no existe).
    """
    calls: list[tuple] = []
    fail_dbs = fail_dbs or set()

    def fake_can_grant(self, level, ref, privileges):
        calls.append(("can_grant", level.value, ref.database, ref.table))
        return True

    def fake_grant(self, grantee, level, ref, privileges, **kw):
        calls.append(("grant", level.value, ref.database, ref.table))
        if ref.database in fail_dbs:
            raise AppHttpException(message="Base de datos desconocida.", status_code=404)

    monkeypatch.setattr(MySQLAdapter, "can_grant", fake_can_grant)
    monkeypatch.setattr(MySQLAdapter, "grant_object", fake_grant)
    return calls


def _audit_rows(action: str) -> list[AuditLog]:
    session = Database().get_declarative_base_session()
    try:
        return session.query(AuditLog).filter(AuditLog.action == action).all()
    finally:
        session.close()


# ----------------------------- camino feliz -------------------------------- #
def test_bulk_applies_profile_to_three_databases(ctx, monkeypatch):
    calls = _capture(monkeypatch)
    r = ctx["client"].post(
        _url(ctx), json={"databases": ["t1", "t2", "t3"], "object_mappings": _MAPPINGS}
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["total_databases"] == 3
    assert data["profile_name"] == "rw"
    assert data["engine"] == "mysql"
    # Orden de la respuesta = orden del request (el frontend mapea 1:1).
    assert [i["database"] for i in data["results"]] == ["t1", "t2", "t3"]
    assert all(i["ok"] for i in data["results"])
    # 2 niveles del perfil × 3 BDs.
    assert [i["grants_applied"] for i in data["results"]] == [2, 2, 2]
    assert all(not i["errors"] and not i["skipped_levels"] for i in data["results"])
    assert len([c for c in calls if c[0] == "grant"]) == 6


def test_bulk_overrides_template_database_per_iteration(ctx, monkeypatch):
    """El 'database' de object_mappings se IGNORA; el resto del ref se reusa tal cual."""
    calls = _capture(monkeypatch)
    r = ctx["client"].post(
        _url(ctx), json={"databases": ["t1", "t2"], "object_mappings": _MAPPINGS}
    )
    assert r.status_code == 200, r.text

    # Nunca llegó al adapter el nombre de la plantilla (ni en can_grant ni en grant).
    assert all(c[2] != _TEMPLATE_DB for c in calls)
    grants = [c for c in calls if c[0] == "grant"]
    assert sorted((c[2], c[1]) for c in grants) == [
        ("t1", "database"), ("t1", "table"), ("t2", "database"), ("t2", "table"),
    ]
    # El resto de la plantilla (table) NO se toca entre iteraciones.
    assert all(c[3] == "pedidos" for c in grants if c[1] == "table")
    # El pre-chequeo can_grant también recibe la BD de la iteración, no la plantilla.
    assert {c[2] for c in calls if c[0] == "can_grant"} == {"t1", "t2"}


# --------------------- una BD falla, el lote continúa ---------------------- #
def test_bulk_one_database_fails_others_still_applied(ctx, monkeypatch):
    calls = _capture(monkeypatch, fail_dbs={"t2"})
    r = ctx["client"].post(
        _url(ctx), json={"databases": ["t1", "t2", "t3"], "object_mappings": _MAPPINGS}
    )
    assert r.status_code == 200, r.text  # el lote NUNCA falla por un ítem
    res = {i["database"]: i for i in r.json()["data"]["results"]}

    assert res["t2"]["ok"] is False and res["t2"]["errors"]
    assert res["t1"]["ok"] is True and res["t3"]["ok"] is True
    assert res["t1"]["grants_applied"] == 2
    assert res["t3"]["grants_applied"] == 2
    # Best-effort también DENTRO de la BD que falla: se intentaron sus dos niveles.
    assert res["t2"]["grants_applied"] == 0
    assert len(res["t2"]["errors"]) == 2
    assert len([c for c in calls if c[0] == "grant" and c[2] == "t2"]) == 2
    # t3 va DESPUÉS de la que falla: se procesó igual (el lote no se cortó).
    assert {c[2] for c in calls} == {"t1", "t2", "t3"}


def test_bulk_level_without_mapping_is_skipped(ctx, monkeypatch):
    """Un nivel del perfil sin object_mapping se omite, no es error."""
    _capture(monkeypatch)
    r = ctx["client"].post(
        _url(ctx), json={"databases": ["t1"], "object_mappings": [_MAPPINGS[0]]}
    )
    assert r.status_code == 200, r.text
    item = r.json()["data"]["results"][0]
    assert item["skipped_levels"] == ["table"]
    assert item["grants_applied"] == 1
    assert item["ok"] is True  # omitir no es fallar


# ---------------------- validación de borde del schema -------------------- #
def test_bulk_empty_database_list_422(ctx, monkeypatch):
    _capture(monkeypatch)
    r = ctx["client"].post(_url(ctx), json={"databases": [], "object_mappings": _MAPPINGS})
    assert r.status_code == 422, r.text


def test_bulk_cap_of_100_databases(ctx, monkeypatch):
    _capture(monkeypatch)
    names = [f"d{i}" for i in range(100)]
    ok = ctx["client"].post(
        _url(ctx), json={"databases": names, "object_mappings": _MAPPINGS}
    )
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["total_databases"] == 100

    too_many = ctx["client"].post(
        _url(ctx), json={"databases": names + ["d100"], "object_mappings": _MAPPINGS}
    )
    assert too_many.status_code == 422, too_many.text


@pytest.mark.parametrize("bad", ["", "x" * 65])
def test_bulk_invalid_database_name_422(ctx, monkeypatch, bad):
    _capture(monkeypatch)
    r = ctx["client"].post(
        _url(ctx), json={"databases": [bad], "object_mappings": _MAPPINGS}
    )
    assert r.status_code == 422, r.text


def test_bulk_missing_databases_field_422(ctx, monkeypatch):
    _capture(monkeypatch)
    r = ctx["client"].post(_url(ctx), json={"object_mappings": _MAPPINGS})
    assert r.status_code == 422, r.text


# ------------- mismas precondiciones que el endpoint single ---------------- #
def test_bulk_unknown_user_404(ctx, monkeypatch):
    _capture(monkeypatch)
    r = ctx["client"].post(
        _url(ctx, user_id=999999), json={"databases": ["t1"], "object_mappings": _MAPPINGS}
    )
    assert r.status_code == 404, r.text


def test_bulk_unknown_profile_404(ctx, monkeypatch):
    _capture(monkeypatch)
    r = ctx["client"].post(
        _url(ctx, profile_id=999999),
        json={"databases": ["t1"], "object_mappings": _MAPPINGS},
    )
    assert r.status_code == 404, r.text


def test_bulk_engine_mismatch_422(ctx, monkeypatch):
    """Perfil de PostgreSQL contra servidor MySQL: 422 antes de tocar el motor."""
    calls = _capture(monkeypatch)
    pg_profile = _make_profile(
        ctx["client"], name="pg-ro", engine="postgresql",
        items=[{"level": "table", "privileges": ["SELECT"]}],
    )
    r = ctx["client"].post(
        _url(ctx, profile_id=pg_profile),
        json={"databases": ["t1"], "object_mappings": _MAPPINGS},
    )
    assert r.status_code == 422, r.text
    assert calls == []  # ni un GRANT contra el motor


def test_bulk_requires_auth(ctx):
    # ``ctx`` ya construyó el inventario autenticado; se cae la sesión y se reintenta.
    ctx["client"].cookies.clear()
    r = ctx["client"].post(
        _url(ctx), json={"databases": ["t1"], "object_mappings": _MAPPINGS}
    )
    assert r.status_code in (401, 403)


# ------------------------------ auditoría --------------------------------- #
def test_bulk_records_single_aggregated_audit_row(ctx, monkeypatch):
    _capture(monkeypatch, fail_dbs={"t2"})
    r = ctx["client"].post(
        _url(ctx), json={"databases": ["t1", "t2"], "object_mappings": _MAPPINGS}
    )
    assert r.status_code == 200, r.text

    rows = _audit_rows("server_user.apply_profile_bulk")
    assert len(rows) == 1, "la auditoría es AGREGADA: una fila por llamada, no por BD"
    row = rows[0]
    assert row.status == "success"
    assert row.touched_engine is True
    assert row.grantee == "alice@%"
    assert row.grantor == "root"
    assert f"profile_id={ctx['profile_id']}" in row.detail
    # El rastro nombra las BDs tocadas y las fallidas (auditabilidad del fan-out DCL).
    assert "t1,t2" in row.detail
    assert "fallidas=t2" in row.detail
    # No contamina la acción del endpoint single.
    assert _audit_rows("server_user.apply_profile") == []


def test_single_apply_profile_unchanged_after_refactor(ctx, monkeypatch):
    """El endpoint single conserva su contrato exacto (cambio aditivo, no reescritura)."""
    calls = _capture(monkeypatch)
    r = ctx["client"].post(
        f"/api/v1/server-users/{ctx['user_id']}/apply-profile/{ctx['profile_id']}",
        json={
            "object_mappings": [
                {"level": "database", "object_ref": {"database": "solo_una"}},
                {"level": "table", "object_ref": {"database": "solo_una", "table": "t"}},
            ]
        },
    )
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert set(data) == {
        "profile_id", "profile_name", "engine",
        "grants_applied", "skipped_levels", "errors",
    }
    assert data["grants_applied"] == 2
    assert data["skipped_levels"] == [] and data["errors"] == []
    assert {c[2] for c in calls} == {"solo_una"}
    assert len(_audit_rows("server_user.apply_profile")) == 1
    assert _audit_rows("server_user.apply_profile_bulk") == []
