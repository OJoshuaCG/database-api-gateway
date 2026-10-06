"""
La credencial de SOLO LECTURA del servidor y su sonda negativa (plan 12 §5.2).

Sin motor real: la sonda se ejercita con un adapter falso (lo que importa probar acá es qué hace
el gateway con el veredicto) y la clasificación de grants con las funciones puras de
``readonly_probe``, que es donde vive la regla.
"""

from datetime import datetime

import pytest

from app.core.database import Database
from app.services.db_admin.dtos import ConnectionInfo
from app.services.db_admin.readonly_probe import (
    mysql_grant_violations,
    postgres_role_violations,
)

SECRETO = "contraseña-de-solo-lectura-xyz"


def _servidor(admin_client, server_payload, **overrides) -> int:
    r = admin_client.post("/api/v1/servers", json=server_payload(**overrides))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _registrar(admin_client, sid, username="mcp_ro", password=SECRETO):
    return admin_client.put(
        f"/api/v1/servers/{sid}/readonly-credential",
        json={"username": username, "password": password},
    )


def _fila(sid):
    from app.models.server import Server

    s = Database().get_declarative_base_session()
    try:
        return s.get(Server, sid)
    finally:
        s.close()


class _AdapterFalso:
    def __init__(self, violaciones):
        self._violaciones = violaciones

    def test_connection(self):
        return ConnectionInfo(ok=True, dialect="mysql", server_version="8.0.36")

    def readonly_violations(self, *, allow_mysql_proc=False):
        return list(self._violaciones)


@pytest.fixture()
def sonda(monkeypatch):
    """``sonda(violaciones)``: reemplaza el adapter del controller por uno con ese veredicto."""
    import app.controllers.server_controller as ctrl

    capturado = {}

    def _instalar(violaciones):
        def _get_adapter(target):
            capturado["target"] = target
            return _AdapterFalso(violaciones)

        monkeypatch.setattr(ctrl, "get_adapter", _get_adapter)
        return capturado

    return _instalar


# --------------------------------------------------------------------------- #
# Alta, baja y lo que sale por la API                                          #
# --------------------------------------------------------------------------- #


def test_registering_the_credential_never_returns_it(admin_client, server_payload):
    sid = _servidor(admin_client, server_payload)
    r = _registrar(admin_client, sid)
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["has_readonly_credential"] is True
    assert data["readonly_verified_at"] is None
    for texto in (r.text, admin_client.get(f"/api/v1/servers/{sid}").text):
        assert SECRETO not in texto
        assert "mcp_ro" not in texto
        assert "readonly_password" not in texto


def test_the_password_is_stored_encrypted(admin_client, server_payload):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    fila = _fila(sid)
    assert fila.readonly_password_encrypted and fila.readonly_password_encrypted != SECRETO


def test_the_payload_is_closed(admin_client, server_payload):
    sid = _servidor(admin_client, server_payload)
    r = admin_client.put(
        f"/api/v1/servers/{sid}/readonly-credential",
        json={"username": "u", "password": "p", "host": "otro"},
    )
    assert r.status_code == 422


def test_clearing_is_idempotent(admin_client, server_payload):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    for _ in range(2):
        r = admin_client.delete(f"/api/v1/servers/{sid}/readonly-credential")
        assert r.status_code == 200, r.text
        assert r.json()["data"]["has_readonly_credential"] is False


# --------------------------------------------------------------------------- #
# La sonda negativa                                                            #
# --------------------------------------------------------------------------- #


def test_verifying_without_a_credential_is_a_409(admin_client, server_payload):
    sid = _servidor(admin_client, server_payload)
    r = admin_client.post(f"/api/v1/servers/{sid}/test-connection?credential=readonly")
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "server.readonly_credential_missing"


def test_a_passing_probe_sets_verified_at(admin_client, server_payload, sonda):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    capturado = sonda([])
    r = admin_client.post(f"/api/v1/servers/{sid}/test-connection?credential=readonly")
    assert r.status_code == 200, r.text
    assert r.json()["data"]["readonly_verified_at"] is not None
    assert _fila(sid).readonly_verified_at is not None
    # La sonda corre con la credencial de SOLO LECTURA, nunca con la pseudo-root.
    assert capturado["target"].admin_user == "mcp_ro"
    assert capturado["target"].admin_password == SECRETO


def test_a_failing_probe_clears_verified_at_and_lists_the_reasons(
    admin_client, server_payload, sonda
):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    sonda([])
    admin_client.post(f"/api/v1/servers/{sid}/test-connection?credential=readonly")
    assert _fila(sid).readonly_verified_at is not None

    sonda(["privilege:insert"])
    r = admin_client.post(f"/api/v1/servers/{sid}/test-connection?credential=readonly")
    assert r.status_code == 422
    ctx = r.json()["detail"]["public_context"]
    assert ctx["code"] == "server.readonly_probe_failed"
    assert ctx["violations"] == ["privilege:insert"]
    assert _fila(sid).readonly_verified_at is None


def test_the_probe_does_not_change_the_server_status(admin_client, server_payload, sonda):
    sid = _servidor(admin_client, server_payload)
    antes = admin_client.get(f"/api/v1/servers/{sid}").json()["data"]["status"]
    _registrar(admin_client, sid)
    sonda([])
    admin_client.post(f"/api/v1/servers/{sid}/test-connection?credential=readonly")
    assert admin_client.get(f"/api/v1/servers/{sid}").json()["data"]["status"] == antes


def test_replacing_the_credential_clears_the_verification(admin_client, server_payload, sonda):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    sonda([])
    admin_client.post(f"/api/v1/servers/{sid}/test-connection?credential=readonly")
    assert _fila(sid).readonly_verified_at is not None
    _registrar(admin_client, sid, username="mcp_ro2")
    assert _fila(sid).readonly_verified_at is None


