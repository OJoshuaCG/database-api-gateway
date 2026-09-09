"""
Autorización: ``require(capability)`` y los alias que las rutas importan.

DÓNDE VIVE EL CHEQUEO, Y POR QUÉ ACÁ
------------------------------------
Es una **dependencia por endpoint, declarada como PARÁMETRO**. Las tres alternativas se
descartaron con motivo:

- **Middleware con tabla path → capacidad: NO.** Sería una segunda tabla de routing que diverge
  de la real, y su modo de fallo por default es **permitir** lo que no matchea. Y hay una razón
  medida: ``/database-models`` lo sirven CUATRO archivos de rutas y ``/servers`` TRES, así que
  un mapeo por prefijo mezclaría "leer un blueprint" con "convertir la collation de N bases".
  Además el repo ya se quemó con middlewares y sub-apps montadas: el docstring de
  ``PathScopedCORSMiddleware`` documenta que el middleware externo corre **antes** que el
  routing que resuelve el mount, así que no sabe qué ruta se va a resolver.
- **Decorador: NO.** No participa del grafo de dependencias de FastAPI: no aparece en OpenAPI,
  no es enumerable para construir ``/auth/me``, y **un decorador no aplicado es invisible**. Es
  la misma falla de disciplina de hoy con otra sintaxis.
- **Solo en el controller: NO como capa primaria.** Los controllers se llaman desde rutas,
  desde workers y desde el dispatch del MCP. Pero es el único lugar donde el objeto resuelto
  —y por tanto su entorno— se conoce: de ahí la capa 2, que es fase 3.

PARÁMETRO Y NO ``dependencies=[...]``
-------------------------------------
Por una razón medida: **227 sitios** necesitan el objeto en mano para auditar. Con
``dependencies=[]`` habría que inyectar *además* un alias sin capacidad — y ese alias sin
capacidad es precisamente el atajo que no hay que tener.

EL MARCADOR ``__gw_capability__``
---------------------------------
``require()`` estampa la capacidad en el callable que devuelve. Python permite atributos en
funciones, así que el marcador **viaja con la dependencia** sin tocar el modelo de FastAPI — y
eso es lo que hace ENUMERABLE la cobertura: un script puede recorrer ``route.dependant`` y
exigir que toda ruta declare una capacidad del catálogo. Sin el marcador, "ningún endpoint
quedó sin proteger" no sería una propiedad verificable.

LO QUE ESTA CAPA **NO** HACE TODAVÍA
------------------------------------
No exige el step-up de las capacidades que lo piden (``CapabilitySpec.requires_step_up``). El
mecanismo es la fase 5 y no existe: enforzar la presencia de un token que nadie emite sería un
guard inerte, que es justo lo que este repo prohíbe. Cuando exista, se enchufa acá —``require``
ya tiene el spec a mano— y el binding al actor va en el ``verify`` del controller, con el
destino en la mano. Las dos mitades, ninguna opcional.

Ver ``docs/plans/13-usuarios-y-autorizacion-del-gateway.md`` §6.
"""

from __future__ import annotations

from typing import Annotated, Callable

from fastapi import Depends, Request

from app.core.actor import Actor, admin_actor
from app.exceptions import AppHttpException
from app.models.user_model import UserModel
from app.services.capability_catalog import (
    CODE_FORBIDDEN,
    Capability,
    GatewayRole,
    GlobalCapability,
)


def get_current_actor(request: Request) -> Actor:
    """
    Resuelve la sesión a un ``Actor``. Identidad y capacidades; **cero autorización**.

    El rol y las capacidades se releen de la BD en cada request. No es por confidencialidad
    (la cookie está firmada): es que **un rol en la cookie es un rol que no se puede
    revocar**, porque el ``SessionMiddleware`` re-firma en cada respuesta y una sesión activa
    no expira nunca. Degradar a alguien no surtiría efecto jamás.

    Un rol o una capacidad global que el código no conoce se IGNORA en vez de romper: el
    catálogo resuelve un rol desconocido al conjunto vacío, y acá una global desconocida se
    descarta. Fail-closed en el lector — el camino de autenticación no puede caerse por una
    fila legada, y tampoco puede resolver a un default permisivo.
    """
    from app.core.auth import authenticated_user

    user = authenticated_user(request)
    ctx = UserModel().find_access_context(user["id"])

    try:
        base = GatewayRole(ctx["role"])
    except ValueError:
        base = GatewayRole.VIEWER

    overrides: dict[int, GatewayRole] = {}
    for scope_id, role in (ctx["overrides"] or {}).items():
        try:
            overrides[int(scope_id)] = GatewayRole(role)
        except (TypeError, ValueError):
            continue

    globals_: set[GlobalCapability] = set()
    for name in ctx["globals"] or []:
        try:
            globals_.add(GlobalCapability(name))
        except ValueError:
            continue

    return admin_actor(
        user_id=user["id"],
        username=user["username"],
        role=base,
        overrides=overrides,
        globals_=frozenset(globals_),
    )


