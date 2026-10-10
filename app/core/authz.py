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

EL STEP-UP ES EL ÚLTIMO ESLABÓN
-------------------------------
Las capacidades con ``CapabilitySpec.requires_step_up`` exigen además una contraseña fresca
(``app/core/step_up.py``). Va DESPUÉS de las capas 1 y 2 en ``require``/``require_at``, en
``assert_capability`` (por default) y en ``assert_at``/``assert_at_point``: nadie tipea su
contraseña para enterarse después de que igual no podía.

Ver ``docs/plans/13-usuarios-y-autorizacion-del-gateway.md`` §6.
"""

from __future__ import annotations

from typing import Annotated, Callable

from fastapi import Depends, Request

from app.core import csrf
from app.core.actor import Actor, admin_actor
from app.core.capability_resolution import parse_access_context
from app.core.denial_audit import record_denial
from app.core.scope import ScopeTarget, assert_layer2
from app.core.step_up import assert_step_up, window_until
from app.exceptions import AppHttpException
from app.models.user_model import UserModel
from app.services.capability_catalog import CODE_FORBIDDEN, CODE_SOD_CONFLICT, Capability


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
    from app.core.auth import authenticated_session

    user, sesion = authenticated_session(request)
    ctx = UserModel().find_access_context(user["id"])
    parsed = parse_access_context(ctx)
    actor = _actor_from_parsed(
        user["id"], user["username"], parsed, step_up_until=window_until(sesion.step_up_at)
    )
    if parsed.sod_neutralized:
        # Solo acá y no en `actor_from_access_context`: la vista de acceso efectivo también lo
        # usa, y ahí el actor de la denegación sería la persona MIRADA, no quien mira.
        record_denial(
            CODE_SOD_CONFLICT,
            actor=actor,
            capability=None,
            check="sod",
            extra={"rules": list(parsed.sod_neutralized)},
        )
    return actor


def actor_from_access_context(
    user_id: int, username: str, ctx: dict, *, step_up_until=None
) -> Actor:
    """
    ``find_access_context`` → ``Actor``. Extraída de ``get_current_actor`` para que la vista de
    acceso efectivo acuñe el actor con EXACTAMENTE el mismo código que la autorización real.

    La lectura es tolerante y vive en ``parse_access_context`` (un solo lugar): un rol, una global
    o un alcance desconocidos se descartan, y una capacidad puntual desconocida o no otorgable
    también. Fail-closed en el lector — el camino de autenticación no puede caerse por una fila
    legada, y tampoco puede resolver a un default permisivo.
    """
    return _actor_from_parsed(
        user_id, username, parse_access_context(ctx), step_up_until=step_up_until
    )


def _actor_from_parsed(user_id: int, username: str, parsed, *, step_up_until=None) -> Actor:
    return admin_actor(
        user_id=user_id,
        username=username,
        role=parsed.base,
        grants=list(parsed.scope_roles),
        globals_=parsed.globals_,
        capability_grants=[(c, st, sid) for c, st, sid, _ in parsed.capability_grants],
        step_up_until=step_up_until,
    )


def assert_capability(actor: Actor, capability: Capability, *, step_up: bool = True) -> None:
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

    ``step_up=True`` (default) exige además la ventana de step-up si la capacidad la pide: es lo
    que cubre sin tocarlas las exigencias por payload de los handlers. Solo ``_authenticate`` lo
    apaga, porque en las dependencias el step-up va DESPUÉS de la capa 2.
    """
    if not actor.has(capability):
        # El rastro (agregado, best-effort) lleva la capacidad; la respuesta no. Ver
        # ``app.core.denial_audit``.
        record_denial(CODE_FORBIDDEN, actor=actor, capability=capability, check="capability")
        raise AppHttpException(
            message="No tienes permiso para esta operación.",
            status_code=403,
            public_context={"code": CODE_FORBIDDEN},
        )
    if step_up:
        assert_step_up(actor, capability)


CODE_LAST_ADMIN = "access.last_admin_protected"


