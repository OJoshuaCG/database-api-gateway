"""
S6 (mcp-schema-definitions): ``PUT /servers/{id}/readonly-credential/routine-bodies``.

Enciende o apaga ``SELECT ON mysql.proc`` para la credencial de solo lectura (MariaDB < 11.3 /
MySQL 5.7). Es SERVER-WIDE: expone el código de las rutinas de TODAS las bases del servidor.

Sin motor: el adapter es un fake que modela el único hecho que importa acá, "¿la cuenta tiene
``SELECT ON mysql.proc``?", y que ``provision_readonly_account`` lo re-converge (``REVOKE ALL`` y
re-grant). Lo que se prueba es lo que decide el gateway: acknowledgement, motor soportado,
re-convergencia, orden fail-safe, credencial manual, auditoría y el cableado de ``flag_off``.

Cubre S6.3 a S6.6. NO prueba un servidor MariaDB/MySQL real: ver "no verificado" del reporte.

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_readonly_proc_grant``
"""

# ruff: noqa: F811 — las fixtures importadas se piden como parámetro, que es como pytest las usa.
import pytest

from app.core.crypto import decrypt
from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog
from app.models.server import Server
from app.services.db_admin.dtos import ConnectionInfo
from app.services.db_admin.readonly_probe import ReadonlyPreflight, proc_grant_supported
from app.services.server_catalog import (
    CODE_READONLY_PROC_GRANT_ACK_MISMATCH,
    CODE_READONLY_PROC_GRANT_ENGINE_UNSUPPORTED,
    CODE_READONLY_PROVISION_IN_PROGRESS,
    ERROR_CODES,
    READONLY_PROC_ACK_TEXT,
)

URL = "/api/v1/servers/{sid}/readonly-credential/routine-bodies"
_ACTION = "server.readonly_proc_grant.set"


# --------------------------------------------------------------------------- #
# Arnés                                                                        #
# --------------------------------------------------------------------------- #
class _MotorFalso:
    """Un servidor: versión, y si la cuenta de solo lectura tiene ``SELECT ON mysql.proc``."""

    def __init__(self):
        self.version = "5.7.44"
        self.dialect = "mysql"
        self.existe = False
        self.proc_otorgado = False  # el estado REAL del motor
        self.falla_al_provisionar = False
        self.provisiones = []  # (proc_grant pedido, proc_grant_supported) por corrida
        self.sondas = []  # allow_mysql_proc con que se sondeó
        self.conexiones_root = 0

    def adapter(self, target):
        return _AdapterFalso(self)


class _AdapterFalso:
    def __init__(self, motor: _MotorFalso):
        self._m = motor

    def list_databases(self):
        return ["app_prod"]

    def is_privileged_role(self, username):
        return False

    def preflight_readonly_account(self, username, host):
        return ReadonlyPreflight(
            exists=self._m.existe,
            proc_grant_supported=proc_grant_supported(self._m.version, self._m.dialect),
        )

    def provision_readonly_account(self, username, password, host, databases, preflight):
        if self._m.falla_al_provisionar:
            raise AppHttpException(message="El motor rechazó la operación.", status_code=502)
        self._m.provisiones.append((preflight.proc_grant, preflight.proc_grant_supported))
        # REVOKE ALL y re-grant: el motor queda con mysql.proc SOLO si se pidió y se soporta.
        self._m.proc_otorgado = bool(preflight.proc_grant and preflight.proc_grant_supported)
        self._m.existe = True

    def test_connection(self):
        self._m.conexiones_root += 1
        return ConnectionInfo(ok=True, dialect=self._m.dialect, server_version=self._m.version)

    def readonly_violations(self, *, allow_mysql_proc=False):
        self._m.sondas.append(allow_mysql_proc)
        if self._m.proc_otorgado and not allow_mysql_proc:
            return ["select_on_mysql_schema"]
        return []


@pytest.fixture()
def motor(monkeypatch):
    import app.controllers.server_controller as ctrl

    m = _MotorFalso()
    monkeypatch.setattr(ctrl, "get_adapter", m.adapter)
    monkeypatch.setattr(ctrl, "DB_NAME", "datum_meta")
    monkeypatch.setattr(ctrl, "DB_HOST", "127.0.0.1")
    monkeypatch.setattr(ctrl, "DB_PORT", 3399)
    return m