def assert_capability(actor: Actor, capability: Capability) -> None:
    """
    Exige una capacidad sobre un ``Actor`` ya resuelto. Levanta 403 si no la tiene.

    Existe además de ``require`` porque **hay exigencias que no se pueden declarar en la firma
    del endpoint**: dependen del payload. Dos rutas persisten DATOS DE NEGOCIO solo si el
    cliente lo pide (``from-snapshot`` con ``data_tables``, y una migración con
    ``capture_selects``), y una ruta declara UNA capacidad —el punto 1 del §6.3— así que el
    piso va en la firma y el extra va acá.

    El 403 usa un código CERRADO (``access.forbidden``) y **no nombra la capacidad que falta**:
    un mensaje como "falta servers.admin" le da a un atacante un mapa de la superficie por
    fuerza bruta de 403. El criterio es el mismo que el de nunca volcar ``str(exc)`` del motor,
    aplicado a nombres de capacidad.
    """
    if not actor.has(capability):
        raise AppHttpException(
            message="No tienes permiso para esta operación.",
            status_code=403,
            public_context={"code": CODE_FORBIDDEN},
        )


def require(capability: Capability) -> Callable[[Request], Actor]:
    """
    Fábrica de la dependencia que exige una capacidad. Devuelve el ``Actor`` resuelto.

    Delega el veredicto en ``assert_capability`` para que la forma del 403 —y sobre todo la
    decisión de no nombrar la capacidad faltante— viva en UN solo lugar.
    """

    def _dependency(request: Request) -> Actor:
        actor = get_current_actor(request)
        assert_capability(actor, capability)
        return actor

    # El marcador que hace enumerable la cobertura. Ver el docstring del módulo.
    _dependency.__gw_capability__ = capability.value  # type: ignore[attr-defined]
    _dependency.__name__ = f"require_{capability.value.replace('.', '_')}"
    return _dependency


def declared_capability(dependency: Callable) -> str | None:
    """
    La capacidad que declara una dependencia, o ``None``.

    Existe acá y no en el script de cobertura para que **el productor y el lector del marcador
    vivan juntos**: si mañana el marcador cambia de nombre o de forma, no hay un segundo lugar
    que quede desincronizado en silencio.
    """
    return getattr(dependency, "__gw_capability__", None)


# --------------------------------------------------------------------------- #
# Alias públicos — lo ÚNICO que la capa de rutas importa                       #
# --------------------------------------------------------------------------- #
#
# Un alias por capacidad, y ninguno "sin capacidad": un `ActorDep` genérico sería el atajo por
# el que un endpoint nuevo quedaría autenticado pero no autorizado.

SelfRead = Annotated[Actor, Depends(require(Capability.SELF_READ))]

ServersRead = Annotated[Actor, Depends(require(Capability.SERVERS_READ))]
ServersAdmin = Annotated[Actor, Depends(require(Capability.SERVERS_ADMIN))]

EngineUsersRead = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_READ))]
EngineUsersWrite = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_WRITE))]
EngineUsersDrop = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_DROP))]
EngineUsersSecrets = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_SECRETS))]

DatabasesRead = Annotated[Actor, Depends(require(Capability.DATABASES_READ))]
DatabasesWrite = Annotated[Actor, Depends(require(Capability.DATABASES_WRITE))]
DatabasesDrop = Annotated[Actor, Depends(require(Capability.DATABASES_DROP))]

BlueprintsRead = Annotated[Actor, Depends(require(Capability.BLUEPRINTS_READ))]
BlueprintsWrite = Annotated[Actor, Depends(require(Capability.BLUEPRINTS_WRITE))]
BlueprintsApply = Annotated[Actor, Depends(require(Capability.BLUEPRINTS_APPLY))]
BlueprintsCaptures = Annotated[Actor, Depends(require(Capability.BLUEPRINTS_CAPTURES))]

SchemaDiffRead = Annotated[Actor, Depends(require(Capability.SCHEMA_DIFF_READ))]
SchemaDiffExecute = Annotated[Actor, Depends(require(Capability.SCHEMA_DIFF_EXECUTE))]

ClonesRead = Annotated[Actor, Depends(require(Capability.CLONES_READ))]
ClonesExecute = Annotated[Actor, Depends(require(Capability.CLONES_EXECUTE))]

CollationRead = Annotated[Actor, Depends(require(Capability.COLLATION_READ))]
CollationExecute = Annotated[Actor, Depends(require(Capability.COLLATION_EXECUTE))]

ExportsRead = Annotated[Actor, Depends(require(Capability.EXPORTS_READ))]
ExportsExecute = Annotated[Actor, Depends(require(Capability.EXPORTS_EXECUTE))]
ExportsDownload = Annotated[Actor, Depends(require(Capability.EXPORTS_DOWNLOAD))]

SqlConsoleHistory = Annotated[Actor, Depends(require(Capability.SQL_CONSOLE_HISTORY))]
SqlConsoleExecute = Annotated[Actor, Depends(require(Capability.SQL_CONSOLE_EXECUTE))]

CatalogsRead = Annotated[Actor, Depends(require(Capability.CATALOGS_READ))]
CatalogsWrite = Annotated[Actor, Depends(require(Capability.CATALOGS_WRITE))]

EnvironmentsRead = Annotated[Actor, Depends(require(Capability.ENVIRONMENTS_READ))]

GatewayAdmin = Annotated[Actor, Depends(require(Capability.GATEWAY_ADMIN))]
