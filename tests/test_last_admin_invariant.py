"""
El invariante que evita el bloqueo total, y el arranque que lo repara.

EL ESCENARIO, Y NO ES HIPOTÉTICO
--------------------------------
Dos administradores. A le revoca el ``access_admin`` a B "para ordenar", y después se desactiva a
sí mismo por error. Sin el invariante: **bloqueo total**, y la única salida es SQL a mano contra la
BD de metadatos — hecho en plena incidencia, por alguien con credenciales pseudo-root y **sin
ninguna auditoría**.

POR QUÉ HAY DOS COSAS ACÁ Y NO UNA
----------------------------------
El invariante impide **llegar** al bloqueo; el re-anclaje de ``bootstrap_admin`` lo repara si se
llega igual (por SQL directo, por una migración, por un camino que todavía no existe). Y el
re-anclaje arregla un defecto propio: la versión anterior estaba keyed en el **username**, así que
si al administrador se lo renombraba o desactivaba **el seed no reparaba nada**.
"""

from sqlalchemy import text

import pytest

from app.core.auth import bootstrap_admin
from app.core.authz import assert_not_last_access_admin
from app.core.database import Database
from app.exceptions import AppHttpException
from app.models.user_model import UserModel
from app.utils.security import hash_password


def _crear_usuario(username: str, *, activo=True, access_admin=False) -> int:
    um = UserModel()
    um.create(
        {
            "username": username,
            "email": f"{username}@gateway.local",
            "hashed_password": hash_password("Secreta123"),
            "full_name": username,
            "notes": None,
            "is_active": activo,
            "gateway_role": "viewer",
        }
    )
    fila = um.find_by_username(username)
    if access_admin:
        um.grant_global_capabilities(username, ["access_admin"])
    return fila["id"]


def _hash_de(username: str) -> str:
    return UserModel().find_by_username(username)["hashed_password"]


# --------------------------------------------------------------------------- #
# El invariante                                                               #
# --------------------------------------------------------------------------- #


def test_the_last_access_admin_is_protected(client):
    """El admin sembrado es el único: sacarlo dejaría el gateway sin quién repare nada."""
    admin_id = UserModel().find_by_username("admin")["id"]
    with pytest.raises(AppHttpException) as exc:
        assert_not_last_access_admin(admin_id, action="desactivar")
    assert exc.value.status_code == 409
    assert exc.value.public_context["code"] == "access.last_admin_protected"


def test_with_a_second_admin_the_first_one_can_go(client):
    _crear_usuario("segunda", access_admin=True)
    admin_id = UserModel().find_by_username("admin")["id"]
    # No levanta.
    assert_not_last_access_admin(admin_id, action="desactivar")


def test_an_inactive_second_admin_does_not_count(client):
    """
    Contar filas de ``user_global_capabilities`` sin mirar ``is_active`` habría dejado pasar
    esto — y el resultado es un bloqueo total con un "administrador" que no puede entrar.
    """
    _crear_usuario("dormida", activo=False, access_admin=True)
    admin_id = UserModel().find_by_username("admin")["id"]
    with pytest.raises(AppHttpException):
        assert_not_last_access_admin(admin_id, action="desactivar")


def test_an_owner_without_access_admin_does_not_count(client):
    """
    ``owner`` es el rol OPERATIVO y no administra accesos. Quedarse sin ningún ``owner`` es un
    problema de operación; quedarse sin ningún ``access_admin`` es **no poder repararlo**.
    """
    otro = _crear_usuario("operativa")
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET gateway_role = 'owner' WHERE id = :i"), {"i": otro}
        )

    admin_id = UserModel().find_by_username("admin")["id"]
    with pytest.raises(AppHttpException):
        assert_not_last_access_admin(admin_id, action="desactivar")


def test_the_count_excludes_the_candidate_in_one_query(client):
    """
    La pregunta que importa es "¿quedaría alguno si saco a éste?", y se responde en UNA
    consulta. Contar y restar en Python es donde una carrera mete el error.
    """
    um = UserModel()
    admin_id = um.find_by_username("admin")["id"]
    assert um.count_active_access_admins() == 1
    assert um.count_active_access_admins(exclude_user_id=admin_id) == 0


# --------------------------------------------------------------------------- #
# El arranque anclado al invariante                                           #
# --------------------------------------------------------------------------- #


def test_bootstrap_does_nothing_when_the_invariant_holds(client):
    """
    Idempotencia, y algo más: que el administrador se llame **distinto** de `ADMIN_USERNAME` es
    una situación NORMAL, no algo que el arranque deba "corregir".
    """
    um = UserModel()
    antes = _hash_de("admin")
    with Database().engine.begin() as conn:
        conn.execute(text("UPDATE users SET username = 'renombrada' WHERE username = 'admin'"))

    bootstrap_admin()

    assert um.find_by_username("admin") is None, "el seed sembró un duplicado"
    assert um.count_active_access_admins() == 1
    assert _hash_de("renombrada") == antes, "el arranque tocó la credencial"


def test_bootstrap_repairs_a_broken_invariant(client):
    """
    El caso que la versión anclada al username **no reparaba**: el administrador existe pero
    está desactivado, así que hay cero ``access_admin`` activos.
    """
    um = UserModel()
    with Database().engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_active = 0 WHERE username = 'admin'"))
    assert um.count_active_access_admins() == 0

    bootstrap_admin()

    assert um.count_active_access_admins() == 1
    assert um.find_by_username("admin")["is_active"]


def test_the_repair_never_touches_the_password(client):
    """
    **La línea entre una reparación y un bypass de autenticación.** Si el arranque re-afirmara
    la password, desactivar a alguien sería reversible **por reinicio** — y el reinicio es la
    operación más común del mundo. El control dejaría de existir sin que nadie se dé cuenta.
    """
    original = _hash_de("admin")
    with Database().engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_active = 0 WHERE username = 'admin'"))

    bootstrap_admin()

    assert _hash_de("admin") == original, "el arranque reescribió la credencial"


def test_the_repair_never_touches_the_role_of_an_existing_admin(client):
    """
    Mismo criterio para el rol: degradar a alguien no puede deshacerse reiniciando el proceso.
    """
    with Database().engine.begin() as conn:
        conn.execute(
            text("UPDATE users SET is_active = 0, gateway_role = 'viewer' WHERE username = 'admin'")
        )

    bootstrap_admin()

    fila = UserModel().find_by_username("admin")
    assert fila["gateway_role"] == "viewer", "el arranque re-afirmó el rol"
    # Pero SÍ le devuelve las globales, que es lo que el invariante exige.
    assert UserModel().count_active_access_admins() == 1


def test_the_repair_is_audited(client):
    """
    Una reparación de privilegio hecha por el arranque es exactamente el evento que alguien
    tiene que poder ver después. Sembrar un despliegue nuevo NO se audita como recuperación:
    no lo es.
    """
    from app.models.audit_log import AuditLog

    with Database().engine.begin() as conn:
        conn.execute(text("UPDATE users SET is_active = 0 WHERE username = 'admin'"))

    bootstrap_admin()

    s = Database().get_declarative_base_session()
    try:
        filas = (
            s.query(AuditLog)
            .filter(AuditLog.action == "access.bootstrap_recovery")
            .all()
        )
        assert filas, "la recuperación no dejó rastro"
        assert "NO se modificó" in (filas[-1].detail or "")
    finally:
        s.close()
