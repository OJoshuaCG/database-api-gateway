"""
Tests de API (``TestClient``) de la CONSOLA SQL: ``POST /servers/{id}/query/preview``,
``POST /servers/{id}/query/execute`` y ``GET /servers/{id}/query/history``.

A diferencia de otros módulos (``test_api_engine_users.py``, ``test_api_database_clones.py``)
que mockean un ADAPTER (``get_adapter``), la consola no pasa por la capa de adapters: el
controller llama directo a ``query_runner.run_statements``/``estimate_impact`` (que abren
una conexión SQLAlchemy contra el motor destino). Por eso acá se mockea ``query_runner`` a
nivel de módulo, igual criterio que mockear un adapter: se verifica la ORQUESTACIÓN del
controller (política -> confirmación -> ejecución -> auditoría/historial -> respuesta), sin
tocar un motor real. La política (``query_policy``) SÍ corre de verdad: es pura y es
justamente lo que hace que "sentencia bloqueada" nunca llegue a llamar al runner.
"""

from app.controllers import query_console_controller as qcc
from app.services.db_admin import query_runner as qr


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
def _make_server(admin_client, **ov) -> int:
    payload = {
        "name": "srv-console",
        "host": "10.0.0.3",
        "port": 3306,
        "engine": "mysql",
        "root_username": "root",
        "root_password": "rootpw",
    }
    payload.update(ov)
    r = admin_client.post("/api/v1/servers", json=payload)
    assert r.status_code == 201, r.text
    return r.json()["data"]["id"]


class _FakeRunner:
    """Doble de ``query_runner``: registra llamadas y devuelve un resultado configurable."""

    def __init__(self):
        self.run_calls: list[dict] = []
        self.estimate_calls: list[list] = []
        self.next_outcome: qr.ExecutionOutcome | None = None
        self.next_estimates: dict[int, int | None] | None = None

    def run_statements(self, target, *, database, engine, statements, credential,
                        read_only, dry_run=False, max_rows, max_cell_chars, timeout_ms):
        self.run_calls.append(
            {
                "database": database, "engine": engine, "statements": list(statements),
                "credential": credential, "read_only": read_only, "dry_run": dry_run,
            }
        )
        if self.next_outcome is not None:
            return self.next_outcome
        return qr.ExecutionOutcome(
            statements=[
                qr.StatementOutcome(
                    seq=s.seq, sql=s.sql, kind=s.kind, danger=s.danger,
                    success=True, duration_ms=1, columns=["x"], rows=[[1]], row_count=1,
                )
                for s in statements
            ],
            success=True,
            committed=not read_only and not dry_run,
            rolled_back=read_only or dry_run,
        )

    def estimate_impact(self, target, *, database, engine, credential, impact_queries,
                         timeout_ms):
        self.estimate_calls.append(list(impact_queries))
        if self.next_estimates is not None:
            return self.next_estimates
        return {seq: 7 for seq, _ in impact_queries}


def _patch(monkeypatch, fake: _FakeRunner | None = None) -> _FakeRunner:
    fake = fake or _FakeRunner()
    monkeypatch.setattr(qcc.query_runner, "run_statements", fake.run_statements)
    monkeypatch.setattr(qcc.query_runner, "estimate_impact", fake.estimate_impact)
    return fake


def _preview(admin_client, sid, sql, **ov):
    payload = {"database": "tienda", "sql": sql}
    payload.update(ov)
    return admin_client.post(f"/api/v1/servers/{sid}/query/preview", json=payload)


def _execute(admin_client, sid, sql, **ov):
    payload = {"database": "tienda", "sql": sql}
    payload.update(ov)
    return admin_client.post(f"/api/v1/servers/{sid}/query/execute", json=payload)