def test_verifying_requires_step_up(admin_client, server_payload, sonda, expire_step_up):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    sonda([])
    expire_step_up(admin_client)
    r = admin_client.post(f"/api/v1/servers/{sid}/test-connection?credential=readonly")
    assert r.status_code == 403
    assert r.json()["detail"]["public_context"]["code"] == "access.step_up_required"
    assert _fila(sid).readonly_verified_at is None


def test_the_root_probe_does_not_require_step_up(admin_client, server_payload, expire_step_up):
    """La prueba con la pseudo-root sigue siendo una lectura: no cambia su contrato."""
    sid = _servidor(admin_client, server_payload, port=3399)
    expire_step_up(admin_client)
    r = admin_client.post(f"/api/v1/servers/{sid}/test-connection")
    assert r.status_code == 502


# --------------------------------------------------------------------------- #
# Re-apuntar el servidor descarta la credencial                                #
# --------------------------------------------------------------------------- #


def test_rebinding_the_server_discards_the_readonly_credential(admin_client, server_payload, sonda):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    sonda([])
    admin_client.post(f"/api/v1/servers/{sid}/test-connection?credential=readonly")

    r = admin_client.patch(
        f"/api/v1/servers/{sid}", json={"port": 3400, "root_password": "supersecret"}
    )
    assert r.status_code == 200, r.text
    assert r.json()["data"]["has_readonly_credential"] is False
    fila = _fila(sid)
    assert fila.readonly_username is None
    assert fila.readonly_password_encrypted is None
    assert fila.readonly_verified_at is None


def test_a_non_rebinding_edit_keeps_the_credential(admin_client, server_payload):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    r = admin_client.patch(f"/api/v1/servers/{sid}", json={"notes": "solo notas"})
    assert r.json()["data"]["has_readonly_credential"] is True


# --------------------------------------------------------------------------- #
# La regla de clasificación (pura)                                             #
# --------------------------------------------------------------------------- #


def test_mysql_minimal_grants_pass():
    lineas = [
        "GRANT USAGE ON *.* TO `mcp_ro`@`10.0.0.%`",
        "GRANT SHOW_ROUTINE ON *.* TO `mcp_ro`@`10.0.0.%`",
        "GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON `la_base`.* TO `mcp_ro`@`10.0.0.%`",
        "GRANT SELECT ON `mysql`.`proc` TO `mcp_ro`@`%`",
    ]
    # ``SELECT ON mysql.proc`` ya no es gratis: solo pasa con la bandera ``readonly_proc_grant``.
    assert mysql_grant_violations(lineas, allow_mysql_proc=True) == []
    assert mysql_grant_violations(lineas) == ["select_on_mysql_schema"]


@pytest.mark.parametrize(
    "linea, motivo",
    [
        ("GRANT INSERT ON `la_base`.* TO `u`@`%`", "privilege:insert"),
        ("GRANT ALL PRIVILEGES ON `la_base`.* TO `u`@`%`", "all_privileges"),
        ("GRANT SELECT ON *.* TO `u`@`%`", "global_privilege:select"),
        ("GRANT SELECT ON `mysql`.* TO `u`@`%`", "select_on_mysql_schema"),
        ("GRANT SELECT ON `la_base`.* TO `u`@`%` WITH GRANT OPTION", "grant_option"),
        ("GRANT `rol_app`@`%` TO `u`@`%`", "unrecognized_grant"),
        ("GRANT PROCESS ON *.* TO `u`@`%`", "privilege:process"),
        ("GRANT EXECUTE ON `la_base`.* TO `u`@`%`", "privilege:execute"),
    ],
)
def test_mysql_grants_that_can_write_or_disclose_fail(linea, motivo):
    assert motivo in mysql_grant_violations([linea])


def test_mysql_column_level_select_is_still_select():
    assert mysql_grant_violations(["GRANT SELECT (a, b) ON `db`.`t` TO `u`@`%`"]) == []


_PG_OK = {
    "rolsuper": False,
    "rolcreatedb": False,
    "rolcreaterole": False,
    "rolreplication": False,
    "rolbypassrls": False,
    "default_transaction_read_only": "on",
    "can_create_in_database": False,
    "can_create_in_public": False,
    "write_roles": [],
    "table_write_privileges": 0,
    "temp_write_succeeded": False,
}


def test_postgres_minimal_role_passes():
    assert postgres_role_violations(dict(_PG_OK)) == []


@pytest.mark.parametrize(
    "cambio, motivo",
    [
        ({"rolsuper": True}, "role_attribute:rolsuper"),
        ({"default_transaction_read_only": "off"}, "default_transaction_read_only_off"),
        ({"can_create_in_public": True}, "create_on_schema_public"),
        ({"write_roles": ["pg_write_all_data"]}, "member_of:pg_write_all_data"),
        ({"table_write_privileges": 3}, "table_write_privileges"),
        ({"temp_write_succeeded": True}, "write_attempt_succeeded"),
    ],
)
def test_postgres_roles_that_can_write_fail(cambio, motivo):
    assert motivo in postgres_role_violations({**_PG_OK, **cambio})


def test_a_missing_postgres_fact_counts_as_a_violation():
    """Si el adapter no pudo leer un hecho, la sonda no lo asume favorable."""
    hechos = dict(_PG_OK)
    del hechos["rolsuper"]
    assert "role_attribute:rolsuper" in postgres_role_violations(hechos)


def test_the_verification_column_is_a_naive_utc_datetime(admin_client, server_payload, sonda):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    sonda([])
    admin_client.post(f"/api/v1/servers/{sid}/test-connection?credential=readonly")
    valor = _fila(sid).readonly_verified_at
    assert isinstance(valor, datetime) and valor.tzinfo is None
