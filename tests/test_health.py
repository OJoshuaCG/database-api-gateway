"""Liveness y readiness del gateway."""


def test_health_liveness_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_readiness_ok_when_db_reachable(client):
    """Con la BD de metadatos accesible (SQLite de test), readiness responde 200."""
    r = client.get("/health/ready")
    assert r.status_code == 200
    assert r.json()["status"] == "ready"


def test_readiness_503_when_db_unreachable(client, monkeypatch):
    """Si el SELECT 1 a la BD del gateway falla, readiness devuelve 503."""
    import app.routes.health as health

    class BoomSession:
        def execute(self, *a, **k):
            raise RuntimeError("db down")

        def close(self):
            pass

    class BoomDB:
        def get_declarative_base_session(self):
            return BoomSession()

    monkeypatch.setattr(health, "Database", lambda *a, **k: BoomDB())
    r = client.get("/health/ready")
    assert r.status_code == 503
    assert r.json()["status"] == "unavailable"


def test_health_endpoints_send_cors_header(client):
    """
    /health y /health/ready viven en el app principal, NO en una sub-app versionada, así
    que necesitan su propio CORSMiddleware (regresión: sin él, el navegador bloquea la
    lectura de la respuesta para un frontend en otro origen, p. ej. localhost:5173 en dev,
    aunque la respuesta llegue con 200).
    """
    origin = "http://localhost:5173"
    r = client.get("/health", headers={"Origin": origin})
    assert r.headers.get("access-control-allow-origin") == origin

    r = client.get("/health/ready", headers={"Origin": origin})
    assert r.headers.get("access-control-allow-origin") == origin


def test_v1_cors_preflight_not_blocked_by_main_app_cors(client):
    """
    Regresión: el CORS del app principal (agregado solo para /health) NO debe interceptar
    el preflight de las rutas de /api/v1/*. El middleware del app principal envuelve
    también las sub-apps montadas (app.mount("/api/v1", ...)) — si no queda acotado por
    path, un CORSMiddleware "global" con allow_methods=["GET"] (pensado solo para
    /health) rechaza con 400 el preflight de cualquier POST/PUT/DELETE de /api/v1, aunque
    la sub-app v1 sí lo permita (bug real reportado: preflight de POST /api/v1/auth/login
    fallando con "Disallowed CORS method").
    """
    origin = "http://localhost:5173"
    r = client.options(
        "/api/v1/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type",
        },
    )
    assert r.status_code == 200, r.text
    assert "POST" in (r.headers.get("access-control-allow-methods") or "")

    # /health, en cambio, sigue acotado a GET (no se filtró el allow_methods=["GET"]
    # hacia /api/v1, ni se amplió de más).
    r = client.options(
        "/health",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
        },
    )
    assert r.status_code == 400
