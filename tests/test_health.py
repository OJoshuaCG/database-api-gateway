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
