"""Sanitización de campos sensibles para logs."""

from app.utils.dict_utils import _sanitize_dict


def test_masks_common_sensitive_keys():
    out = _sanitize_dict(
        {"username": "admin", "password": "x", "root_password": "y", "token": "z"}
    )
    assert out["username"] == "admin"
    assert out["password"] == "***"
    assert out["root_password"] == "***"
    assert out["token"] == "***"


def test_masks_recursively_in_nested_dicts_and_lists():
    out = _sanitize_dict(
        {"a": {"hashed_password": "h", "ok": 1}, "items": [{"secret": "s"}, {"v": 2}]}
    )
    assert out["a"]["hashed_password"] == "***"
    assert out["a"]["ok"] == 1
    assert out["items"][0]["secret"] == "***"
    assert out["items"][1]["v"] == 2


def test_non_dict_passthrough():
    assert _sanitize_dict("plain") == "plain"
    assert _sanitize_dict(42) == 42
