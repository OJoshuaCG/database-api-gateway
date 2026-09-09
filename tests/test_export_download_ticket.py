"""
Los dos GET de exportación que MUTAN, y por qué cada uno se arregla distinto.

EL DEFECTO
----------
``GET /database-exports/{id}/download`` y ``GET /database-exports/{id}/content`` **consumen y
borran el artefacto** (``EXPORT_SINGLE_USE_DOWNLOAD``, vía ``finish_delivery``). Y
``same_site="lax"`` **sí** manda la cookie en una navegación GET de primer nivel. O sea que un
``<img src=".../download">`` en cualquier página que el admin abriera:

1. destruía el artefacto del cliente (denegación + pérdida forense), y
2. dejaba la entrega registrada contra ese admin — que es peor que el borrado, porque el rastro
   es el único control que le queda al módulo de exportación.

Un token CSRF **no** lo arregla: un ``<img>`` no puede mandar un header y a una navegación de
primer nivel tampoco se le puede pedir.

DOS ARREGLOS DISTINTOS, Y NO ES INCONSISTENCIA
----------------------------------------------
La diferencia la impone **cómo llama el cliente**, no el gusto:

- ``/download`` se abre como navegación (``<a download>``, ``window.open``): no puede mandar
  headers, así que va un **ticket en la query**, emitido por un POST que sí está cubierto por
  CSRF.
- ``/content`` lo pide la SPA con ``fetch`` porque necesita el cuerpo para el portapapeles: sí
  puede mandar el header, así que se le **exige el token CSRF aunque sea GET**.
"""

import pytest

from tests.test_api_database_exports import (
    _execute,
    _install,
    _install_execution,
    _ready,
    _server,
)


@pytest.fixture()
def job_listo(admin_client, monkeypatch):
    """Un job ejecutado, con artefacto en disco y listo para entregar."""
    _install(monkeypatch)
    _install_execution(monkeypatch, chunks=["-- x\n", "CREATE TABLE t (id int);\n"])
    sid = _server(admin_client, 3999)
    job, token = _ready(admin_client, sid)
    assert _execute(admin_client, job, token).status_code == 200
    return job


def _ticket(client, job) -> str:
    r = client.post(f"/api/v1/database-exports/{job}/download-ticket")
    assert r.status_code == 200, r.text
    return r.json()["data"]["ticket"]


# --------------------------------------------------------------------------- #
# /download: el ticket                                                       #
# --------------------------------------------------------------------------- #


def test_the_download_refuses_without_a_ticket(admin_client, job_listo):
    """
    **El test que fija el arreglo.** Con sesión y capacidad válidas, sin ticket no se entrega
    nada — que es exactamente lo que le pasa al ``<img>`` malicioso.
    """
    r = admin_client.get(f"/api/v1/database-exports/{job_listo}/download")
    assert r.status_code == 422, r.text


def test_the_artifact_survives_a_request_without_a_ticket(admin_client, job_listo):
    """
    Lo que de verdad importaba no era el 422: era que el artefacto **siga estando**. El daño del
    ataque era el borrado, así que se verifica que después del intento la descarga legítima
    todavía funciona.
    """
    admin_client.get(f"/api/v1/database-exports/{job_listo}/download")

    r = admin_client.get(
        f"/api/v1/database-exports/{job_listo}/download?ticket={_ticket(admin_client, job_listo)}"
    )
    assert r.status_code == 200, "el intento sin ticket consumió el artefacto"


def test_a_request_without_a_ticket_leaves_no_disclosure_trail(admin_client, job_listo):
    """
    El ticket se valida **ANTES** de ``prepare_download``, que audita fail-closed la intención
    de divulgar. Si el orden fuera al revés, cada ``<img>`` malicioso escribiría una fila de
    "INTENT descargar" — o sea que un atacante podría escribir en el registro de auditoría del
    gateway a voluntad.
    """
    from app.core.database import Database
    from app.models.audit_log import AuditLog

    def intentos() -> int:
        s = Database().get_declarative_base_session()
        try:
            return (
                s.query(AuditLog)
                .filter(AuditLog.action == "database_export.download")
                .count()
            )
        finally:
            s.close()

    antes = intentos()
    admin_client.get(f"/api/v1/database-exports/{job_listo}/download")
    assert intentos() == antes, "un request sin ticket dejó rastro de intento de descarga"