def assert_not_last_access_admin(user_id: int, *, action: str) -> None:
    """
    Impide dejar al gateway **sin ningún administrador de accesos activo**.

    EL ESCENARIO QUE CUBRE, Y NO ES HIPOTÉTICO
    ------------------------------------------
    Dos administradores. A le revoca el ``access_admin`` a B "para ordenar", y después se
    desactiva a sí mismo por error. Resultado sin este guard: **bloqueo total**, y la única
    salida es SQL a mano contra la BD de metadatos — hecho en plena incidencia, por alguien con
    credenciales pseudo-root y **sin ninguna auditoría**.

    Este invariante elimina la mayoría de los caminos al bloqueo *antes* de que haga falta un
    mecanismo de recuperación, que es mucho mejor que tener un buen mecanismo de recuperación.

    POR QUÉ ``access_admin`` Y NO EL ROL ``owner``
    ----------------------------------------------
    ``owner`` es el rol OPERATIVO y explícitamente no administra accesos. Quedarse sin ningún
    ``owner`` es un problema de operación —molesto, reparable por quien administra accesos—;
    quedarse sin ningún ``access_admin`` es **no poder repararlo**.

    ``action`` va al mensaje para que el 409 diga qué se estaba intentando: "no se puede
    desactivar" y "no se puede revocar" mandan a la persona a lugares distintos.

    ESTE CHEQUEO ES UN PRE-CHEQUEO, NO EL CANDADO
    ---------------------------------------------
    Cuenta fuera de la transacción de la escritura, así que por sí solo es check-then-write.
    Los caminos que escriben (``UserModel.replace_access(last_admin_action=…)`` y
    ``UserModel.deactivate_guarded``) repiten el invariante DENTRO de su transacción con las
    filas bloqueadas; ése es el que vale bajo concurrencia.
    """
    from app.models.user_model import UserModel

    if UserModel().count_active_access_admins(exclude_user_id=user_id) > 0:
        return
    raise last_access_admin_error(action)


def last_access_admin_error(action: str) -> AppHttpException:
    """El 409 ``access.last_admin_protected``: UNA forma, la usen el pre-chequeo o el candado."""
    return AppHttpException(
        message=(
            f"No se puede {action}: es el último administrador de accesos activo. "
            "Otorgale 'access_admin' a otro usuario activo primero."
        ),
        status_code=409,
        public_context={"code": CODE_LAST_ADMIN},
    )


def _identify(request: Request) -> Actor:
    """Autenticación + CSRF, sin capacidad. ``require_at`` elige la capacidad recién después."""
    actor = get_current_actor(request)
    # CSRF ANTES de la capacidad, y solo para el actor de tipo `admin` (el que se autentica
    # con una cookie que el navegador adjunta solo). Antes de la capacidad porque un
    # cross-site detectado no debería recibir un 403 distinto según si tiene o no la
    # capacidad: eso sería un oráculo sobre la superficie a través de un origen ajeno.
    if actor.kind == "admin":
        from app.core.auth import SESSION_SID

        csrf.enforce(request, request.session.get(SESSION_SID) or "", actor=actor)
    return actor


def _authenticate(request: Request, capability: Capability) -> Actor:
    """
    Autenticación + CSRF + capa 1. Es el tramo común de ``require`` y ``require_at``: vive en un
    solo lugar para que las dos fábricas no puedan divergir en el orden ni en la forma del 403.
    Sin step-up: cada fábrica lo exige al final, después de su capa 2.
    """
    actor = _identify(request)
    assert_capability(actor, capability, step_up=False)
    return actor


def _mark_step_up_exempt(dependency: Callable) -> None:
    """
    Estampa ``__gw_step_up_exempt__``: la ruta que usa esta dependencia NO pide step-up.

    Es un marcador y no un silencio para que la excepción sea ENUMERABLE: el chequeo 7 de
    ``scripts/check_route_capabilities.py`` exige que toda ruta marcada esté en
    ``STEP_UP_EXEMPT`` (con su motivo) y que sea una cancelación. Un ``step_up=False`` nuevo
    sin entrada en esa lista rompe CI en vez de abrir un hueco callado.
    """
    dependency.__gw_step_up_exempt__ = True  # type: ignore[attr-defined]


