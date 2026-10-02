"""
``confirm_token.verify``: la firma se verifica ANTES que la expiración.

Con la expiración primero, ``"1.x"`` respondía 410 "expiró" sin haber probado nada, así que el
410 no significaba "tu token venció" sino "mandaste un número chico". Los flujos autenticados
(preview → execute) siguen necesitando el 410 para un token AUTÉNTICO vencido; un token forjado
es 422 sin importar su ``exp``.
"""

import pytest

from app.exceptions import AppHttpException
from app.services import confirm_token

_OP = "drop-db"


def _status(token: str, **kw) -> int:
    with pytest.raises(AppHttpException) as exc:
        confirm_token.verify(token, _OP, 1, "midb", **kw)
    return exc.value.status_code


def test_an_authentic_token_verifies():
    token, _ = confirm_token.issue(_OP, 1, "midb")
    confirm_token.verify(token, _OP, 1, "midb")


def test_an_authentic_expired_token_is_410():
    token, _ = confirm_token.issue(_OP, 1, "midb", ttl_seconds=-5)
    assert _status(token) == 410


def test_a_forged_token_with_a_past_expiry_is_422_not_410():
    """**El orden que fija F-33**: un ``exp`` vencido con firma basura no llega a ser 410."""
    assert _status("1.x") == 422
    assert _status("1." + "0" * 64) == 422


def test_an_expired_token_for_another_target_is_422():
    """Vencido Y de otra BD: lo que falla primero es que no le corresponde."""
    token, _ = confirm_token.issue(_OP, 1, "otradb", ttl_seconds=-5)
    assert _status(token) == 422