def test_a_ticket_for_another_job_does_not_work(admin_client, monkeypatch, job_listo):
    """Está atado al job: el ticket de uno no descarga el otro."""
    _install_execution(monkeypatch, chunks=["-- y\n"])
    sid = _server(admin_client, 3998)
    otro, token = _ready(admin_client, sid)
    assert _execute(admin_client, otro, token).status_code == 200

    ajeno = _ticket(admin_client, otro)
    r = admin_client.get(f"/api/v1/database-exports/{job_listo}/download?ticket={ajeno}")
    assert r.status_code == 422, r.text


def test_a_tampered_ticket_does_not_work(admin_client, job_listo):
    t = _ticket(admin_client, job_listo)
    exp, mac = t.split(".", 1)
    alterado = f"{exp}.{'0' * len(mac)}"
    r = admin_client.get(f"/api/v1/database-exports/{job_listo}/download?ticket={alterado}")
    assert r.status_code == 422


def test_an_expired_ticket_is_410(admin_client, job_listo):
    """
    410 y no 422: "el ticket venció, pedí otro" es una acción distinta de "ese ticket no
    corresponde". El TTL es de 60 s porque el ticket viaja en la query y entra en los logs del
    proxy — la ventana tiene que ser del tamaño de un click.
    """
    import app.services.confirm_token as ct

    real = ct.issue

    def vencido(*a, **kw):
        kw["ttl_seconds"] = -1
        return real(*a, **kw)

    ct.issue = vencido
    try:
        t = _ticket(admin_client, job_listo)
    finally:
        ct.issue = real

    r = admin_client.get(f"/api/v1/database-exports/{job_listo}/download?ticket={t}")
    assert r.status_code == 410, r.text


def test_issuing_a_ticket_runs_the_same_guards(admin_client, monkeypatch):
    """
    El POST corre los MISMOS guards que la descarga, así que falla acá y no emite un ticket que
    el GET va a rechazar. Un ticket para una descarga imposible manda al cliente a un segundo
    request que va a fallar por otro motivo.
    """
    _install(monkeypatch)
    sid = _server(admin_client, 3997)
    job, _ = _ready(admin_client, sid)  # sin ejecutar: no hay artefacto

    r = admin_client.post(f"/api/v1/database-exports/{job}/download-ticket")
    assert r.status_code == 409, r.text


# --------------------------------------------------------------------------- #
# /content: el header, aunque sea GET                                        #
# --------------------------------------------------------------------------- #


def test_content_refuses_without_the_csrf_header(client, admin_client, job_listo):
    """
    ``/content`` también consume el artefacto, así que también es un GET que muta. Acá el
    arreglo es exigirle el header —la SPA lo pide con ``fetch``, así que puede mandarlo— en vez
    de un ticket.
    """
    from app.core.csrf import CSRF_HEADER

    # Un header vacío es lo que ve el servidor cuando el cliente no lo manda: `httpx` no
    # permite quitar un header por defecto en una llamada puntual, y el guard trata vacío y
    # ausente igual a propósito.
    r = admin_client.get(
        f"/api/v1/database-exports/{job_listo}/content",
        headers={CSRF_HEADER: ""},
    )
    assert r.status_code == 403, r.text
    assert r.json()["detail"]["public_context"]["code"] == "auth.csrf_missing"


def test_content_works_with_the_header(admin_client, job_listo):
    r = admin_client.get(f"/api/v1/database-exports/{job_listo}/content")
    assert r.status_code == 200, r.text
    assert "CREATE TABLE" in r.text
