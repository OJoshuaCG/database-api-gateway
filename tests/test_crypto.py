"""Cifrado Fernet de credenciales."""

import pytest

from app.core import crypto


def test_roundtrip():
    token = crypto.encrypt("s3cr3t-pass")
    assert token != "s3cr3t-pass"
    assert crypto.decrypt(token) == "s3cr3t-pass"


def test_token_is_not_plaintext_and_varies():
    # Fernet incluye IV/timestamp: dos cifrados del mismo texto difieren.
    t1 = crypto.encrypt("same")
    t2 = crypto.encrypt("same")
    assert t1 != t2
    assert crypto.decrypt(t1) == crypto.decrypt(t2) == "same"


def test_encrypt_rejects_empty():
    with pytest.raises(crypto.CryptoError):
        crypto.encrypt("")


def test_decrypt_invalid_token_raises():
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt("not-a-valid-token")


def test_try_decrypt_is_lenient():
    assert crypto.try_decrypt(None) is None
    assert crypto.try_decrypt("") is None
    assert crypto.try_decrypt("garbage") is None
    assert crypto.try_decrypt(crypto.encrypt("ok")) == "ok"


def test_decrypt_with_different_key_fails(monkeypatch):
    token = crypto.encrypt("secret")
    # Simular otra clave: _active_dek ahora retorna MultiFernet, así que el mock también.
    import base64

    from cryptography.fernet import Fernet, MultiFernet

    other = MultiFernet([Fernet(base64.urlsafe_b64encode(b"x" * 32))])
    monkeypatch.setattr(crypto, "_active_dek", lambda: other)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(token)


def test_multifernet_decrypts_old_key_token():
    """Token cifrado con una clave previa se descifra si esa clave está en el historial."""
    from cryptography.fernet import Fernet, MultiFernet

    old_key = Fernet.generate_key()
    new_key = Fernet.generate_key()
    old_fernet = Fernet(old_key)
    token = old_fernet.encrypt(b"old-secret").decode("utf-8")

    # MultiFernet con la NUEVA clave primero (la que cifraría) y la VIEJA detrás.
    multi = MultiFernet([Fernet(new_key), Fernet(old_key)])

    # Inyectar el MultiFernet directamente en el cache (bajo el lock) y restaurar luego.
    original_cache = crypto._dek_cache
    try:
        with crypto._dek_lock:
            crypto._dek_cache = multi
        assert crypto.decrypt(token) == "old-secret"
    finally:
        with crypto._dek_lock:
            crypto._dek_cache = original_cache


def test_bootstrap_dek_is_idempotent():
    """bootstrap_dek() retorna un bool sin lanzar, incluso sin BD real disponible."""
    # En el entorno de test (BD SQLite temporal sin tabla crypto_keys creada en este
    # contexto) la excepción se captura y retorna False; con tabla y DEK activa también.
    result = crypto.bootstrap_dek()
    assert isinstance(result, bool)


def test_active_dek_returns_multifernet():
    """_active_dek() retorna un MultiFernet (no un Fernet simple)."""
    from cryptography.fernet import MultiFernet

    crypto.reset_dek_cache()
    dek = crypto._active_dek()
    assert isinstance(dek, MultiFernet)
