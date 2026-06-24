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
    # Simular otra clave derivada: limpiar cache y cambiar el salt efectivo.
    import base64

    from cryptography.fernet import Fernet

    other = Fernet(base64.urlsafe_b64encode(b"x" * 32))
    monkeypatch.setattr(crypto, "_active_dek", lambda: other)
    with pytest.raises(crypto.CryptoError):
        crypto.decrypt(token)