def require(capability: Capability, *, step_up: bool = True) -> Callable[[Request], Actor]:
    """
    Fábrica de la dependencia que exige una capacidad. Devuelve el ``Actor`` resuelto.

    Delega el veredicto en ``assert_capability`` para que la forma del 403 —y sobre todo la
    decisión de no nombrar la capacidad faltante— viva en UN solo lugar.

    ``step_up=False`` exime a la ruta del step-up (las capas de capacidad siguen valiendo).
    Existe SOLO para las cancelaciones: frenar una operación destructiva nunca puede costar más
    que lanzarla. Ver ``_mark_step_up_exempt``.
    """

    def _dependency(request: Request) -> Actor:
        actor = _authenticate(request, capability)
        if step_up:
            assert_step_up(actor, capability, method=request.method)
        return actor

    # El marcador que hace enumerable la cobertura. Ver el docstring del módulo.
    _dependency.__gw_capability__ = capability.value  # type: ignore[attr-defined]
    _dependency.__name__ = f"require_{capability.value.replace('.', '_')}"
    if not step_up:
        _mark_step_up_exempt(_dependency)
    return _dependency


def require_either(
    capability: Capability, alternative: Capability
) -> Callable[[Request], Actor]:
    """
    Fábrica de la dependencia que acepta UNA de dos capacidades. Devuelve el ``Actor`` resuelto.

    Existe para ``/api-tokens``: ``access.admin`` administra los tokens de todos y ``tokens.own``
    solo los propios, sobre LAS MISMAS rutas. Duplicar los endpoints dejaba dos implementaciones
    del techo del emisor, del step-up y de la auditoría que divergen en silencio (es el motivo de
    ``_validate_scopes`` único); aceptar las dos capacidades en el guard y acotar por dueño en el
    controller deja una sola. ``require`` y ``require_at`` no sirven: exigen UNA capacidad.

    Esta dependencia **solo autoriza la capa 1**. Quién ve qué fila NO lo decide ella: la ruta
    tiene que pasarle al controller el dueño al que se acota a quien no tiene ``capability``
    (``api_token_controller.owner_scope_of``). Usarla sin ese filtro convertiría a ``alternative``
    en ``capability`` para todos.

    Step-up: se evalúa SIEMPRE con la spec de ``capability`` (``access.admin`` exige step-up en todo
    método no seguro), no con la de ``alternative``. Si dependiera de la capacidad que el actor
    casualmente tiene, quien solo tiene ``tokens.own`` emitiría y revocaría tokens sin contraseña
    fresca, y el camino de autoservicio sería más laxo que el administrativo.

    Marcadores: ``__gw_capability__`` lleva ``capability`` (la estricta, el piso que ven los
    chequeos del script) y ``__gw_alternatives__`` lleva ``alternative``, para que la cobertura
    siga siendo enumerable y ``alternative`` no se lea como vocabulario muerto.
    """

    def _dependency(request: Request) -> Actor:
        actor = _identify(request)
        holds_alternative = actor.has(alternative)
        if not holds_alternative:
            # El 403 y su rastro salen de ``assert_capability`` (una sola forma), con la capacidad
            # estricta: la respuesta no nombra ninguna.
            assert_capability(actor, capability, step_up=False)
        assert_step_up(actor, capability, method=request.method)
        return actor

    _dependency.__gw_capability__ = capability.value  # type: ignore[attr-defined]
    _dependency.__gw_alternatives__ = (alternative.value,)  # type: ignore[attr-defined]
    _dependency.__name__ = (
        f"require_{capability.value.replace('.', '_')}_or_{alternative.value.replace('.', '_')}"
    )
    return _dependency


