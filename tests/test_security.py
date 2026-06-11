"""Hashing de contraseñas con Argon2."""

from app.utils.security import hash_password, verify_password


def test_hash_is_not_plaintext_and_verifies():
    h = hash_password("my-password")
    assert h != "my-password"
    assert h.startswith("$argon2")
    assert verify_password("my-password", h) is True


def test_verify_rejects_wrong_password():
    h = hash_password("correct")
    assert verify_password("wrong", h) is False


def test_verify_handles_garbage_hash():
    assert verify_password("x", "not-a-hash") is False
