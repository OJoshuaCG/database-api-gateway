"""
Helpers de step-up para los tests, sin efectos al importarse (mismo motivo que
``csrf_helpers``: importar ``tests.conftest`` crea una segunda instancia de ese archivo).
"""

import base64
import json
from datetime import datetime, timedelta

#: Ventana de step-up "siempre abierta" para los actores que un test arma A MANO (sin sesión).
#: Esos tests prueban las capas 1 y 2; sin esto, el step-up —que va después y falla cerrado para
#: un actor sin ventana— taparía lo que miden. El step-up se prueba en ``test_step_up.py``.
OPEN_WINDOW = datetime(9999, 1, 1)


def session_sid(client) -> str:
    """El ``sid`` de la cookie de sesión del client (el payload no se verifica: es un test)."""
    cruda = client.cookies.get("gw_session")
    assert cruda, "el client no tiene cookie de sesión"
    payload_b64 = cruda.split(".")[0]
    relleno = "=" * (-len(payload_b64) % 4)
    return json.loads(base64.urlsafe_b64decode(payload_b64 + relleno))["sid"]


def expire_step_up(client) -> None:
    """
    Cierra la ventana de step-up de la sesión del client, como si hubiera pasado el TTL.

    Envejece ``step_up_at`` en la fila en vez de adelantar el reloj: así la sesión no vence por
    inactividad ni toca nada más que la ventana.
    """
    from sqlalchemy import update

    from app.core import session_store
    from app.core.step_up import STEP_UP_TTL_SECONDS
    from app.models.gateway_session import GatewaySession

    sid = session_sid(client)
    s = session_store._session()
    try:
        s.execute(
            update(GatewaySession)
            .where(GatewaySession.sid == sid)
            .values(step_up_at=session_store._utcnow() - timedelta(seconds=STEP_UP_TTL_SECONDS + 1))
        )
        s.commit()
    finally:
        s.close()
