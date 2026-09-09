"""
Catálogo de CAPACIDADES del gateway — vocabulario cerrado de autorización.

DOS PLANOS QUE NO SE PUEDEN MEZCLAR
-----------------------------------
Este módulo es el plano de CONTROL: qué puede hacer un usuario (o un token) **del gateway**.
El plano GESTIONADO —qué puede hacer un usuario **del motor**— vive en
``app/services/privilege_catalog.py`` y en ``app/models/{privilege,permission_profile}.py``,
y usa otras palabras a propósito: ``Privilege``, ``PermissionProfile``, ``grant``.

Es la misma clase de trampa que las "dos cosas llamadas environment" que documenta
``CLAUDE.md``, con un agravante: PostgreSQL llama **roles** a los usuarios del motor, así que
la colisión no es solo del repo, es del vocabulario del dominio.

**Regla de escritura: nunca "permisos" a secas.** Es *capacidad del gateway* o *privilegio del
motor*. Acá: ``Capability`` y ``GatewayRole``. Allá: ``Privilege`` y ``PermissionProfile``.

POR QUÉ EN CÓDIGO Y NO EN UNA TABLA
-----------------------------------
El mapeo rol → capacidades es código, no dato. El argumento decisivo lo da el propio repo:
``privilege_catalog.py`` siembra su catálogo con ``except Exception: logger.exception(...)`` y
**el arranque sigue**. Eso es correcto para un catálogo informativo y es inaceptable para uno
de autorización: si el seed falla a medias hay dos finales y los dos son malos — si la ausencia
de fila deniega, es una caída total sin error visible en el arranque; si permite, es un agujero
silencioso. **Un ``Mapping`` en un archivo no puede driftear.**

Lo que se pierde, declarado: **un rol nuevo requiere deploy.** Con tres roles y alcance por
entorno el caso real entra ("operador en desarrollo, lector en producción"); el día que haga
falta un cuarto, es una entrada en ``ROLE_CAPABILITIES`` revisada en un PR — que para política
de autorización es *mejor* que un formulario. La costura para cambiar de opinión es ese
``Mapping``: se cambia el proveedor sin tocar ``has()``.

LOS DOS EJES SON INDEPENDIENTES
-------------------------------
``mutates`` y ``discloses`` **no** son la misma escala. ``reveal-password``, la descarga de un
export y la rotación de crypto **no destruyen nada** — así que un modelo partido en
destructivo/no-destructivo los deja pasar completos, y ``operator`` en producción incluiría en
silencio exportar la base entera del cliente en claro y leer las contraseñas de su motor.

De ahí la consecuencia de forma: **las capacidades que divulgan NO participan del orden
acumulativo del módulo.** ``engine_users.secrets`` no implica ``engine_users.write``, porque
leer una credencial y reescribir los privilegios del motor son riesgos incomparables; y
``exports.download`` no implica ``exports.execute``, porque quien solo tiene que bajar un
artefacto no necesita poder generarlos. Se otorgan sueltas.

DE DÓNDE SALEN LOS NIVELES
--------------------------
No se inventaron. El repo ya clasificó el riesgo de cada endpoint **dos veces de forma
independiente**: en los escalones de ``@limiter.limit`` (3/10/20/30/60) y en qué endpoint exige
``confirm_token``. Los niveles solo le ponen nombre a eso.

Y el criterio que impide que el catálogo crezca a una capacidad por endpoint: **una capacidad
nace cuando dos roles necesitan diferir en ella.** Si los tres roles coinciden sobre un
conjunto de endpoints, ese conjunto es UNA capacidad.

Ver ``docs/plans/13-usuarios-y-autorizacion-del-gateway.md`` §4 y §5.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Literal, Mapping

# --------------------------------------------------------------------------- #
# Roles                                                                        #
# --------------------------------------------------------------------------- #


class GatewayRole(StrEnum):
    """
    Los tres roles CON ALCANCE, en cadena totalmente ordenada.

    El orden importa: ``union_role``/``effective_role`` toman el MÁXIMO sobre los alcances de
    un actor, y "máximo" solo está definido si la cadena es monótona
    (``viewer ⊆ operator ⊆ owner``). El invariante 2 de abajo lo afirma al importar.

    Las capacidades ORTOGONALES (``access_admin``, ``security_officer``) **no** son valores de
    este enum: no son comparables con ninguno de los tres, así que meterlas acá rompería la
    monotonía y con ella el ``max``. Viven en ``GlobalCapability``.
    """

    VIEWER = "viewer"
    OPERATOR = "operator"
    OWNER = "owner"


_ROLE_RANK: Mapping[GatewayRole, int] = MappingProxyType(
    {GatewayRole.VIEWER: 0, GatewayRole.OPERATOR: 1, GatewayRole.OWNER: 2}
)


class GlobalCapability(StrEnum):
    """
    Capacidades globales y ortogonales a la cadena de roles.

    ``ACCESS_ADMIN`` administra usuarios, roles, grants y tokens, y es **explícitamente NO
    operativo**: no aplica migraciones, no dropea, no revela contraseñas. Esa separación es la
    mitad de la separación de deberes — sin ella, el único rol que puede operar en producción
    es también el que puede desarmar la política, y en un equipo de tres todos terminan ahí.

    ``SECURITY_OFFICER`` escribe **datos de política**: los flags de ``Environment``, los
    catálogos que deciden qué llega al motor, y el host y la credencial de un ``Server``.
    Regla general que lo justifica: **toda fila que un guard lee es una frontera de
    privilegio**, así que su escritor necesita al menos el privilegio del guard que puede
    apagar.
    """

    ACCESS_ADMIN = "access_admin"
    SECURITY_OFFICER = "security_officer"


# --------------------------------------------------------------------------- #
# Capacidades                                                                  #
# --------------------------------------------------------------------------- #


class Capability(StrEnum):
    """
    Vocabulario cerrado. El valor se declara EXPLÍCITO, no se deriva del nombre del miembro:
    ``ENGINE_USERS_WRITE.name.lower().replace("_", ".")`` daría ``engine.users.write``, que es
    incorrecto. Derivar es el tipo de astucia que rompe en el sexto miembro.

    Formato ``modulo.accion`` con PUNTO: es seguro en URL, query string, clave JSON y CSV
    —relevante porque los scopes de un token de API viajan como string separado por comas— y
    el punto ya es el separador de los vocabularios jerárquicos del repo
    (``public_context["code"]``).
    """

    # -- Propio del actor (los tres roles lo tienen) ------------------------ #
    SELF_READ = "self.read"

    # -- Inventario de servidores (plano de control) ------------------------ #
    SERVERS_READ = "servers.read"
    SERVERS_ADMIN = "servers.admin"

    # -- Usuarios del MOTOR ------------------------------------------------- #
    ENGINE_USERS_READ = "engine_users.read"
    ENGINE_USERS_WRITE = "engine_users.write"
    ENGINE_USERS_DROP = "engine_users.drop"
    ENGINE_USERS_SECRETS = "engine_users.secrets"

    # -- Bases de datos ----------------------------------------------------- #
    DATABASES_READ = "databases.read"
    DATABASES_WRITE = "databases.write"
    DATABASES_DROP = "databases.drop"

    # -- Blueprints y sus versiones ----------------------------------------- #
    BLUEPRINTS_READ = "blueprints.read"
    BLUEPRINTS_WRITE = "blueprints.write"
    BLUEPRINTS_APPLY = "blueprints.apply"
    BLUEPRINTS_CAPTURES = "blueprints.captures"

    # -- Comparación de esquemas -------------------------------------------- #
    SCHEMA_DIFF_READ = "schema_diff.read"
    SCHEMA_DIFF_EXECUTE = "schema_diff.execute"

    # -- Clonado (incluye los lotes por blueprint) -------------------------- #
    CLONES_READ = "clones.read"
    CLONES_EXECUTE = "clones.execute"

    # -- Conversión de collation -------------------------------------------- #
    COLLATION_READ = "collation.read"
    COLLATION_EXECUTE = "collation.execute"

    # -- Exportación -------------------------------------------------------- #
    EXPORTS_READ = "exports.read"
    EXPORTS_EXECUTE = "exports.execute"
    EXPORTS_DOWNLOAD = "exports.download"

    # -- Consola SQL -------------------------------------------------------- #
    SQL_CONSOLE_HISTORY = "sql_console.history"
    SQL_CONSOLE_EXECUTE = "sql_console.execute"

    # -- Catálogos del motor y de charsets ---------------------------------- #
    CATALOGS_READ = "catalogs.read"
    CATALOGS_WRITE = "catalogs.write"

    # -- Entornos ----------------------------------------------------------- #
    ENVIRONMENTS_READ = "environments.read"

    # -- Administración del propio gateway ---------------------------------- #
    GATEWAY_ADMIN = "gateway.admin"


ScopeAxis = Literal["global", "environment", "server"]


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """
    Metadatos de una capacidad. ``label`` va en español porque lo consume la SPA.

    ``mutates`` y ``discloses`` son INDEPENDIENTES (ver el docstring del módulo).
    ``requires_step_up`` es atributo de la CAPACIDAD y no del endpoint: así la SPA puede pedir
    la contraseña *antes* de mandar la operación en vez de descubrirlo por un error.
    ``agent_allowed`` es el techo de lo que puede vivir en los scopes de un token de API.
    """

    id: Capability
    module: str
    level: str
    label: str
    mutates: bool
    discloses: bool
    requires_step_up: bool
    agent_allowed: bool
    scope_axis: ScopeAxis


def _spec(
    cap: Capability,
    label: str,
    *,
    mutates: bool = False,
    discloses: bool = False,
    step_up: bool = False,
    agent: bool = False,
    axis: ScopeAxis = "environment",
) -> CapabilitySpec:
    module, level = cap.value.split(".", 1)
    return CapabilitySpec(
        id=cap,
        module=module,
        level=level,
        label=label,
        mutates=mutates,
        discloses=discloses,
        requires_step_up=step_up,
        agent_allowed=agent,
        scope_axis=axis,
    )


CAPABILITIES: tuple[CapabilitySpec, ...] = (
    _spec(Capability.SELF_READ, "Ver su propia identidad y capacidades", axis="global"),
    # `servers` no tiene nivel intermedio A PROPÓSITO: `read` ya expone host, puerto y usuario
    # pseudo-root de todo el parque —o sea reconocimiento de la infraestructura del cliente— y
    # `admin` es la llave maestra, porque editar un servidor puede RE-APUNTAR un server_id a
    # un host que el editor controla. Un nivel entre esos dos solo daría falsa gradualidad.
    _spec(Capability.SERVERS_READ, "Ver el inventario de servidores", axis="server"),
    _spec(
        Capability.SERVERS_ADMIN,
        "Registrar, editar y dar de baja servidores",
        mutates=True,
        step_up=True,
        axis="global",
    ),
    _spec(Capability.ENGINE_USERS_READ, "Ver los usuarios del motor", axis="server"),
    _spec(
        Capability.ENGINE_USERS_WRITE,
        "Crear, editar y otorgar privilegios a usuarios del motor",
        mutates=True,
        axis="server",
    ),
    # `secrets` NO implica `write`: rotar una contraseña es rutina, LEERLA en claro es
    # divulgación y quien opera no la necesita. Son riesgos incomparables.
    # Existe por SIMETRÍA con `databases.drop`, y porque su ausencia contradecía el criterio
    # que `operator` declara en su propio comentario ("no incluye `*.drop`"): sin este nivel,
    # DROP USER caía en `engine_users.write` y un operator podía dejar sin acceso a la
    # aplicación de un tercero, mientras borrar la BD que ese usuario posee pedía `owner`.
    _spec(
        Capability.ENGINE_USERS_DROP,
        "Borrar usuarios del motor",
        mutates=True,
        step_up=True,
        axis="server",
    ),
    _spec(
        Capability.ENGINE_USERS_SECRETS,
        "Revelar contraseñas de usuarios del motor",
        discloses=True,
        step_up=True,
        axis="server",
    ),
    _spec(Capability.DATABASES_READ, "Ver bases y su estructura", agent=True),
    _spec(Capability.DATABASES_WRITE, "Crear y editar bases gestionadas", mutates=True),
    _spec(
        Capability.DATABASES_DROP,
        "Borrar bases de datos",
        mutates=True,
        step_up=True,
    ),
    _spec(Capability.BLUEPRINTS_READ, "Ver blueprints y sus versiones", agent=True),
    _spec(Capability.BLUEPRINTS_WRITE, "Crear y editar versiones de blueprint", mutates=True),
    # `write` ≠ `apply`: autor de la migración y ejecutor sobre producción son dos personas.
    # Es la separación que el TODO.md del repo ya pide por escrito.
    _spec(
        Capability.BLUEPRINTS_APPLY,
        "Aplicar y revertir versiones sobre bases reales",
        mutates=True,
        step_up=True,
    ),
    # Las capturas son DATOS DE NEGOCIO de la base del tercero: divulgación, no lectura.
    _spec(
        Capability.BLUEPRINTS_CAPTURES,
        "Leer los resultados de SELECT capturados en una migración",
        discloses=True,
        step_up=True,
    ),
    _spec(Capability.SCHEMA_DIFF_READ, "Comparar esquemas y ver el diff", agent=True),
    _spec(
        Capability.SCHEMA_DIFF_EXECUTE,
        "Adoptar o ejecutar el DDL de una comparación",
        mutates=True,
        step_up=True,
    ),
    _spec(Capability.CLONES_READ, "Ver planes de clonado"),
    _spec(
        Capability.CLONES_EXECUTE,
        "Ejecutar un clonado de estructura y datos",
        mutates=True,
        discloses=True,
        step_up=True,
    ),
    _spec(Capability.COLLATION_READ, "Ver planes de conversión de collation"),
    _spec(
        Capability.COLLATION_EXECUTE,
        "Ejecutar una conversión de collation",
        mutates=True,
        step_up=True,
    ),
    _spec(Capability.EXPORTS_READ, "Ver planes de exportación y su estado"),
    _spec(Capability.EXPORTS_EXECUTE, "Generar el artefacto de una exportación", mutates=True),
    # `download` NO implica `execute`: planear y generar no divulga nada mientras el artefacto
    # no se entregue. `_guard_owner` ya reconoce esa frontera en el código.
    _spec(
        Capability.EXPORTS_DOWNLOAD,
        "Descargar los datos exportados en claro",
        discloses=True,
        step_up=True,
    ),
    _spec(Capability.SQL_CONSOLE_HISTORY, "Ver el historial de la consola SQL"),
    _spec(
        Capability.SQL_CONSOLE_EXECUTE,
        "Ejecutar SQL ad-hoc contra un motor",
        mutates=True,
        discloses=True,
        step_up=True,
    ),
    _spec(Capability.CATALOGS_READ, "Ver los catálogos de privilegios y charsets", axis="global"),
    # Dato de política: `privileges.is_active` decide qué se puede otorgar y
    # `permission_profiles` es la plantilla de GRANTs. Solo `security_officer`.
    _spec(
        Capability.CATALOGS_WRITE,
        "Editar los catálogos de privilegios, perfiles y charsets",
        mutates=True,
        step_up=True,
        axis="global",
    ),
    _spec(Capability.ENVIRONMENTS_READ, "Ver los entornos y su política", axis="global"),
    _spec(
        Capability.GATEWAY_ADMIN,
        "Administrar el gateway: entornos, crypto, usuarios y tokens",
        mutates=True,
        step_up=True,
        axis="global",
    ),
)

_BY_ID: Mapping[Capability, CapabilitySpec] = MappingProxyType(
    {s.id: s for s in CAPABILITIES}
)

# --------------------------------------------------------------------------- #
# Rol → capacidades                                                            #
# --------------------------------------------------------------------------- #

_VIEWER: frozenset[Capability] = frozenset(
    {
        Capability.SELF_READ,
        Capability.SERVERS_READ,
        Capability.ENGINE_USERS_READ,
        Capability.DATABASES_READ,
        Capability.BLUEPRINTS_READ,
        Capability.SCHEMA_DIFF_READ,
        Capability.CLONES_READ,
        Capability.COLLATION_READ,
        Capability.EXPORTS_READ,
        Capability.SQL_CONSOLE_HISTORY,
        Capability.CATALOGS_READ,
        Capability.ENVIRONMENTS_READ,
    }
)

# `operator` acumula sobre `viewer` la escritura NO destructiva y NO divulgante. No incluye
# `*.drop`, `blueprints.apply`, `sql_console.execute` ni ninguna capacidad que divulgue.
_OPERATOR: frozenset[Capability] = _VIEWER | {
    Capability.ENGINE_USERS_WRITE,
    Capability.DATABASES_WRITE,
    Capability.BLUEPRINTS_WRITE,
    Capability.EXPORTS_EXECUTE,
    Capability.COLLATION_EXECUTE,
}

# `owner` es todo lo OPERATIVO del alcance. NO incluye `servers.admin`, `catalogs.write` ni
# `gateway.admin`: esas tres son datos de política o la llave del inventario, y van en
# `security_officer` / `access_admin`. Que el rol operativo pudiera apagar la barrera de
# producción es exactamente el agujero que la separación existe para cerrar.
# `clones.execute` está acá y NO en `operator` aunque su nombre lo emparente con los otros
# `*.execute`: un clon copia DATOS, y meter la base de producción de un cliente en un entorno
# de desarrollo es divulgación —los ponía a la par el nombre del nivel, no el riesgo—. Lo
# encontró `test_disclosing_capabilities_are_never_implied_by_a_mutating_one`, que es
# exactamente para lo que ese test existe.
_OWNER: frozenset[Capability] = _OPERATOR | {
    Capability.CLONES_EXECUTE,
    Capability.ENGINE_USERS_DROP,
    Capability.ENGINE_USERS_SECRETS,
    Capability.DATABASES_DROP,
    Capability.BLUEPRINTS_APPLY,
    Capability.BLUEPRINTS_CAPTURES,
    Capability.SCHEMA_DIFF_EXECUTE,
    Capability.EXPORTS_DOWNLOAD,
    Capability.SQL_CONSOLE_EXECUTE,
}

ROLE_CAPABILITIES: Mapping[GatewayRole, frozenset[Capability]] = MappingProxyType(
    {
        GatewayRole.VIEWER: _VIEWER,
        GatewayRole.OPERATOR: _OPERATOR,
        GatewayRole.OWNER: _OWNER,
    }
)

GLOBAL_CAPABILITIES: Mapping[GlobalCapability, frozenset[Capability]] = MappingProxyType(
    {
        GlobalCapability.ACCESS_ADMIN: frozenset({Capability.GATEWAY_ADMIN}),
        GlobalCapability.SECURITY_OFFICER: frozenset(
            {Capability.SERVERS_ADMIN, Capability.CATALOGS_WRITE, Capability.GATEWAY_ADMIN}
        ),
    }
)

#: Techo de lo que puede vivir en los scopes de un token de API (plan 12).
AGENT_ALLOWED: frozenset[Capability] = frozenset(
    s.id for s in CAPABILITIES if s.agent_allowed
)

# --------------------------------------------------------------------------- #
# Códigos de error — vocabulario cerrado, va en public_context["code"]         #
# --------------------------------------------------------------------------- #

CODE_FORBIDDEN = "access.forbidden"
CODE_STEP_UP_REQUIRED = "access.step_up_required"
CODE_NOT_VISIBLE = "access.not_visible"
CODE_UNDECLARED_ROUTE = "access.undeclared_route"

# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #


def spec(capability: Capability) -> CapabilitySpec:
    return _BY_ID[capability]


def role_capabilities(role: GatewayRole) -> frozenset[Capability]:
    """
    Capacidades de un rol. Un rol DESCONOCIDO devuelve el conjunto vacío, no una excepción.

    Fail-closed en el lector: una fila con un rol que el código no conoce (rollback de un
    deploy que agregó un rol, ``UPDATE`` manual, dato legado) no puede tumbar el camino de
    autenticación ni, peor, resolver a un default permisivo.
    """
    try:
        return ROLE_CAPABILITIES[GatewayRole(role)]
    except (KeyError, ValueError):
        return frozenset()


def union_role(base: GatewayRole, overrides: Mapping[int, GatewayRole]) -> GatewayRole:
    """
    El MÁXIMO sobre ``{base} ∪ overrides``. Responde "¿podría, en algún alcance?".

    El máximo y no el mínimo es deliberado: con el mínimo, "lector en producción" degradaría
    también el trabajo en desarrollo, que es justo el caso de uso. La contrapartida —esta capa
    es más laxa que la política real— solo es aceptable porque la capa 2 (el alcance por
    destino) no es salteable.
    """
    candidates = [GatewayRole(base), *(GatewayRole(r) for r in overrides.values())]
    return max(candidates, key=lambda r: _ROLE_RANK[r])


def parse_scopes(raw: str) -> frozenset[Capability]:
    """
    Scopes de un token de API → capacidades, INTERSECTADOS con el techo de agente.

    La intersección no es defensiva por gusto: una fila de ``api_tokens`` manipulada o legada
    nunca puede otorgar una capacidad fuera del techo, **incluso si el string lo dice**.
    Fail-closed en el lector, no solo en el escritor. Un scope desconocido se ignora.
    """
    out: set[Capability] = set()
    for token in (raw or "").split(","):
        token = token.strip()
        if not token:
            continue
        try:
            out.add(Capability(token))
        except ValueError:
            continue
    return frozenset(out) & AGENT_ALLOWED


def capability_matrix() -> list[dict]:
    """
    El catálogo, para publicarlo. **La misma estructura que hace cumplir ``has()``.**

    Publicar una promesa que el servidor no cumple es peor que no publicarla, así que esto no
    es una lista paralela: se deriva de ``CAPABILITIES`` y de ``ROLE_CAPABILITIES``.
    """
    return [
        {
            "id": s.id.value,
            "module": s.module,
            "level": s.level,
            "label": s.label,
            "mutates": s.mutates,
            "discloses": s.discloses,
            "requires_step_up": s.requires_step_up,
            "agent_allowed": s.agent_allowed,
            "scope_axis": s.scope_axis,
            "roles": sorted(
                r.value for r, caps in ROLE_CAPABILITIES.items() if s.id in caps
            ),
            "global_capabilities": sorted(
                g.value for g, caps in GLOBAL_CAPABILITIES.items() if s.id in caps
            ),
        }
        for s in CAPABILITIES
    ]


# --------------------------------------------------------------------------- #
# Invariantes — se afirman AL IMPORTAR                                         #
# --------------------------------------------------------------------------- #
#
# Al importar y no en un test: fallar al importar es fallar al arrancar, y para un catálogo de
# autorización eso es lo correcto. Un test alguien puede no correrlo; el proceso no puede no
# importar el módulo del que depende cada request.


def _assert_invariants() -> None:
    # 1. Cada Capability tiene exactamente un spec.
    if len(_BY_ID) != len(Capability):
        faltan = {c.value for c in Capability} - {c.value for c in _BY_ID}
        raise AssertionError(f"Capacidades sin CapabilitySpec: {sorted(faltan)}")
    if len(CAPABILITIES) != len(_BY_ID):
        raise AssertionError("Hay CapabilitySpec duplicados para la misma Capability.")

    # 2. Monotonía. Es lo que hace bien definido el `max` de `union_role`: sin esto, "rol
    #    máximo" no significa nada.
    if not (
        ROLE_CAPABILITIES[GatewayRole.VIEWER]
        <= ROLE_CAPABILITIES[GatewayRole.OPERATOR]
        <= ROLE_CAPABILITIES[GatewayRole.OWNER]
    ):
        raise AssertionError("Los roles no son monótonos: viewer ⊆ operator ⊆ owner.")

    # 3. `viewer` no muta NI divulga. Las dos, no una: la divulgación es el eje que un modelo
    #    destructivo/no-destructivo pierde entero.
    for cap in ROLE_CAPABILITIES[GatewayRole.VIEWER]:
        s = _BY_ID[cap]
        if s.mutates or s.discloses:
            raise AssertionError(f"'viewer' tiene una capacidad que muta o divulga: {cap.value}")

    # 4. Toda capacidad que divulga exige step-up. Un factor fresco antes de que un dato del
    #    cliente salga del perímetro.
    for s in CAPABILITIES:
        if s.discloses and not s.requires_step_up:
            raise AssertionError(f"{s.id.value} divulga y no exige step-up.")

    # 5. El techo de agente. Un token nunca puede mutar ni divulgar.
    for s in CAPABILITIES:
        if s.agent_allowed and (s.mutates or s.discloses):
            raise AssertionError(f"{s.id.value} es agent_allowed y muta o divulga.")

    # 6. El id concuerda con módulo.nivel. Evita que un valor y su spec se desincronicen.
    for s in CAPABILITIES:
        if s.id.value != f"{s.module}.{s.level}":
            raise AssertionError(f"{s.id.value} no concuerda con {s.module}.{s.level}")

    # 7. Ninguna capacidad global está en la cadena de roles, y viceversa. Si `gateway.admin`
    #    cayera en `owner`, el rol operativo podría apagar la barrera de producción.
    globales = set().union(*GLOBAL_CAPABILITIES.values())
    for cap in globales:
        for role, caps in ROLE_CAPABILITIES.items():
            if cap in caps:
                raise AssertionError(
                    f"{cap.value} es global y además está en el rol '{role.value}'."
                )


_assert_invariants()
