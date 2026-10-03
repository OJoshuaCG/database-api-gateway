"""
Aprovisionamiento de UN click de la credencial de solo lectura del MCP.

Sin motor real: el adapter es un fake que registra qué se le pidió. Lo que se prueba es lo que
decide el gateway: qué le pide al motor, qué guarda, qué audita y qué deja verificado.
"""

import logging

import pytest

from app.core.crypto import decrypt
from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.audit_log import AuditLog
from app.services.db_admin.dtos import ConnectionInfo
from app.services.db_admin.readonly_probe import (
    MYSQL_ALLOWED_PRIVILEGES,
    MYSQL_READONLY_DB_GRANTS,
    MYSQL_READONLY_GLOBAL_GRANTS,
    ReadonlyPreflight,
)

URL = "/api/v1/servers/{sid}/readonly-credential/provision"


def _servidor(admin_client, server_payload, **overrides) -> int:
    r = admin_client.post("/api/v1/servers", json=server_payload(**overrides))
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


def _fila(sid):
    from app.models.server import Server

    s = Database().get_declarative_base_session()
    try:
        return s.get(Server, sid)
    finally:
        s.close()


def _registrar(admin_client, sid, username="mcp_ro", password="registrada-a-mano-xyz"):
    """Registra a mano (PUT) una credencial de solo lectura: el gateway pasa a 'tenerla'."""
    r = admin_client.put(
        f"/api/v1/servers/{sid}/readonly-credential",
        json={"username": username, "password": password},
    )
    assert r.status_code == 200, r.text


def _marcar_verificada(sid):
    from datetime import datetime

    from app.models.server import Server

    s = Database().get_declarative_base_session()
    try:
        s.get(Server, sid).readonly_verified_at = datetime(2026, 1, 1)
        s.commit()
    finally:
        s.close()


def _auditoria() -> list[tuple[str, str, str | None]]:
    s = Database().get_declarative_base_session()
    try:
        return [(a.action, a.status, a.detail) for a in s.query(AuditLog).all()]
    finally:
        s.close()


class _MotorFalso:
    """Un motor: la pseudo-root provisiona y la cuenta de solo lectura es sondeada."""

    def __init__(self):
        self.llamadas = []  # (usuario, password, host, bases)
        self.existe = False
        self.privilegiado = False
        self.bases = ["app_prod", "otra", "mysql", "datum_meta"]
        self.violaciones: list[str] = []
        self.falla_al_provisionar = False
        self.con_roles = False  # el preflight rechaza: la cuenta tiene roles otorgados
        self.preflights = []  # (usuario, host): lecturas previas a mutar
        self.objetivos = []  # targets con los que se pidió un adapter

    def adapter(self, target):
        self.objetivos.append(target)
        return _AdapterFalso(self)


class _AdapterFalso:
    def __init__(self, motor: _MotorFalso):
        self._m = motor

    def list_databases(self):
        return list(self._m.bases)

    def is_privileged_role(self, username):
        return self._m.privilegiado

    def preflight_readonly_account(self, username, host):
        """Solo lectura: no muta NADA (lo que el controller exige antes de tocar el motor)."""
        self._m.preflights.append((username, host))
        if self._m.con_roles:
            raise AppHttpException(
                message="La cuenta tiene roles.",
                status_code=409,
                public_context={"code": "readonly_account.has_roles"},
            )
        return ReadonlyPreflight(exists=self._m.existe)

    def provision_readonly_account(self, username, password, host, databases, preflight):
        if self._m.falla_al_provisionar:
            raise AppHttpException(message="El motor rechazó la operación.", status_code=502)
        self._m.llamadas.append((username, password, host, list(databases)))
        self._m.existe = True

    def test_connection(self):
        return ConnectionInfo(ok=True, dialect="mysql", server_version="8.0.36")

    def readonly_violations(self):
        return list(self._m.violaciones)


@pytest.fixture()
def motor(monkeypatch):
    import app.controllers.server_controller as ctrl

    m = _MotorFalso()
    monkeypatch.setattr(ctrl, "get_adapter", m.adapter)
    # La base de metadatos del gateway co-alojada: debe quedar fuera de los grants.
    monkeypatch.setattr(ctrl, "DB_NAME", "datum_meta")
    monkeypatch.setattr(ctrl, "DB_HOST", "127.0.0.1")
    monkeypatch.setattr(ctrl, "DB_PORT", 3399)
    return m


# --------------------------------------------------------------------------- #
# Camino feliz                                                                 #
# --------------------------------------------------------------------------- #