def require_at(
    capability: Capability,
    *,
    target: Callable[..., ScopeTarget],
    capability_for: Callable[[ScopeTarget], Capability] | None = None,
    step_up: bool = True,
) -> Callable[..., Actor]:
    """
    Como ``require`` pero con capa 2: exige la capacidad EN el destino que declara ``target``.

    Devuelve el mismo ``Actor`` (no un wrapper): 227 sitios de controller lo reciben como
    ``admin=actor`` y no tienen que enterarse. ``target`` es un resolvedor puro de
    ``app.core.scope_targets``; FastAPI lo ejecuta antes que esta dependencia, así que no puede
    consultar la BD (oráculo previo a la autenticación). Acá, ya autenticado, se resuelve a
    entornos y solo si el actor tiene grants por alcance: para los demás es un no-op sin BD.

    Estampa ``__gw_capability__`` (los chequeos 1-4 del script y el trinquete siguen valiendo) y
    ``__gw_scope__`` (el tipo de destino, que exige el chequeo 6). Un resolvedor no registrado
    en ``TARGET_KINDS`` falla con ``KeyError`` al importar la ruta, no en runtime.

    ``capability_for``: elige la capacidad de las capas 1 y 2 según el destino ya leído del
    payload. Se invoca DESPUÉS de autenticar (y del CSRF), así que puede consultar la fila sin
    ser un oráculo para quien no tiene sesión. ``capability`` sigue siendo la que se estampa en
    ``__gw_capability__`` —el piso que ven los chequeos 1-4 y 6—, y ``capability_for`` solo puede
    devolver otra capacidad del catálogo. Lo usa el PATCH de inventario, donde reclasificar es
    ``environments.write`` y cualquier otro campo es ``databases.write``.

    ``step_up=False``: igual que en ``require``, solo para cancelaciones (capas 1 y 2 intactas).
    """
    from app.core.scope_targets import TARGET_KINDS

    kind = TARGET_KINDS[target]

    def _dependency(request: Request, t: ScopeTarget = Depends(target)) -> Actor:
        if capability_for is None:
            exigida = capability
            actor = _authenticate(request, exigida)
        else:
            actor = _identify(request)
            exigida = capability_for(t)
            assert_capability(actor, exigida, step_up=False)
        assert_layer2(actor, exigida, t)
        if step_up:
            assert_step_up(actor, exigida, method=request.method)
        return actor

    _dependency.__gw_capability__ = capability.value  # type: ignore[attr-defined]
    _dependency.__gw_scope__ = kind  # type: ignore[attr-defined]
    _dependency.__name__ = f"require_at_{capability.value.replace('.', '_')}_{kind}"
    if not step_up:
        _mark_step_up_exempt(_dependency)
    return _dependency


def declared_scope(dependency: Callable) -> str | None:
    """El tipo de destino que declara una dependencia (``require_at``), o ``None``."""
    return getattr(dependency, "__gw_scope__", None)


def declared_step_up_exempt(dependency: Callable) -> bool:
    """¿La dependencia exime del step-up (``step_up=False``)? Ver ``_mark_step_up_exempt``."""
    return bool(getattr(dependency, "__gw_step_up_exempt__", False))


def declared_capability(dependency: Callable) -> str | None:
    """
    La capacidad que declara una dependencia, o ``None``.

    Existe acá y no en el script de cobertura para que **el productor y el lector del marcador
    vivan juntos**: si mañana el marcador cambia de nombre o de forma, no hay un segundo lugar
    que quede desincronizado en silencio.
    """
    return getattr(dependency, "__gw_capability__", None)


def declared_alternatives(dependency: Callable) -> tuple[str, ...]:
    """
    Las capacidades ALTERNATIVAS que acepta una dependencia (``require_either``), o ``()``.

    Vive junto a ``declared_capability`` por el mismo motivo: productor y lector del marcador
    en un solo lugar.
    """
    return tuple(getattr(dependency, "__gw_alternatives__", ()))


# --------------------------------------------------------------------------- #
# Alias públicos — lo ÚNICO que la capa de rutas importa                       #
# --------------------------------------------------------------------------- #
#
# Un alias por capacidad, y ninguno "sin capacidad": un `ActorDep` genérico sería el atajo por
# el que un endpoint nuevo quedaría autenticado pero no autorizado.

SelfRead = Annotated[Actor, Depends(require(Capability.SELF_READ))]
#: Alias público de ``tokens.own`` (el catálogo exige uno por capacidad). Ninguna ruta lo usa solo:
#: ``/api-tokens`` usa ``AccessAdminOrOwnTokens``. Sin el filtro por dueño del controller, este
#: alias NO acota nada.
TokensOwn = Annotated[Actor, Depends(require(Capability.TOKENS_OWN))]

