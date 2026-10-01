"""
Resolvedores de destino de la capa 2: declaran QUÉ se toca, y recién después se resuelve DÓNDE.

DOS MITADES, Y POR QUÉ ESTÁN SEPARADAS
--------------------------------------
Cada tipo de destino tiene un **resolvedor puro** y una **función de puntos**:

- El resolvedor es una dependencia de FastAPI que devuelve un ``ScopeTarget`` (tipo + parámetros)
  **sin tocar la BD**. FastAPI ejecuta las sub-dependencias ANTES que la dependencia padre, o
  sea antes de autenticar y de verificar el CSRF; una consulta ahí sería un oráculo de
  existencia ("este id existe / no existe") para quien ni siquiera tiene sesión.
- La función de puntos (``_RESOLVERS``) consulta la BD y devuelve los ``ScopePoint`` donde se
  evalúa la capacidad. ``require_at`` la invoca después de autenticar, y solo si el actor tiene
  grants por alcance.

Un destino inexistente resuelve al entorno MÁS PROTEGIDO, no a un 404: para un actor con
restricciones, "no existe" y "no podés" tienen que ser indistinguibles.

``TARGET_KINDS`` mapea cada resolvedor a su tipo y es el vocabulario cerrado que el script de
cobertura valida: un resolvedor que no esté registrado falla al importar la ruta.
"""

from __future__ import annotations

from typing import Callable

from app.core.scope import (
    ScopePoint,
    ScopeTarget,
    _session,
    most_protected_environment_id,
    resolve_environment_id,
)


# --------------------------------------------------------------------------- #
# Resolvedores puros (dependencias de FastAPI): cero BD                        #
# --------------------------------------------------------------------------- #


def database(db_id: int) -> ScopeTarget:
    """Una BD del inventario, por el ``db_id`` de la ruta."""
    return ScopeTarget("database", (db_id,))


def server(server_id: int) -> ScopeTarget:
    """Un servidor entero: el entorno más protegido entre sus BDs inventariadas."""
    return ScopeTarget("server", (server_id,))


def server_database(server_id: int, database: str) -> ScopeTarget:
    """
    Una BD de un servidor por nombre: la fila del inventario si existe, si no la regla del
    servidor. Cubre las rutas que operan por referencia cruda del motor.
    """
    return ScopeTarget("server_database", (server_id, database))


#: resolvedor → tipo. Es lo que ``require_at`` consulta para estampar ``__gw_scope__``.
TARGET_KINDS: dict[Callable[..., ScopeTarget], str] = {
    database: "database",
    server: "server",
    server_database: "server_database",
}


# --------------------------------------------------------------------------- #
# Funciones de puntos: consultan la BD, solo después de autenticar             #
# --------------------------------------------------------------------------- #


def _points_database(params: tuple) -> list[ScopePoint]:
    from app.models.managed_database import ManagedDatabase

    (db_id,) = params
    session = _session()
    try:
        bd = session.get(ManagedDatabase, db_id)
        server_id = bd.server_id if bd else None
    finally:
        session.close()
    env_id = resolve_environment_id(server_id=None, managed_database_id=db_id)
    return [ScopePoint(environment_id=env_id, server_id=server_id, item_id=db_id)]


def _points_server(params: tuple) -> list[ScopePoint]:
    (server_id,) = params
    env_id = resolve_environment_id(server_id=server_id, managed_database_id=None)
    return [ScopePoint(environment_id=env_id, server_id=server_id)]


def _points_server_database(params: tuple) -> list[ScopePoint]:
    from app.models.managed_database import ManagedDatabase

    server_id, nombre = params
    session = _session()
    try:
        filas = (
            session.query(ManagedDatabase.id)
            .filter(
                ManagedDatabase.server_id == server_id, ManagedDatabase.name == nombre
            )
            .all()
        )
    finally:
        session.close()
    if not filas:
        # Sin fila de inventario: regla de servidor (F-17, solo BDs inventariadas).
        return _points_server((server_id,))
    return [
        ScopePoint(
            environment_id=resolve_environment_id(
                server_id=None, managed_database_id=f[0]
            ),
            server_id=server_id,
            item_id=f[0],
        )
        for f in filas
    ]


#: tipo → función de puntos. Sus claves son el vocabulario cerrado de tipos de destino.
_RESOLVERS: dict[str, Callable[[tuple], list[ScopePoint]]] = {
    "database": _points_database,
    "server": _points_server,
    "server_database": _points_server_database,
}


def resolve_target_points(target: ScopeTarget) -> list[ScopePoint]:
    """Despacha por tipo. Un tipo desconocido es fail-closed: el entorno más protegido."""
    fn = _RESOLVERS.get(target.kind)
    if fn is None:
        return [ScopePoint(environment_id=most_protected_environment_id(), server_id=None)]
    return fn(target.params)