def test_provisioning_creates_stores_and_verifies(admin_client, server_payload, motor):
    sid = _servidor(admin_client, server_payload)
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["has_readonly_credential"] is True
    assert data["readonly_verified_at"] is not None

    usuario, password, host, bases = motor.llamadas[0]
    assert usuario == "mcp_ro"
    assert len(password) >= 32
    assert host == "%"
    # Bases del sistema y la de metadatos del gateway quedan FUERA.
    assert bases == ["app_prod", "otra"]

    fila = _fila(sid)
    assert fila.readonly_username == "mcp_ro"
    assert fila.readonly_password_encrypted != password
    assert decrypt(fila.readonly_password_encrypted) == password
    assert fila.readonly_verified_at is not None


def test_the_first_target_is_the_root_and_the_probe_uses_the_readonly_account(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    admin_client.post(URL.format(sid=sid))
    root, ro = motor.objetivos[0], motor.objetivos[-1]
    assert root.admin_user == "root"
    assert ro.admin_user == "mcp_ro"


def test_the_request_body_is_not_accepted_as_input(admin_client, server_payload, motor):
    """Ni password ni grants ni usuario los elige el cliente: se ignora cualquier cuerpo."""
    sid = _servidor(admin_client, server_payload)
    r = admin_client.post(
        URL.format(sid=sid),
        json={"password": "elegida-por-el-cliente", "grants": ["ALL PRIVILEGES"], "username": "x"},
    )
    assert r.status_code == 200, r.text
    usuario, password, _host, _bases = motor.llamadas[0]
    assert usuario == "mcp_ro"
    assert password != "elegida-por-el-cliente"


def test_the_fixed_grants_pass_the_probe_allowlist():
    """Lo que se otorga TIENE que ser lo que la sonda tolera: si divergen, nada verifica."""
    assert set(MYSQL_READONLY_DB_GRANTS) <= MYSQL_ALLOWED_PRIVILEGES
    assert set(MYSQL_READONLY_GLOBAL_GRANTS) == {"SHOW_ROUTINE"}
    assert set(MYSQL_READONLY_DB_GRANTS) == {"SELECT", "SHOW VIEW", "TRIGGER", "EVENT"}


# --------------------------------------------------------------------------- #
# Idempotencia y recuperación                                                  #
# --------------------------------------------------------------------------- #


def test_an_existing_account_of_ours_is_rotated_not_rejected(admin_client, server_payload, motor):
    """Propia = el gateway ya guarda una credencial con ESE usuario (registrada por PUT)."""
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    motor.existe = True
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 200, r.text
    detalles = [d for a, _s, d in _auditoria() if a == "server.readonly_credential.provision"]
    assert any("rotada" in (d or "") for d in detalles)


def test_running_it_twice_converges_with_a_new_password(admin_client, server_payload, motor):
    sid = _servidor(admin_client, server_payload)
    assert admin_client.post(URL.format(sid=sid)).status_code == 200
    primera = decrypt(_fila(sid).readonly_password_encrypted)
    assert admin_client.post(URL.format(sid=sid)).status_code == 200
    segunda = decrypt(_fila(sid).readonly_password_encrypted)
    assert primera != segunda
    # Lo guardado es siempre lo que el motor recibió por última vez.
    assert motor.llamadas[-1][1] == segunda
    assert _fila(sid).readonly_verified_at is not None


def test_an_engine_failure_is_retryable_and_leaves_the_server_unverified(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    assert admin_client.post(URL.format(sid=sid)).status_code == 200
    assert _fila(sid).readonly_verified_at is not None

    motor.falla_al_provisionar = True
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 502
    # La contraseña guardada ya no es la del motor: no puede seguir figurando verificada.
    assert _fila(sid).readonly_verified_at is None
    assert ("server.readonly_credential.provision", "error") in [
        (a, s) for a, s, _d in _auditoria()
    ]

    motor.falla_al_provisionar = False
    assert admin_client.post(URL.format(sid=sid)).status_code == 200
    assert _fila(sid).readonly_verified_at is not None


def test_a_retry_after_a_failure_creating_the_account_converges(
    admin_client, server_payload, motor
):
    """
    El gateway guarda la propiedad ANTES de que el motor mute. Si el motor falla a mitad (la
    cuenta pudo quedar creada), el reintento ve usuario guardado == configurado: es propia y
    converge en vez de chocar con 'already_exists'.
    """
    sid = _servidor(admin_client, server_payload)
    motor.falla_al_provisionar = True
    assert admin_client.post(URL.format(sid=sid)).status_code == 502
    fila = _fila(sid)
    assert fila.readonly_username == "mcp_ro"  # propiedad registrada
    assert fila.readonly_verified_at is None  # honestamente sin verificar

    motor.existe = True  # la cuenta quedó creada en el motor antes de fallar
    motor.falla_al_provisionar = False
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 200, r.text
    assert _fila(sid).readonly_verified_at is not None
    assert motor.llamadas[-1][1] == decrypt(_fila(sid).readonly_password_encrypted)


# --------------------------------------------------------------------------- #
# Toda precondición se detecta ANTES de la primera mutación                    #
# --------------------------------------------------------------------------- #


def test_an_existing_account_that_is_not_ours_is_refused_without_changes(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid, username="otra_cuenta", password="de-otro-xyz")
    _marcar_verificada(sid)
    antes = _fila(sid)
    cifrado_antes = antes.readonly_password_encrypted
    motor.existe = True  # 'mcp_ro' existe en el motor y el gateway no la tiene guardada

    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "readonly_account.already_exists"
    assert motor.llamadas == []  # ninguna sentencia que muta
    fila = _fila(sid)
    assert fila.readonly_username == "otra_cuenta"
    assert fila.readonly_password_encrypted == cifrado_antes
    assert fila.readonly_verified_at is not None  # la verificación NO se tocó
    assert not [e for e in _auditoria() if e[0] == "server.readonly_credential.provision"]


def test_an_existing_account_with_no_stored_credential_is_refused(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    motor.existe = True
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "readonly_account.already_exists"
    assert motor.llamadas == []
    assert _fila(sid).readonly_username is None


def test_a_preflight_failure_such_as_roles_changes_nothing(admin_client, server_payload, motor):
    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    _marcar_verificada(sid)
    motor.existe = True
    motor.con_roles = True
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "readonly_account.has_roles"
    assert motor.llamadas == []
    assert _fila(sid).readonly_verified_at is not None


def test_a_bad_configured_host_is_rejected_before_clearing_the_verification(
    admin_client, server_payload, motor, monkeypatch
):
    import app.controllers.server_controller as ctrl

    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    _marcar_verificada(sid)
    monkeypatch.setattr(ctrl, "MCP_READONLY_ACCOUNT_HOST", "bad host';--")
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 422
    assert motor.preflights == [] and motor.llamadas == []
    assert _fila(sid).readonly_verified_at is not None
    assert not [e for e in _auditoria() if e[0] == "server.readonly_credential.provision"]


def test_a_bad_configured_username_is_rejected_before_clearing_the_verification(
    admin_client, server_payload, motor, monkeypatch
):
    import app.controllers.server_controller as ctrl

    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    _marcar_verificada(sid)
    monkeypatch.setattr(ctrl, "MCP_READONLY_ACCOUNT_USERNAME", "bad`user")
    assert admin_client.post(URL.format(sid=sid)).status_code == 422
    assert motor.llamadas == []
    assert _fila(sid).readonly_verified_at is not None


def test_a_provision_in_progress_for_the_same_server_returns_409_and_changes_nothing(
    admin_client, server_payload, motor
):
    import app.controllers.server_controller as ctrl

    sid = _servidor(admin_client, server_payload)
    _registrar(admin_client, sid)
    _marcar_verificada(sid)
    assert ctrl._try_acquire_provision(sid) is True
    try:
        r = admin_client.post(URL.format(sid=sid))
    finally:
        ctrl._release_provision(sid)
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "readonly_provision.in_progress"
    assert motor.preflights == [] and motor.llamadas == []
    assert _fila(sid).readonly_verified_at is not None
    # Liberado el lock, el siguiente click procede.
    assert admin_client.post(URL.format(sid=sid)).status_code == 200


def test_the_lock_is_released_even_when_the_provision_fails(admin_client, server_payload, motor):
    import app.controllers.server_controller as ctrl

    sid = _servidor(admin_client, server_payload)
    motor.falla_al_provisionar = True
    assert admin_client.post(URL.format(sid=sid)).status_code == 502
    assert sid not in ctrl._PROVISIONING


# --------------------------------------------------------------------------- #
# La sonda manda                                                               #
# --------------------------------------------------------------------------- #


def test_a_failing_probe_returns_422_and_leaves_it_unverified(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    motor.violaciones = ["privilege:insert"]
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 422
    ctx = r.json()["detail"]["public_context"]
    assert ctx["code"] == "server.readonly_probe_failed"
    assert ctx["violations"] == ["privilege:insert"]
    fila = _fila(sid)
    assert fila.readonly_verified_at is None
    # La credencial queda registrada (el DBA puede corregir grants y re-verificar).
    assert fila.readonly_username == "mcp_ro"


# --------------------------------------------------------------------------- #
# Guardas de cuenta                                                            #
# --------------------------------------------------------------------------- #


def test_a_privileged_postgres_role_is_never_taken_over(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload, engine="postgresql", port=5432)
    motor.privilegiado = True
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 409
    assert r.json()["detail"]["public_context"]["code"] == "engine_user.protected_account"
    assert motor.llamadas == []


def test_the_configured_username_cannot_be_the_root_account(
    admin_client, server_payload, motor, monkeypatch
):
    import app.controllers.server_controller as ctrl

    monkeypatch.setattr(ctrl, "MCP_READONLY_ACCOUNT_USERNAME", "root")
    sid = _servidor(admin_client, server_payload)
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 409
    assert motor.llamadas == []


def test_an_unknown_server_is_a_404(admin_client, motor):
    assert admin_client.post(URL.format(sid=99999)).status_code == 404


# --------------------------------------------------------------------------- #
# Ningún secreto sale ni se loguea                                             #
# --------------------------------------------------------------------------- #


def test_the_password_never_appears_in_responses_logs_or_audit(
    admin_client, server_payload, motor
):
    # Handler propio y no `caplog`: el runner directo del repo no soporta esa fixture.
    registros: list[str] = []

    class _Captura(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            registros.append(self.format(record))

    raiz = logging.getLogger()
    captura = _Captura(logging.DEBUG)
    nivel_previo = raiz.level
    raiz.addHandler(captura)
    raiz.setLevel(logging.DEBUG)
    sid = _servidor(admin_client, server_payload)
    try:
        r = admin_client.post(URL.format(sid=sid))
    finally:
        raiz.removeHandler(captura)
        raiz.setLevel(nivel_previo)
    assert r.status_code == 200, r.text
    password = motor.llamadas[0][1]
    for texto in (
        r.text,
        admin_client.get(f"/api/v1/servers/{sid}").text,
        "\n".join(registros),
        " ".join(d or "" for _a, _s, d in _auditoria()),
    ):
        assert password not in texto
        assert "readonly_password" not in texto


def test_the_failure_response_does_not_leak_the_password(
    admin_client, server_payload, motor
):
    sid = _servidor(admin_client, server_payload)
    motor.violaciones = ["privilege:insert"]
    r = admin_client.post(URL.format(sid=sid))
    assert motor.llamadas[0][1] not in r.text


# --------------------------------------------------------------------------- #
# Auditoría                                                                    #
# --------------------------------------------------------------------------- #


def test_provisioning_is_audited_with_intent_and_outcome(admin_client, server_payload, motor):
    sid = _servidor(admin_client, server_payload)
    admin_client.post(URL.format(sid=sid))
    estados = [s for a, s, _d in _auditoria() if a == "server.readonly_credential.provision"]
    assert "attempt" in estados and "success" in estados
    acciones = [a for a, _s, _d in _auditoria()]
    assert "server.readonly_credential.set" in acciones
    assert "server.readonly_credential.verify" in acciones


# --------------------------------------------------------------------------- #
# Capacidad, step-up y el MCP                                                  #
# --------------------------------------------------------------------------- #


def test_it_requires_step_up(admin_client, server_payload, motor, expire_step_up):
    sid = _servidor(admin_client, server_payload)
    expire_step_up(admin_client)
    r = admin_client.post(URL.format(sid=sid))
    assert r.status_code == 403
    assert r.json()["detail"]["public_context"]["code"] == "access.step_up_required"
    assert motor.llamadas == []


def test_it_requires_the_servers_admin_capability(
    admin_client, owner_client, server_payload, motor
):
    """Un ``owner`` opera pero no tiene ``servers.admin`` (es del security_officer)."""
    sid = _servidor(admin_client, server_payload)
    r = owner_client.post(URL.format(sid=sid))
    assert r.status_code == 403
    assert motor.llamadas == []


def test_it_requires_authentication(client, motor):
    assert client.post(URL.format(sid=1)).status_code in (401, 403)
    assert motor.llamadas == []


def test_the_mcp_registry_exposes_no_provisioning_tool():
    from app.mcp.registry import TOOLS

    for t in TOOLS:
        nombre = f"{t.name} {t.description}".lower()
        assert "provision" not in nombre, t.name
        assert "credential" not in t.name, t.name
        assert "create_user" not in nombre, t.name


def test_the_mcp_package_cannot_reach_the_server_controller():
    import ast
    import pathlib

    raiz = pathlib.Path(__file__).resolve().parents[1] / "app" / "mcp"
    for archivo in raiz.rglob("*.py"):
        arbol = ast.parse(archivo.read_text(encoding="utf-8"))
        for nodo in ast.walk(arbol):
            modulos = []
            if isinstance(nodo, ast.Import):
                modulos = [a.name for a in nodo.names]
            elif isinstance(nodo, ast.ImportFrom) and nodo.module:
                modulos = [nodo.module]
            for m in modulos:
                assert "server_controller" not in m, (archivo.name, m)
                assert "db_admin" not in m, (archivo.name, m)