ServersRead = Annotated[Actor, Depends(require(Capability.SERVERS_READ))]
ServersAdmin = Annotated[Actor, Depends(require(Capability.SERVERS_ADMIN))]

EngineUsersRead = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_READ))]
EngineUsersWrite = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_WRITE))]
EngineUsersDrop = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_DROP))]
EngineUsersSecrets = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_SECRETS))]
EngineUsersCredentials = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_CREDENTIALS))]

#: Delegar privilegios del motor (WITH GRANT OPTION, sensibles, ``provision`` al reasignar dueño).
#: Ninguna ruta lo usa como guard: la exige el PAYLOAD (``scope.assert_at_with_code``); existe
#: porque el catálogo exige un alias público por capacidad.
EngineUsersGrantAdmin = Annotated[Actor, Depends(require(Capability.ENGINE_USERS_GRANT_ADMIN))]

DatabasesRead = Annotated[Actor, Depends(require(Capability.DATABASES_READ))]
DatabasesWrite = Annotated[Actor, Depends(require(Capability.DATABASES_WRITE))]
DatabasesDrop = Annotated[Actor, Depends(require(Capability.DATABASES_DROP))]

BlueprintsRead = Annotated[Actor, Depends(require(Capability.BLUEPRINTS_READ))]
BlueprintsWrite = Annotated[Actor, Depends(require(Capability.BLUEPRINTS_WRITE))]
BlueprintsApply = Annotated[Actor, Depends(require(Capability.BLUEPRINTS_APPLY))]
BlueprintsCaptures = Annotated[Actor, Depends(require(Capability.BLUEPRINTS_CAPTURES))]

#: Código de vistas, rutinas, triggers y eventos. Ninguna ruta lo usa como guard (el guard de esas
#: rutas es de estructura): lo decide el PAYLOAD de la respuesta (``definition_visibility``).
#: Existe porque el catálogo exige un alias público por capacidad.
SchemaDefinitions = Annotated[Actor, Depends(require(Capability.SCHEMA_DEFINITIONS))]

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
EnvironmentsWrite = Annotated[Actor, Depends(require(Capability.ENVIRONMENTS_WRITE))]

#: Datos de bases gestionadas para agentes (excepción cerrada del catálogo). Las rutas HTTP de
#: opt-in usan ``require_at(Capability.DATA_READ, target=database)`` (capa 2); estos alias existen
#: porque el catálogo exige un alias público por capacidad. ``DataQuery`` no lo usa ninguna ruta:
#: lo consume la tool ``run_select`` del MCP.
DataRead = Annotated[Actor, Depends(require(Capability.DATA_READ))]
DataQuery = Annotated[Actor, Depends(require(Capability.DATA_QUERY))]
#: Igual que ``DataQuery``: ninguna ruta HTTP lo usa. Lo consume la tool ``get_definition`` del MCP.
DataDefinitions = Annotated[Actor, Depends(require(Capability.DATA_DEFINITIONS))]
#: Igual que ``DataDefinitions``: ninguna ruta HTTP lo usa. Lo consume la tool
#: ``get_blueprint_migration`` del MCP.
DataBlueprintSql = Annotated[Actor, Depends(require(Capability.DATA_BLUEPRINT_SQL))]

#: Usuarios del gateway, accesos, capacidades puntuales, tokens y preparación de alcances. Solo
#: la global ``access_admin``.
AccessAdmin = Annotated[Actor, Depends(require(Capability.ACCESS_ADMIN_CAP))]
#: ``/api-tokens``: ``access.admin`` (todos los tokens) o ``tokens.own`` (solo los que emitió el
#: actor). Quien no tiene ``access.admin`` TIENE que ser acotado por dueño en el controller.
AccessAdminOrOwnTokens = Annotated[
    Actor, Depends(require_either(Capability.ACCESS_ADMIN_CAP, Capability.TOKENS_OWN))
]
#: Lectura de la auditoría (``GET /audit-log``). Solo la global ``security_officer``.
AuditRead = Annotated[Actor, Depends(require(Capability.AUDIT_READ))]
#: Rotación del cifrado (``POST /admin/crypto/rotate``). Solo la global ``security_officer``.
CryptoRotate = Annotated[Actor, Depends(require(Capability.CRYPTO_ROTATE))]
