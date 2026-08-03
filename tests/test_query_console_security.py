"""
Invariantes de SEGURIDAD de la consola SQL que no viven en ``query_policy``.

Cada test cubre una forma concreta de que la consola mienta sobre lo que hizo:

- Un token de confirmación que sirva para un SQL distinto del que se previsualizó.
- Un engine cacheado que ejecute como pseudo-root una prueba pedida como otro usuario.
- Un error de permisos disfrazado de error de infraestructura (o al revés).
- Un valor del driver que rompa la serialización o vuelque un BLOB entero.
"""

import pytest

from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.services import confirm_token
from app.services.db_admin import query_runner as qr

_OP = "sql-console"


def _target(user: str = "root") -> ServerTarget:
    return ServerTarget(
        server_id=1,
        dialect="mysql",
        host="127.0.0.1",
        port=3306,
        admin_user=user,
        admin_password="secreto",
    )


# --------------------------------------------------------------------------- #
# Token atado al SQL y al usuario                                              #
# --------------------------------------------------------------------------- #
def test_el_token_no_sirve_para_otro_sql():
    """
    El agujero que cierra el ``subject``: sin él, ``(operación, server_id, base)`` es
    igual para CUALQUIER consulta sobre esa base, así que se podría previsualizar un
    SELECT inocuo y canjear el token para ejecutar un DROP.
    """
    token, _ = confirm_token.issue(_OP, 1, "midb", subject="hash-del-select|admin|root|")
    confirm_token.verify(token, _OP, 1, "midb", subject="hash-del-select|admin|root|")

    with pytest.raises(AppHttpException) as exc:
        confirm_token.verify(token, _OP, 1, "midb", subject="hash-del-drop|admin|root|")
    assert exc.value.status_code == 422


def test_el_token_no_sirve_para_otro_usuario_ni_otra_base():
    token, _ = confirm_token.issue(_OP, 1, "midb", subject="h|provided|lector|")
    with pytest.raises(AppHttpException):
        confirm_token.verify(token, _OP, 1, "midb", subject="h|admin|root|")
    with pytest.raises(AppHttpException):
        confirm_token.verify(token, _OP, 1, "otradb", subject="h|provided|lector|")
    with pytest.raises(AppHttpException):
        confirm_token.verify(token, _OP, 2, "midb", subject="h|provided|lector|")


def test_un_token_sin_subject_no_valida_uno_con_subject():
    """Los usos existentes (DROP DATABASE) no deben poder canjearse en la consola."""
    token, _ = confirm_token.issue("drop-db", 1, "midb")
    with pytest.raises(AppHttpException):
        confirm_token.verify(token, _OP, 1, "midb", subject="h|admin|root|")


# --------------------------------------------------------------------------- #
# El usuario forma parte de la identidad de la conexión                        #
# --------------------------------------------------------------------------- #
def test_el_cache_de_engines_separa_por_usuario():
    """
    Regresión del bug latente: la clave del cache no incluía el usuario, así que un
    ``ServerTarget`` con otra credencial devolvía el engine pseudo-root ya cacheado — la
    prueba "como usuario limitado" corría en realidad como root y daba verde siempre.
    """
    from app.core import remote_engine

    root = remote_engine.get_engine(_target("root"), "midb")
    lector = remote_engine.get_engine(_target("lector"), "midb")
    try:
        assert root is not lector
        assert root.url.username == "root"
        assert lector.url.username == "lector"
    finally:
        remote_engine.invalidate_server(1)


def test_effective_target_usa_la_credencial_elegida():
    base = _target("root")

    admin = qr.effective_target(base, qr.QueryCredential(mode=qr.MODE_ADMIN, username="root"))
    assert admin.admin_user == "root"

    # ``impersonate`` conecta como pseudo-root a propósito: el cambio de identidad lo
    # hace el ``SET ROLE`` posterior, no la credencial.
    imp = qr.effective_target(
        base,
        qr.QueryCredential(mode=qr.MODE_IMPERSONATE, username="reportes", impersonate_role="reportes"),
    )
    assert imp.admin_user == "root"

    provided = qr.effective_target(
        base, qr.QueryCredential(mode=qr.MODE_PROVIDED, username="lector", password="p")
    )
    assert provided.admin_user == "lector"
    assert provided.admin_password == "p"
    # El target original es inmutable: no se contamina la credencial del servidor.
    assert base.admin_user == "root"


# --------------------------------------------------------------------------- #
# Un error de permisos es un RESULTADO, no un fallo de infraestructura         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "code,sqlstate",
    [
        (1045, None),      # MySQL: access denied for user
        (1044, None),      # MySQL: access denied for database
        (1049, None),      # MySQL: unknown database
        (None, "28P01"),   # PostgreSQL: invalid_password
        (None, "42501"),   # PostgreSQL: insufficient_privilege
        (None, "42704"),   # PostgreSQL: undefined_object (rol inexistente)
    ],
)
def test_errores_de_credencial_se_reportan_como_resultado(code, sqlstate):
    err = qr.ExecError(code=str(code) if code else sqlstate, sqlstate=sqlstate, message="x")
    assert qr._is_auth_like(err)


@pytest.mark.parametrize("code", ["2003", "2013", "08006"])
def test_fallos_de_infraestructura_no_se_confunden_con_permisos(code):
    assert not qr._is_auth_like(qr.ExecError(code=code, sqlstate=None, message="x"))


# --------------------------------------------------------------------------- #
# Solo lectura: la garantía la da el motor                                     #
# --------------------------------------------------------------------------- #
def test_cada_motor_tiene_su_sentencia_de_solo_lectura():
    assert qr._READ_ONLY_SQL["mysql"] == "START TRANSACTION READ ONLY"
    assert qr._READ_ONLY_SQL["mariadb"] == "START TRANSACTION READ ONLY"
    assert qr._READ_ONLY_SQL["postgresql"] == "SET TRANSACTION READ ONLY"


# --------------------------------------------------------------------------- #
# Serialización de valores                                                     #
# --------------------------------------------------------------------------- #
def test_normalizacion_de_valores_del_driver():
    from datetime import date, datetime
    from decimal import Decimal
    from uuid import UUID

    v = lambda x: qr._json_value(x, 100)  # noqa: E731

    assert v(None) is None
    assert v(True) is True
    assert v(42) == 42
    # Decimal a str: un float perdería la precisión que se está inspeccionando.
    assert v(Decimal("10.50")) == "10.50"
    assert v(datetime(2026, 8, 2, 10, 30)) == "2026-08-02T10:30:00"
    assert v(date(2026, 8, 2)) == "2026-08-02"
    assert v(UUID("12345678-1234-5678-1234-567812345678")).startswith("12345678")
    assert v(b"\x00\xff") == "0x00ff"
    # El tipo SET de MySQL llega como set de Python.
    assert v({"b", "a"}) == ["a", "b"]


def test_las_celdas_grandes_se_recortan():
    largo = qr._json_value("x" * 5000, 100)
    assert largo.startswith("x" * 100)
    assert "5000 caracteres" in largo

    blob = qr._json_value(b"\xab" * 5000, 100)
    assert "5000 bytes" in blob


def test_el_porcentaje_literal_se_escapa_antes_de_llegar_al_driver():
    """
    ``exec_driver_sql`` llega al DBAPI con params distilados a ``()``, así que pymysql y
    psycopg parsean placeholders y un ``%`` literal reventaría antes de tocar el motor.
    """
    assert qr._escape_percent("SELECT * FROM t WHERE x LIKE '%a%'") == (
        "SELECT * FROM t WHERE x LIKE '%%a%%'"
    )