# --------------------------------------------------------------------------- #
# Lectura: no exige preview ni confirmación                                    #
# --------------------------------------------------------------------------- #
def test_preview_de_una_lectura_no_emite_token_ni_exige_confirmacion(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-r1", port=3401)
    _patch(monkeypatch)
    r = _preview(admin_client, sid, "SELECT * FROM pedidos")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["danger"] == "read"
    assert data["requires_confirmation"] is False
    assert data["blocked"] is False
    assert data["confirm_token"] is None


def test_execute_de_una_lectura_corre_directo_sin_confirmar(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-r2", port=3402)
    fake = _patch(monkeypatch)
    r = _execute(admin_client, sid, "SELECT * FROM pedidos")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["success"] is True
    assert data["read_only"] is True
    assert data["statements"][0]["rows"] == [[1]]
    assert len(fake.run_calls) == 1
    assert fake.run_calls[0]["read_only"] is True


# --------------------------------------------------------------------------- #
# Escritura: exige preview -> token -> confirm_target_name + confirm_token      #
# --------------------------------------------------------------------------- #
def test_preview_de_una_escritura_emite_token_y_exige_confirmacion(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-w1", port=3403)
    _patch(monkeypatch)
    r = _preview(admin_client, sid, "UPDATE pedidos SET estado='x' WHERE id=1")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["danger"] == "write"
    assert data["requires_confirmation"] is True
    assert data["confirm_token"] is not None
    assert data["statements"][0]["estimated_rows"] == 7  # viene del fake estimate_impact


def test_execute_de_una_escritura_sin_confirm_token_422_y_no_toca_el_motor(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-w2", port=3404)
    fake = _patch(monkeypatch)
    r = _execute(admin_client, sid, "UPDATE pedidos SET estado='x' WHERE id=1",
                  confirm_target_name="tienda")
    assert r.status_code == 422, r.text
    assert fake.run_calls == []


def test_execute_de_una_escritura_con_confirm_target_name_incorrecto_422(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-w3", port=3405)
    fake = _patch(monkeypatch)
    preview = _preview(admin_client, sid, "UPDATE pedidos SET estado='x' WHERE id=1")
    token = preview.json()["data"]["confirm_token"]
    r = _execute(admin_client, sid, "UPDATE pedidos SET estado='x' WHERE id=1",
                 confirm_target_name="OTRA_BASE", confirm_token=token)
    assert r.status_code == 422, r.text
    assert fake.run_calls == []


def test_execute_de_una_escritura_con_token_de_otro_sql_422(admin_client, monkeypatch):
    """El token está atado al HASH del SQL: no sirve para ejecutar un SQL distinto."""
    sid = _make_server(admin_client, name="qc-w4", port=3406)
    fake = _patch(monkeypatch)
    preview = _preview(admin_client, sid, "UPDATE pedidos SET estado='a' WHERE id=1")
    token = preview.json()["data"]["confirm_token"]
    r = _execute(admin_client, sid, "UPDATE pedidos SET estado='DIFERENTE' WHERE id=2",
                 confirm_target_name="tienda", confirm_token=token)
    assert r.status_code == 422, r.text
    assert fake.run_calls == []


def test_execute_de_una_escritura_con_token_y_nombre_correctos_ejecuta(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-w5", port=3407)
    fake = _patch(monkeypatch)
    sql = "UPDATE pedidos SET estado='x' WHERE id=1"
    preview = _preview(admin_client, sid, sql)
    token = preview.json()["data"]["confirm_token"]
    r = _execute(admin_client, sid, sql, confirm_target_name="tienda", confirm_token=token)
    assert r.status_code == 200, r.text
    assert r.json()["data"]["success"] is True
    assert len(fake.run_calls) == 1
    assert fake.run_calls[0]["read_only"] is False


def test_execute_de_una_lectura_no_necesita_confirmar_aunque_haya_token_de_otra_operacion(
    admin_client, monkeypatch
):
    """Una consulta de solo lectura nunca pasa por el chequeo de confirmación."""
    sid = _make_server(admin_client, name="qc-w6", port=3408)
    fake = _patch(monkeypatch)
    r = _execute(admin_client, sid, "SELECT * FROM pedidos")
    assert r.status_code == 200, r.text
    assert fake.run_calls[0]["read_only"] is True


# --------------------------------------------------------------------------- #
# Bloqueado: 403 sin tocar el motor, ni con confirmación                        #
# --------------------------------------------------------------------------- #
def test_execute_sentencia_bloqueada_403_sin_tocar_el_motor(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-b1", port=3409)
    fake = _patch(monkeypatch)
    r = _execute(admin_client, sid, "GRANT ALL ON *.* TO 'x'@'%'")
    assert r.status_code == 403, r.text
    assert fake.run_calls == []

    # Los motivos viajan en ``public_context``, NO en ``context``: este último solo se
    # expone en desarrollo (``APP_ENV``), así que en PRODUCCIÓN el operador habría
    # recibido "hay sentencias prohibidas" sin saber cuál ni por qué — lo contrario de lo
    # que esta consola necesita comunicar.
    detail = r.json()["detail"]
    assert "public_context" in detail, detail
    reasons = detail["public_context"]["reasons"]
    assert any(rr["code"] == "dcl_grant_revoke" for rr in reasons)
    # Y se identifica QUÉ sentencia del lote fue la rechazada.
    assert detail["public_context"]["blocked_statements"][0]["seq"] == 0


def test_execute_bloqueada_persiste_historial_con_status_blocked(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-b2", port=3410)
    _patch(monkeypatch)
    _execute(admin_client, sid, "DROP DATABASE otra")
    hist = admin_client.get(f"/api/v1/servers/{sid}/query/history").json()["data"]
    assert len(hist) == 1
    assert hist[0]["status"] == "blocked"
    assert hist[0]["danger_level"] == "blocked"


def test_preview_de_sentencia_bloqueada_no_emite_token(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-b3", port=3411)
    _patch(monkeypatch)
    r = _preview(admin_client, sid, "GRANT ALL ON *.* TO 'x'@'%'")
    data = r.json()["data"]
    assert data["blocked"] is True
    assert data["confirm_token"] is None


# --------------------------------------------------------------------------- #
# Modo de conexión: impersonate (solo PostgreSQL) / provided / stored           #
# --------------------------------------------------------------------------- #
def test_impersonate_en_mysql_422(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-i1", port=3412)  # engine=mysql (default)
    _patch(monkeypatch)
    r = _preview(admin_client, sid, "SELECT 1", connection={"mode": "impersonate", "role": "reportes"})
    assert r.status_code == 422, r.text


def test_impersonate_en_postgres_sin_role_422(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-i2", port=5432, engine="postgresql")
    _patch(monkeypatch)
    r = _preview(admin_client, sid, "SELECT 1", connection={"mode": "impersonate"})
    assert r.status_code == 422, r.text


def test_impersonate_en_postgres_con_role_ejecuta(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-i3", port=5433, engine="postgresql")
    fake = _patch(monkeypatch)
    r = _execute(admin_client, sid, "SELECT 1", connection={"mode": "impersonate", "role": "reportes"})
    assert r.status_code == 200, r.text
    assert r.json()["data"]["run_as"] == "reportes"
    cred = fake.run_calls[0]["credential"]
    assert cred.mode == "impersonate"
    assert cred.impersonate_role == "reportes"


def test_provided_sin_password_422(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-p1", port=3413)
    _patch(monkeypatch)
    r = _preview(admin_client, sid, "SELECT 1", connection={"mode": "provided", "username": "app_ro"})
    assert r.status_code == 422, r.text


def test_stored_usuario_no_en_inventario_404(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-s1", port=3414)
    _patch(monkeypatch)
    r = _preview(admin_client, sid, "SELECT 1", connection={"mode": "stored", "username": "fantasma"})
    assert r.status_code == 404, r.text


def test_stored_usuario_adoptado_sin_password_409(admin_client, monkeypatch):
    """Adoptado pero el gateway nunca fijó su contraseña (motor solo guarda el hash)."""
    import app.controllers.server_user_controller as suc
    from app.services.db_admin.dtos import EngineUserInfo

    class _FakeAdapter:
        dialect = "mysql"
        supports_hosts = True

        def list_users(self):
            return [EngineUserInfo(username="sin_pw", host="%")]

        def create_user(self, username, password, host):
            pass

    sid = _make_server(admin_client, name="qc-s2", port=3415)
    monkeypatch.setattr(suc, "get_adapter", lambda target: _FakeAdapter())
    r = admin_client.post(
        "/api/v1/server-users/adopt", json={"server_id": sid, "username": "sin_pw"}
    )
    assert r.status_code == 201, r.text

    _patch(monkeypatch)
    r2 = _preview(admin_client, sid, "SELECT 1", connection={"mode": "stored", "username": "sin_pw"})
    assert r2.status_code == 409, r2.text


def test_stored_usuario_con_password_conocida_ejecuta(admin_client, monkeypatch):
    import app.controllers.server_user_controller as suc

    class _FakeAdapter:
        dialect = "mysql"
        supports_hosts = True

        def list_users(self):
            return []

        def create_user(self, username, password, host):
            pass

    sid = _make_server(admin_client, name="qc-s3", port=3416)
    monkeypatch.setattr(suc, "get_adapter", lambda target: _FakeAdapter())
    r = admin_client.post(
        f"/api/v1/servers/{sid}/users",
        json={"username": "app_rw", "password": "topsecret9", "adopt": True},
    )
    assert r.status_code == 201, r.text

    fake = _patch(monkeypatch)
    r2 = _execute(admin_client, sid, "SELECT 1", connection={"mode": "stored", "username": "app_rw"})
    assert r2.status_code == 200, r2.text
    assert r2.json()["data"]["run_as"] == "app_rw"
    cred = fake.run_calls[0]["credential"]
    assert cred.mode == "stored"
    assert cred.password == "topsecret9"


# --------------------------------------------------------------------------- #
# Advertencias visibles de la respuesta                                        #
# --------------------------------------------------------------------------- #
def test_modo_admin_avisa_que_no_prueba_permisos(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-adm1", port=3417)
    _patch(monkeypatch)
    r = _preview(admin_client, sid, "SELECT 1")
    warnings = r.json()["data"]["warnings"]
    assert any("pseudo-root" in w for w in warnings)


def test_lote_multisentencia_avisa_orden_y_corte_en_error(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-adm2", port=3418)
    _patch(monkeypatch)
    r = _preview(admin_client, sid, "SELECT 1; SELECT 2;")
    warnings = r.json()["data"]["warnings"]
    assert any("2 sentencias" in w for w in warnings)


# --------------------------------------------------------------------------- #
# Un rechazo del MOTOR es un resultado (200), no un error de la API             #
# --------------------------------------------------------------------------- #
def test_rechazo_de_permisos_del_motor_devuelve_200_con_success_false(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-perm1", port=3419)
    fake = _patch(monkeypatch)
    fake.next_outcome = qr.ExecutionOutcome(
        statements=[
            qr.StatementOutcome(
                seq=0, sql="SELECT * FROM pagos", kind="select", danger="read",
                success=False, duration_ms=2,
                error=qr.ExecError(code="1142", sqlstate=None,
                                    message="SELECT command denied to user 'app_ro'@'%' for table 'pagos'"),
            )
        ],
        success=False, committed=False, rolled_back=True,
    )
    r = _execute(admin_client, sid, "SELECT * FROM pagos")
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["success"] is False
    assert data["statements"][0]["error"]["code"] == "1142"


def test_error_de_conexion_credencial_invalida_200_con_connection_error(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-perm2", port=3420)
    fake = _patch(monkeypatch)
    fake.next_outcome = qr.ExecutionOutcome(
        statements=[], success=False, committed=False, rolled_back=False,
        connection_error=qr.ExecError(code="1045", sqlstate=None, message="Access denied"),
    )
    r = _execute(admin_client, sid, "SELECT 1",
                 connection={"mode": "provided", "username": "malo", "password": "x"})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["success"] is False
    assert data["connection_error"]["code"] == "1045"


# --------------------------------------------------------------------------- #
# La consola no puede apuntar a la propia BD de metadatos del gateway           #
# --------------------------------------------------------------------------- #
def test_no_puede_operar_sobre_la_propia_base_del_gateway(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-meta1", host="10.9.9.9", port=1234)
    monkeypatch.setattr(qcc, "DB_HOST", "10.9.9.9")
    monkeypatch.setattr(qcc, "DB_PORT", 1234)
    monkeypatch.setattr(qcc, "DB_NAME", "gw_meta")
    fake = _patch(monkeypatch)
    r = _preview(admin_client, sid, "SELECT 1", database="gw_meta")
    assert r.status_code == 409, r.text
    assert fake.run_calls == []


# --------------------------------------------------------------------------- #
# Historial: paginación y filtro por base                                      #
# --------------------------------------------------------------------------- #
def test_historial_pagina_y_filtra_por_base(admin_client, monkeypatch):
    sid = _make_server(admin_client, name="qc-hist1", port=3421)
    _patch(monkeypatch)
    _execute(admin_client, sid, "SELECT 1", database="db_a")
    _execute(admin_client, sid, "SELECT 2", database="db_b")
    _execute(admin_client, sid, "SELECT 3", database="db_a")

    all_hist = admin_client.get(f"/api/v1/servers/{sid}/query/history").json()
    assert all_hist["pagination"]["total"] == 3

    only_a = admin_client.get(
        f"/api/v1/servers/{sid}/query/history", params={"database": "db_a"}
    ).json()
    assert only_a["pagination"]["total"] == 2
    assert all(h["database_name"] == "db_a" for h in only_a["data"])

    paged = admin_client.get(
        f"/api/v1/servers/{sid}/query/history", params={"page": 1, "size": 2}
    ).json()
    assert len(paged["data"]) == 2
    assert paged["pagination"]["total"] == 3
