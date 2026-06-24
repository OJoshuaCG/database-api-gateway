"""
Tests de la rotación de cifrado (envelope DEK).

Verifica que `POST /admin/crypto/rotate` re-cifra las credenciales con una DEK nueva
SIN cambiar SECRET_KEY, que el texto cifrado cambia pero sigue descifrando al original,
y que requiere admin.
"""


def _create_server(admin_client, port, password):
    return admin_client.post(
        "/api/v1/servers",
        json={
            "name": f"srv-{port}",
            "host": "127.0.0.1",
            "port": port,
            "engine": "mysql",
            "root_username": "root",
            "root_password": password,
        },
    ).json()["data"]["id"]


def _stored_cipher(server_id):
    from app.core.database import Database
    from app.models.server import Server

    session = Database().get_declarative_base_session()
    try:
        return session.get(Server, server_id).root_password_encrypted
    finally:
        session.close()


def test_rotate_requires_admin(client):
    assert client.post("/api/v1/admin/crypto/rotate").status_code in (401, 403)


def test_rotate_reencrypts_and_keeps_decryptable(admin_client):
    from app.core import crypto

    sid = _create_server(admin_client, 3700, "orig-pass-123")
    before = _stored_cipher(sid)
    assert crypto.decrypt(before) == "orig-pass-123"  # cifrado con la DEK actual

    resp = admin_client.post("/api/v1/admin/crypto/rotate")
    assert resp.status_code == 200, resp.text
    assert resp.json()["data"]["servers_reencrypted"] >= 1

    after = _stored_cipher(sid)
    assert after != before                       # el ciphertext cambió (DEK nueva)
    assert crypto.decrypt(after) == "orig-pass-123"  # sigue descifrando al original


def test_rotate_activates_single_dek_row(admin_client):
    from app.core.database import Database
    from app.models.crypto_key import CryptoKey

    _create_server(admin_client, 3701, "p1")
    admin_client.post("/api/v1/admin/crypto/rotate")
    admin_client.post("/api/v1/admin/crypto/rotate")  # segunda rotación

    session = Database().get_declarative_base_session()
    try:
        active = session.query(CryptoKey).filter(CryptoKey.is_active.is_(True)).count()
        total = session.query(CryptoKey).count()
    finally:
        session.close()
    assert active == 1            # solo UNA DEK activa
    assert total >= 2             # se conservan las anteriores (inactivas)
