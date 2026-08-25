"""
``POST /managed-databases/{id}/provision`` — re-aprovisionamiento de una fila ya registrada.

Cubre los guards de estado, el chequeo físico contra el motor (adapter mockeado), la
convergencia por carrera (1007/42P04), la preservación de las notas del operador y la
auditoría fail-closed.
"""

from app.exceptions import AppHttpException
from app.services import provisioning_catalog as pcodes


def _server(admin_client, port, **ov) -> int:
    payload = {
        "name": f"srv{port}",
        "host": "10.0.0.9",
        "port": port,
        "engine": "postgresql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    payload.update(ov)
    return admin_client.post("/api/v1/servers", json=payload).json()["data"]["id"]


def _owner(admin_client, server_id, username="owner1") -> int:
    return admin_client.post(
        "/api/v1/server-users", json={"server_id": server_id, "username": username}
    ).json()["data"]["id"]


def _register(admin_client, sid, oid, name, **extra) -> dict:
    """Alta SIN aprovisionar: la fila queda en ``pending`` (el caso del incidente)."""
    body = {"server_id": sid, "owner_id": oid, "name": name}
    body.update(extra)
    r = admin_client.post("/api/v1/managed-databases", json=body)
    assert r.status_code == 201, r.text
    return r.json()["data"]


class _Adapter:
    """Adapter falso: ``live`` es la lista de BDs que "existen" en el motor."""

    def __init__(self, live=(), on_create=None):
        self.live = list(live)
        self.calls = []
        self._on_create = on_create

    def list_databases(self):
        return list(self.live)

    def create_database(self, name, charset=None, collation=None, owner=None):
        self.calls.append(("create_database", name, charset, collation, owner))
        if self._on_create is not None:
            raise self._on_create
        self.live.append(name)


def _patch(monkeypatch, adapter):
    import app.controllers.managed_database_controller as mdc

    monkeypatch.setattr(mdc, "get_adapter", lambda target: adapter)
    return adapter


# --------------------------------------------------------------------------- #
# Camino feliz                                                                 #
# --------------------------------------------------------------------------- #
def test_requires_auth(client):
    assert client.post("/api/v1/managed-databases/1/provision").status_code == 401


def test_pending_absent_creates_and_activates(admin_client, monkeypatch):
    adapter = _patch(monkeypatch, _Adapter(live=["otra_db"]))
    sid = _server(admin_client, 5560)
    oid = _owner(admin_client, sid, "pown")
    row = _register(admin_client, sid, oid, "prov_pending")
    assert row["status"] == "pending"

    r = admin_client.post(f"/api/v1/managed-databases/{row['id']}/provision")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["provisioned"] is True
    assert data["previous_status"] == "pending"
    assert data["database"]["status"] == "active"
    # Exactamente UN CREATE DATABASE, y sin ningún grant (política: cero privilegios).
    assert [c[0] for c in adapter.calls] == ["create_database"]
    assert adapter.calls[0][1] == "prov_pending"
    assert adapter.calls[0][4] == "pown"


def test_error_absent_is_retried(admin_client, monkeypatch):
    """Una fila en ``error`` por un CREATE fallido SÍ se puede reintentar."""
    _patch(monkeypatch, _Adapter(on_create=AppHttpException("caido", 502)))
    sid = _server(admin_client, 5561)
    oid = _owner(admin_client, sid, "eown")
    r = admin_client.post(
        "/api/v1/managed-databases?provision=true",
        json={"server_id": sid, "owner_id": oid, "name": "retry_db"},
    )
    assert r.status_code == 502
    db_id = admin_client.get(
        f"/api/v1/managed-databases?server_id={sid}"
    ).json()["data"][0]["id"]

    # Ahora el motor responde bien.
    _patch(monkeypatch, _Adapter(live=[]))
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/provision")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["previous_status"] == "error"
    assert r.json()["data"]["database"]["status"] == "active"


# --------------------------------------------------------------------------- #
# Guards                                                                       #
# --------------------------------------------------------------------------- #
def test_already_exists_in_engine_conflicts_without_ddl(admin_client, monkeypatch):
    adapter = _patch(monkeypatch, _Adapter(live=["ya_existe"]))
    sid = _server(admin_client, 5562)
    oid = _owner(admin_client, sid, "xown")
    row = _register(admin_client, sid, oid, "ya_existe")

    r = admin_client.post(f"/api/v1/managed-databases/{row['id']}/provision")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == pcodes.CODE_EXISTS_IN_ENGINE
    # Lo esencial: NO se emitió DDL sobre una base preexistente.
    assert adapter.calls == []


def test_quarantined_error_with_existing_db_points_to_reconcile(admin_client, monkeypatch):
    """``error`` + BD presente = cuarentena de migraciones, no un CREATE pendiente."""
    adapter = _patch(monkeypatch, _Adapter(on_create=AppHttpException("caido", 502)))
    sid = _server(admin_client, 5563)
    oid = _owner(admin_client, sid, "qown")
    admin_client.post(
        "/api/v1/managed-databases?provision=true",
        json={"server_id": sid, "owner_id": oid, "name": "quar_db"},
    )
    db_id = admin_client.get(
        f"/api/v1/managed-databases?server_id={sid}"
    ).json()["data"][0]["id"]

    adapter = _patch(monkeypatch, _Adapter(live=["quar_db"]))
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/provision")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == pcodes.CODE_QUARANTINED_NOT_MISSING
    assert adapter.calls == []


def test_active_conflicts_and_allow_recreate_proceeds(admin_client, monkeypatch):
    adapter = _patch(monkeypatch, _Adapter(live=[]))
    sid = _server(admin_client, 5564)
    oid = _owner(admin_client, sid, "aown")
    r = admin_client.post(
        "/api/v1/managed-databases?provision=true",
        json={"server_id": sid, "owner_id": oid, "name": "act_db"},
    )
    db_id = r.json()["data"]["id"]
    assert r.json()["data"]["status"] == "active"

    # La BD "desapareció" del motor (borrada por fuera): el adapter ya no la lista.
    adapter = _patch(monkeypatch, _Adapter(live=[]))
    r = admin_client.post(f"/api/v1/managed-databases/{db_id}/provision")
    assert r.status_code == 409, r.text
    assert r.json()["detail"]["public_context"]["code"] == pcodes.CODE_ALREADY_ACTIVE
    assert adapter.calls == []

    r = admin_client.post(
        f"/api/v1/managed-databases/{db_id}/provision?allow_recreate=true"
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["provisioned"] is True
    assert [c[0] for c in adapter.calls] == ["create_database"]


def test_reserved_database_name_blocked_without_ddl(admin_client, monkeypatch):
    """Una fila vieja con nombre de sistema no puede emitir DDL (guard que el alta no hace)."""
    import app.controllers.managed_database_controller as mdc
    from app.core.database import Database
    from app.models.managed_database import ManagedDatabase

    adapter = _patch(monkeypatch, _Adapter(live=[]))
    sid = _server(admin_client, 5565, engine="mysql")
    oid = _owner(admin_client, sid, "rown")
    row = _register(admin_client, sid, oid, "inocente")

    # Se renombra por debajo del schema, que es lo único que hoy filtra el nombre.
    session = Database().get_declarative_base_session()
    try:
        session.get(ManagedDatabase, row["id"]).name = "mysql"
        session.commit()
    finally:
        session.close()

    r = admin_client.post(f"/api/v1/managed-databases/{row['id']}/provision")
    assert r.status_code == 409, r.text
    assert adapter.calls == []
    assert mdc is not None  # el import documenta de dónde sale el monkeypatch


# --------------------------------------------------------------------------- #
# Fallo, notas y carrera                                                       #
# --------------------------------------------------------------------------- #
def test_failure_marks_error_and_keeps_operator_notes(admin_client, monkeypatch):
    _patch(monkeypatch, _Adapter(on_create=AppHttpException("motor inaccesible", 502)))
    sid = _server(admin_client, 5566)
    oid = _owner(admin_client, sid, "nown")
    row = _register(
        admin_client, sid, oid, "notes_db", notes="BD del cliente ACME, no borrar"
    )

    r = admin_client.post(f"/api/v1/managed-databases/{row['id']}/provision")
    assert r.status_code == 502, r.text

    after = admin_client.get(f"/api/v1/managed-databases/{row['id']}").json()["data"]
    assert after["status"] == "error"
    # La nota del operador SOBREVIVE; el diagnóstico se agrega marcado.
    assert "BD del cliente ACME, no borrar" in after["notes"]
    assert "[gateway]" in after["notes"]


def test_race_converges_without_reporting_conflict(admin_client, monkeypatch):
    """1007/42P04 tras el chequeo previo es ÉXITO por carrera, no error."""
    duplicate = AppHttpException(
        "El recurso ya existe o tiene dependencias en el servidor destino.",
        409,
        context={"remote_error_code": "42P04"},
    )
    _patch(monkeypatch, _Adapter(live=[], on_create=duplicate))
    sid = _server(admin_client, 5567)
    oid = _owner(admin_client, sid, "cown")
    row = _register(admin_client, sid, oid, "race_db")

    r = admin_client.post(f"/api/v1/managed-databases/{row['id']}/provision")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["provisioned"] is False
    assert r.json()["data"]["database"]["status"] == "active"


def test_other_409_from_engine_is_not_swallowed(admin_client, monkeypatch):
    """Un 409 que NO es 'la base ya existe' (p. ej. 2BP01) sigue siendo un error."""
    other = AppHttpException(
        "El recurso ya existe o tiene dependencias en el servidor destino.",
        409,
        context={"remote_error_code": "2BP01"},
    )
    _patch(monkeypatch, _Adapter(live=[], on_create=other))
    sid = _server(admin_client, 5568)
    oid = _owner(admin_client, sid, "oown")
    row = _register(admin_client, sid, oid, "other_db")

    r = admin_client.post(f"/api/v1/managed-databases/{row['id']}/provision")
    assert r.status_code == 409
    after = admin_client.get(f"/api/v1/managed-databases/{row['id']}").json()["data"]
    assert after["status"] == "error"


def test_audits_intent_before_ddl(admin_client, monkeypatch):
    from app.core.database import Database
    from app.models.audit_log import AuditLog

    _patch(monkeypatch, _Adapter(live=[]))
    sid = _server(admin_client, 5569)
    oid = _owner(admin_client, sid, "audown")
    row = _register(admin_client, sid, oid, "audit_db")
    admin_client.post(f"/api/v1/managed-databases/{row['id']}/provision")

    session = Database().get_declarative_base_session()
    try:
        rows = (
            session.query(AuditLog)
            .filter(AuditLog.action == "managed_database.provision")
            .order_by(AuditLog.id.asc())
            .all()
        )
        statuses = [r.status for r in rows]
    finally:
        session.close()
    # El intento se registra ANTES (fail-closed) y el resultado después.
    assert "attempt" in statuses
    assert statuses.index("attempt") < len(statuses) - 1
    assert all(r.touched_engine for r in rows) if rows else False


def test_not_found(admin_client):
    assert admin_client.post("/api/v1/managed-databases/99999/provision").status_code == 404