def _servidor(admin_client, server_payload, **overrides) -> int:
    r = admin_client.post("/api/v1/servers", json=server_payload(**overrides))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _fila(sid):
    session = Database().get_declarative_base_session()
    try:
        return session.get(Server, sid)
    finally:
        session.close()


def _bandera(sid) -> bool:
    return bool(_fila(sid).readonly_proc_grant)


def _registrar(admin_client, sid, username="otra_cuenta", password="registrada-a-mano-xyz"):
    """PUT de una credencial a mano. ``otra_cuenta`` != ``MCP_READONLY_ACCOUNT_USERNAME``."""
    r = admin_client.put(
        f"/api/v1/servers/{sid}/readonly-credential",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, r.text


def _marcar_verificada(sid):
    from datetime import datetime

    session = Database().get_declarative_base_session()
    try:
        session.get(Server, sid).readonly_verified_at = datetime(2026, 1, 1)
        session.commit()
    finally:
        session.close()


def _auditoria() -> list[tuple[str, str, str | None]]:
    session = Database().get_declarative_base_session()
    try:
        return [
            (a.action, a.status, a.detail)
            for a in session.query(AuditLog).filter(AuditLog.action == _ACTION).all()
        ]
    finally:
        session.close()


def _habilitar(admin_client, sid, ack=READONLY_PROC_ACK_TEXT):
    return admin_client.put(
        URL.format(sid=sid), json={"enabled": True, "acknowledgement": ack}
    )


def _deshabilitar(admin_client, sid):
    return admin_client.put(URL.format(sid=sid), json={"enabled": False})


def _aprovisionar(admin_client, sid):
    r = admin_client.post(f"/api/v1/servers/{sid}/readonly-credential/provision")
    assert r.status_code == 200, r.text


def _codigo(respuesta) -> str:
    return respuesta.json()["detail"]["public_context"]["code"]


# --------------------------------------------------------------------------- #
# Vocabulario cerrado                                                          #
# --------------------------------------------------------------------------- #


def test_the_new_codes_are_in_the_closed_vocabulary():
    assert CODE_READONLY_PROC_GRANT_ACK_MISMATCH in ERROR_CODES
    assert CODE_READONLY_PROC_GRANT_ENGINE_UNSUPPORTED in ERROR_CODES


def test_the_acknowledgement_text_warns_that_the_grant_is_server_wide():
    assert "mysql.proc" in READONLY_PROC_ACK_TEXT
    assert "TODAS" in READONLY_PROC_ACK_TEXT
    assert "filtrado del gateway" in READONLY_PROC_ACK_TEXT


# --------------------------------------------------------------------------- #
# S6.3: capacidad, step-up y acknowledgement                                   #
# --------------------------------------------------------------------------- #


def test_it_requires_authentication(client, motor):
    assert client.put(URL.format(sid=1), json={"enabled": False}).status_code in (401, 403)
    assert motor.provisiones == []


def test_it_requires_the_servers_admin_capability(
    admin_client, owner_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    r = _habilitar(owner_client, sid)
    assert r.status_code == 403
    assert _bandera(sid) is False
    assert motor.provisiones == []


def test_it_requires_step_up(admin_client, server_payload, motor, expire_step_up):
    sid = _servidor(admin_client, server_payload)
    expire_step_up(admin_client)
    r = _habilitar(admin_client, sid)
    assert r.status_code == 403
    assert _codigo(r) == "access.step_up_required"
    assert _bandera(sid) is False
    assert motor.provisiones == []


def test_disabling_also_requires_step_up(admin_client, server_payload, motor, expire_step_up):
    sid = _servidor(admin_client, server_payload)
    expire_step_up(admin_client)
    assert _deshabilitar(admin_client, sid).status_code == 403
    assert motor.provisiones == []


@pytest.mark.parametrize(
    "acknowledgement",
    [
        None,
        "",
        "entiendo",
        READONLY_PROC_ACK_TEXT + " ",
        " " + READONLY_PROC_ACK_TEXT,
        READONLY_PROC_ACK_TEXT.lower(),
        READONLY_PROC_ACK_TEXT[:-1],
        READONLY_PROC_ACK_TEXT.replace("TODAS", "todas"),
    ],
)
def test_s6_3_enabling_without_the_exact_acknowledgement_is_refused_and_changes_nothing(
    admin_client, server_payload, motor, acknowledgement
):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    provisiones_antes = len(motor.provisiones)
    password_antes = _fila(sid).readonly_password_encrypted

    r = _habilitar(admin_client, sid, ack=acknowledgement)

    assert r.status_code == 422
    assert _codigo(r) == CODE_READONLY_PROC_GRANT_ACK_MISMATCH
    assert _bandera(sid) is False
    assert motor.proc_otorgado is False
    assert len(motor.provisiones) == provisiones_antes, "ni un GRANT"
    assert _fila(sid).readonly_password_encrypted == password_antes
    assert _auditoria() == [], "un rechazo previo no abre ni cierra una operación"


def test_the_body_rejects_unknown_fields_and_a_missing_enabled(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    assert admin_client.put(URL.format(sid=sid), json={}).status_code == 422
    r = admin_client.put(
        URL.format(sid=sid),
        json={"enabled": False, "acknowledgement": None, "grants": ["ALL PRIVILEGES"]},
    )
    assert r.status_code == 422
    assert motor.provisiones == []


def test_the_endpoint_is_rate_limited_to_3_per_minute(admin_client, server_payload, motor):
    from app.core.limiter import limiter

    sid = _servidor(admin_client, server_payload)
    limiter.enabled = True
    limiter.reset()
    try:
        for _ in range(3):
            assert _habilitar(admin_client, sid, ack="mal").status_code == 422
        assert _habilitar(admin_client, sid, ack="mal").status_code == 429
    finally:
        limiter.enabled = False
        limiter.reset()


# --------------------------------------------------------------------------- #
# S6.6: motores que no lo necesitan                                            #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "engine, version",
    [
        ("postgresql", "16.2"),
        ("mysql", "8.0.36"),
        ("mysql", "8.0.19"),
        ("mysql", "9.1.0"),
        ("mariadb", "11.3.0-MariaDB"),
        ("mariadb", "11.4.2-MariaDB"),
        ("mysql", None),
    ],
)
def test_s6_6_enabling_on_an_engine_that_does_not_need_it_is_422_and_changes_nothing(
    admin_client, server_payload, motor, engine, version
):
    sid = _servidor(admin_client, server_payload, engine=engine)
    motor.dialect = engine
    motor.version = version

    r = _habilitar(admin_client, sid)

    assert r.status_code == 422
    assert _codigo(r) == CODE_READONLY_PROC_GRANT_ENGINE_UNSUPPORTED
    assert _bandera(sid) is False
    assert motor.provisiones == []
    assert motor.proc_otorgado is False
    assert _auditoria() == []


def test_postgresql_is_decided_by_the_dialect_without_connecting(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload, engine="postgresql")
    assert _habilitar(admin_client, sid).status_code == 422
    assert motor.conexiones_root == 0


@pytest.mark.parametrize(
    "engine, version",
    [("mysql", "5.7.44"), ("mariadb", "10.11.6-MariaDB"), ("mariadb", "11.2.4-MariaDB")],
)
def test_the_engines_that_need_it_accept_it(admin_client, server_payload, motor, engine, version):
    sid = _servidor(admin_client, server_payload, engine=engine)
    motor.dialect = engine
    motor.version = version
    _aprovisionar(admin_client, sid)
    assert _habilitar(admin_client, sid).status_code == 200
    assert _bandera(sid) is True


# --------------------------------------------------------------------------- #
# S6.4: credencial propia del gateway, re-convergencia                         #
# --------------------------------------------------------------------------- #


def test_enabling_a_gateway_owned_credential_regrants_with_mysql_proc_and_stays_verified(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    assert motor.proc_otorgado is False

    r = _habilitar(admin_client, sid)

    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["engine_grant"] == "converged"
    assert data["server"]["readonly_proc_grant"] is True
    assert data["server"]["has_readonly_credential"] is True
    assert data["server"]["readonly_verified_at"] is not None
    assert motor.provisiones[-1] == (True, True)
    assert motor.proc_otorgado is True
    assert motor.sondas[-1] is True, "la sonda juzga el estado recién dejado, no el anterior"
    assert _bandera(sid) is True
    assert _fila(sid).readonly_verified_at is not None


def test_s6_4_disabling_revokes_regrants_without_mysql_proc_and_clears_the_flag(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    assert _habilitar(admin_client, sid).status_code == 200
    assert motor.proc_otorgado is True

    r = _deshabilitar(admin_client, sid)

    assert r.status_code == 200, r.text
    assert r.json()["data"]["engine_grant"] == "converged"
    assert r.json()["data"]["server"]["readonly_proc_grant"] is False
    assert motor.provisiones[-1] == (False, True)
    assert motor.proc_otorgado is False, "deshabilitar REVOCA mysql.proc, nunca lo deja"
    assert motor.sondas[-1] is False
    assert _bandera(sid) is False
    assert _fila(sid).readonly_verified_at is not None


def test_s6_4_repeating_either_direction_is_idempotent(admin_client, server_payload, motor):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    for _ in range(2):
        assert _habilitar(admin_client, sid).status_code == 200
        assert motor.proc_otorgado is True
        assert _bandera(sid) is True
    for _ in range(2):
        assert _deshabilitar(admin_client, sid).status_code == 200
        assert motor.proc_otorgado is False
        assert _bandera(sid) is False
    assert _fila(sid).readonly_verified_at is not None


def test_each_convergence_stores_the_password_the_engine_last_received(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    antes = decrypt(_fila(sid).readonly_password_encrypted)
    assert _habilitar(admin_client, sid).status_code == 200
    despues = decrypt(_fila(sid).readonly_password_encrypted)
    assert antes != despues
    assert _fila(sid).readonly_verified_at is not None


def test_the_plain_provision_keeps_the_flag_the_row_already_has(
    admin_client, server_payload, motor
):
    """Re-aprovisionar (POST) con la bandera encendida conserva mysql.proc; apagada, no lo da."""
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    assert _habilitar(admin_client, sid).status_code == 200

    _aprovisionar(admin_client, sid)
    assert motor.provisiones[-1] == (True, True)
    assert motor.proc_otorgado is True
    assert _fila(sid).readonly_verified_at is not None

    assert _deshabilitar(admin_client, sid).status_code == 200
    _aprovisionar(admin_client, sid)
    assert motor.provisiones[-1] == (False, True)
    assert motor.proc_otorgado is False


def test_a_flag_left_on_after_an_engine_upgrade_does_not_grant_mysql_proc(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    assert _habilitar(admin_client, sid).status_code == 200
    motor.version = "11.4.2-MariaDB"
    motor.dialect = "mysql"

    _aprovisionar(admin_client, sid)

    assert motor.provisiones[-1] == (True, False)
    assert motor.proc_otorgado is False
    assert _fila(sid).readonly_verified_at is not None


# --------------------------------------------------------------------------- #
# Orden fail-safe: el corte es inmediato y la bandera cambia tras el motor     #
# --------------------------------------------------------------------------- #


def test_a_failed_disable_cuts_the_server_off_and_keeps_the_flag_so_a_retry_revokes(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    assert _habilitar(admin_client, sid).status_code == 200
    assert _fila(sid).readonly_verified_at is not None

    motor.falla_al_provisionar = True
    r = _deshabilitar(admin_client, sid)

    assert r.status_code == 502
    assert _bandera(sid) is True, "la bandera se limpia SOLO tras la confirmación del motor"
    assert _fila(sid).readonly_verified_at is None, "el corte es inmediato: sin verificar"
    assert motor.proc_otorgado is True
    assert ("server.readonly_proc_grant.set", "error") in [
        (a, s) for a, s, _d in _auditoria()
    ]

    motor.falla_al_provisionar = False
    assert _deshabilitar(admin_client, sid).status_code == 200
    assert motor.proc_otorgado is False
    assert _bandera(sid) is False
    assert _fila(sid).readonly_verified_at is not None


def test_a_failed_enable_leaves_the_flag_off(admin_client, server_payload, motor):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    motor.falla_al_provisionar = True

    r = _habilitar(admin_client, sid)

    assert r.status_code == 502
    assert _bandera(sid) is False
    assert motor.proc_otorgado is False
    assert _fila(sid).readonly_verified_at is None


def test_a_probe_failure_after_the_regrant_does_not_flip_the_flag(
    admin_client, server_payload, motor, monkeypatch
):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)
    original = _AdapterFalso.readonly_violations
    monkeypatch.setattr(
        _AdapterFalso,
        "readonly_violations",
        lambda self, *, allow_mysql_proc=False: ["privilege:insert"],
    )
    r = _habilitar(admin_client, sid)
    monkeypatch.setattr(_AdapterFalso, "readonly_violations", original)

    assert r.status_code == 422
    assert _codigo(r) == "server.readonly_probe_failed"
    assert _bandera(sid) is False
    assert _fila(sid).readonly_verified_at is None


def test_it_shares_the_provisioning_lock(admin_client, server_payload, motor):
    from app.controllers.server_controller import _release_provision, _try_acquire_provision

    sid = _servidor(admin_client, server_payload)
    assert _try_acquire_provision(sid)
    try:
        r = _deshabilitar(admin_client, sid)
    finally:
        _release_provision(sid)
    assert r.status_code == 409
    assert _codigo(r) == CODE_READONLY_PROVISION_IN_PROGRESS
    assert motor.provisiones == []
    assert _auditoria() == []


# --------------------------------------------------------------------------- #
# S6.5: credencial registrada a mano                                           #
# --------------------------------------------------------------------------- #


def test_s6_5_a_manual_credential_is_only_reprobed_and_reported_not_alterable(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    _marcar_verificada(sid)
    cifrado_antes = _fila(sid).readonly_password_encrypted

    r = _habilitar(admin_client, sid)

    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["engine_grant"] == "not_alterable"
    assert data["server"]["readonly_proc_grant"] is True
    assert motor.provisiones == [], "los grants del motor no se tocan"
    assert motor.sondas == [True], "solo se re-corrió la sonda, con la bandera nueva"
    assert _fila(sid).readonly_password_encrypted == cifrado_antes
    assert _fila(sid).readonly_verified_at is not None


def test_s6_5_disabling_a_manual_credential_that_still_has_mysql_proc_closes_the_server(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    assert _habilitar(admin_client, sid).status_code == 200
    motor.proc_otorgado = True  # lo otorgó el operador a mano y el gateway no lo revoca
    _marcar_verificada(sid)

    r = _deshabilitar(admin_client, sid)

    assert r.status_code == 200, r.text
    assert r.json()["data"]["engine_grant"] == "not_alterable"
    assert r.json()["data"]["server"]["readonly_proc_grant"] is False
    assert r.json()["data"]["server"]["readonly_verified_at"] is None
    assert motor.provisiones == []
    assert motor.proc_otorgado is True, "el gateway no alteró grants que no creó"
    assert _bandera(sid) is False
    assert _fila(sid).readonly_verified_at is None


def test_a_server_without_a_credential_only_changes_the_flag(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)

    r = _habilitar(admin_client, sid)

    assert r.status_code == 200, r.text
    assert r.json()["data"]["engine_grant"] == "no_credential"
    assert _bandera(sid) is True
    assert motor.provisiones == []
    assert motor.sondas == []
    assert _deshabilitar(admin_client, sid).json()["data"]["engine_grant"] == "no_credential"
    assert _bandera(sid) is False


# --------------------------------------------------------------------------- #
# Auditoría                                                                    #
# --------------------------------------------------------------------------- #


def test_the_operation_is_audited_with_intent_then_result_and_no_secrets(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    _aprovisionar(admin_client, sid)

    assert _habilitar(admin_client, sid).status_code == 200

    filas = _auditoria()
    assert [s for _a, s, _d in filas] == ["attempt", "success"]
    contraseña = decrypt(_fila(sid).readonly_password_encrypted)
    for _accion, _estado, detalle in filas:
        assert contraseña not in (detalle or "")
        assert READONLY_PROC_ACK_TEXT not in (detalle or "")
    assert "habilitar" in filas[0][2]
    assert "habilitado" in filas[1][2]


def test_a_manual_credential_disable_is_audited_too(admin_client, server_payload, motor):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    assert _deshabilitar(admin_client, sid).status_code == 200
    assert [s for _a, s, _d in _auditoria()] == ["attempt", "success"]


# --------------------------------------------------------------------------- #
# ServerOut y flag_off                                                         #
# --------------------------------------------------------------------------- #


def test_server_out_exposes_the_flag_defaulting_to_off(admin_client, server_payload, motor):
    sid = _servidor(admin_client, server_payload)
    assert admin_client.get(f"/api/v1/servers/{sid}").json()["data"]["readonly_proc_grant"] is False
    assert _habilitar(admin_client, sid).status_code == 200
    assert admin_client.get(f"/api/v1/servers/{sid}").json()["data"]["readonly_proc_grant"] is True
    listado = admin_client.get("/api/v1/servers").json()["data"]
    fila = next(s for s in _items(listado) if s["id"] == sid)
    assert fila["readonly_proc_grant"] is True


def _items(data):
    return data["items"] if isinstance(data, dict) and "items" in data else data


def test_the_model_column_is_not_nullable_defaults_off_and_documents_the_risk():
    columna = Server.__table__.columns["readonly_proc_grant"]
    assert columna.nullable is False
    assert columna.default.arg is False
    assert columna.server_default is not None
    assert "TODAS las bases" in columna.comment
