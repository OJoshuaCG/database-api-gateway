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

    ``ACCESS_ADMIN`` (capacidad ``access.admin``) administra usuarios, roles, grants y tokens,
    y **nada más** (invariante 10). Es **explícitamente NO
    operativo**: no aplica migraciones, no dropea, no revela contraseñas. Esa separación es la
    mitad de la separación de deberes — sin ella, el único rol que puede operar en producción
    es también el que puede desarmar la política, y en un equipo de tres todos terminan ahí.

    ``SECURITY_OFFICER`` escribe **datos de política**: los flags de ``Environment``, los
    catálogos que deciden qué llega al motor, el host y la credencial de un ``Server``, y la
    rotación del cifrado, y **lee la auditoría** (las dos, ``policy.admin``). **No** administra usuarios: eso es
    ``access.admin``, y los conjuntos de las dos globales son disjuntos (invariante 9). Regla general que lo justifica: **toda fila que un guard lee es una frontera de
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
    ENGINE_USERS_CREDENTIALS = "engine_users.credentials"

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
    ENVIRONMENTS_WRITE = "environments.write"

    # -- Administración del acceso y de la política del gateway -------------- #
    # Eran UNA sola capacidad (``gateway.admin``) y la tenían las dos globales, así que
    # ``security_officer`` también administraba usuarios: la separación de deberes quedaba
    # en el papel. Partida en dos, cada global tiene la suya y los conjuntos son disjuntos
    # (invariantes 9, 10 y 12). ``gateway.admin`` NO se puede reintroducir.
    #: Usuarios del gateway, sus accesos, las capacidades puntuales, los tokens de API y el
    #: reporte de preparación de alcances. Solo ``access_admin``.
    ACCESS_ADMIN_CAP = "access.admin"
    #: Rotación del cifrado y LECTURA de la auditoría (``GET /audit-log``). Solo
    #: ``security_officer``: quien revisa el rastro no puede ser quien hace los cambios de acceso
    #: que el rastro registra (``access_admin``).
    POLICY_ADMIN = "policy.admin"


ScopeAxis = Literal["global", "environment", "server"]


@dataclass(frozen=True, slots=True)
class CapabilitySpec:
    """
    Metadatos de una capacidad. ``label`` va en español porque lo consume la SPA.

    ``mutates`` y ``discloses`` son INDEPENDIENTES (ver el docstring del módulo).
    ``requires_step_up`` es atributo de la CAPACIDAD y no del endpoint: así la SPA puede pedir
    la contraseña *antes* de mandar la operación en vez de descubrirlo por un error.
    ``agent_allowed`` es el techo de lo que puede vivir en los scopes de un token de API.

    ``destructive`` NO es un tercer eje independiente: es un SUBCONJUNTO de ``mutates``. Marca
    las que destruyen o cambian de forma irreversible datos o estructura **del tercero** (no la
    BD de metadatos del gateway). Existe para que "destructivo ⇒ solo ``owner`` y con step-up"
    sea un invariante de import y no una coincidencia de valores: sin el flag,
    ``databases.drop`` y ``databases.write`` eran indistinguibles para cualquier regla.
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
    destructive: bool = False


def _spec(
    cap: Capability,
    label: str,
    *,
    mutates: bool = False,
    discloses: bool = False,
    step_up: bool = False,
    agent: bool = False,
    axis: ScopeAxis = "environment",
    destructive: bool = False,
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
        destructive=destructive,
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
    # `secrets` NO implica `write`: reescribir privilegios es rutina, LEER una contraseña en
    # claro es divulgación y quien opera no la necesita. Son riesgos incomparables. (ELEGIRLA
    # también divulga: es `credentials`, más abajo.)
    # Existe por SIMETRÍA con `databases.drop`, y porque su ausencia contradecía el criterio
    # que `operator` declara en su propio comentario ("no incluye `*.drop`"): sin este nivel,
    # DROP USER caía en `engine_users.write` y un operator podía dejar sin acceso a la
    # aplicación de un tercero, mientras borrar la BD que ese usuario posee pedía `owner`.
    _spec(
        Capability.ENGINE_USERS_DROP,
        "Borrar usuarios del motor",
        mutates=True,
        step_up=True,
        destructive=True,
        axis="server",
    ),
    _spec(
        Capability.ENGINE_USERS_SECRETS,
        "Revelar contraseñas de usuarios del motor",
        discloses=True,
        step_up=True,
        axis="server",
    ),
    # `credentials`: el ACTOR ELIGE la contraseña de una cuenta del motor (crearla con contraseña,
    # rotarla, definir la conocida, agregar un host con contraseña nueva). Elegir la credencial
    # equivale a revelarla —quien la eligió la sabe y entra al motor por fuera del gateway, sin
    # export, consola, step-up ni auditoría—, así que `discloses=True` y por eso solo `owner`,
    # con step-up y segundo aprobador si se otorga suelta. `write` conserva todo lo que NO pone
    # una credencial elegida por el actor: alta de inventario sin contraseña, adopción, grants,
    # perfiles y agregar host copiando el hash de la cuenta origen.
    _spec(
        Capability.ENGINE_USERS_CREDENTIALS,
        "Elegir o definir contraseñas de usuarios del motor",
        mutates=True,
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
        destructive=True,
    ),
    _spec(Capability.BLUEPRINTS_READ, "Ver blueprints y sus versiones", agent=True),
    # Solo AUTORÍA: crear y editar blueprints y versiones en la BD del gateway. Todo lo que
    # escribe en las BDs de terceros (renombrar o migrar la tabla de versión, stampear,
    # borrar un blueprint con versiones) es `apply`.
    _spec(Capability.BLUEPRINTS_WRITE, "Crear y editar versiones de blueprint", mutates=True),
    # `write` ≠ `apply`: autor de la migración y ejecutor sobre producción son dos personas.
    # Es la separación que el TODO.md del repo ya pide por escrito.
    _spec(
        Capability.BLUEPRINTS_APPLY,
        "Aplicar y revertir versiones sobre bases reales",
        mutates=True,
        step_up=True,
        destructive=True,
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
        destructive=True,
    ),
    # Techo de agente (`list_clones` del MCP): solo ESTADO de los clonados que tocan una base que
    # el token alcanza. Ni plan, ni selección, ni `confirm_token`: lo que divulga es
    # `clones.execute`, que sigue fuera del techo.
    _spec(Capability.CLONES_READ, "Ver planes de clonado", agent=True),
    _spec(
        Capability.CLONES_EXECUTE,
        "Ejecutar un clonado de estructura y datos",
        mutates=True,
        discloses=True,
        step_up=True,
        destructive=True,
    ),
    _spec(Capability.COLLATION_READ, "Ver planes de conversión de collation"),
    # Destructiva: ``ALTER TABLE ... CONVERT`` reescribe la tabla del tercero y el propio
    # controller la llama "operación irreversible". Por eso solo ``owner``; a otra persona se
    # le otorga suelta (capacidad puntual) sobre un entorno o servidor.
    _spec(
        Capability.COLLATION_EXECUTE,
        "Ejecutar una conversión de collation",
        mutates=True,
        step_up=True,
        destructive=True,
    ),
    # Techo de agente (`list_exports` del MCP): estado y fechas, nunca artefacto ni contenido. La
    # divulgación vive en `exports.download`, que sigue fuera del techo.
    _spec(Capability.EXPORTS_READ, "Ver planes de exportación y su estado", agent=True),
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
        destructive=True,
    ),
    # Techo de agente (`list_catalogs` del MCP): datos de REFERENCIA globales del gateway
    # (privilegios, charsets, plantillas de perfil). Ninguna fila nombra servidores, bases ni
    # usuarios del motor; la escritura (`catalogs.write`) sigue fuera del techo.
    _spec(
        Capability.CATALOGS_READ,
        "Ver los catálogos de privilegios y charsets",
        axis="global",
        agent=True,
    ),
    # Dato de política: `privileges.is_active` decide qué se puede otorgar y
    # `permission_profiles` es la plantilla de GRANTs. Solo `security_officer`.
    _spec(
        Capability.CATALOGS_WRITE,
        "Editar los catálogos de privilegios, perfiles y charsets",
        mutates=True,
        step_up=True,
        axis="global",
    ),
    # Techo de agente (`list_environments` del MCP): la política de los entornos de las bases que
    # el token alcanza, y solo de esos — nunca la lista completa de entornos del gateway.
    _spec(
        Capability.ENVIRONMENTS_READ,
        "Ver los entornos y su política",
        axis="global",
        agent=True,
    ),
    # Dato de política: ``blocks_destructive_migrations``, ``allows_agent_access`` y la
    # clasificación de cada BD deciden qué barreras se aplican. Quien las escribe no puede ser
    # quien administra el acceso (``access_admin``) ni el rol operativo: solo
    # ``security_officer``. Sin ``security_officer`` asignado, esas escrituras quedan BLOQUEADAS
    # a propósito; no hay fallback a ``access.admin``.
    _spec(
        Capability.ENVIRONMENTS_WRITE,
        "Crear, editar y borrar entornos, abrir BDs a agentes y reclasificarlas",
        mutates=True,
        step_up=True,
        axis="global",
    ),
    _spec(
        Capability.ACCESS_ADMIN_CAP,
        "Administrar usuarios del gateway, accesos, capacidades puntuales y tokens",
        mutates=True,
        step_up=True,
        axis="global",
    ),
    _spec(
        Capability.POLICY_ADMIN,
        "Administrar la política del gateway: rotación del cifrado y lectura de la auditoría",
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
# `*.drop`, `blueprints.apply`, `sql_console.execute`, `collation.execute` ni ninguna capacidad
# que divulgue. Lo que se le niega por rol se le puede otorgar suelto (``capability_grants``).
_OPERATOR: frozenset[Capability] = _VIEWER | {
    Capability.ENGINE_USERS_WRITE,
    Capability.DATABASES_WRITE,
    Capability.BLUEPRINTS_WRITE,
    Capability.EXPORTS_EXECUTE,
}

# `owner` es todo lo OPERATIVO del alcance. NO incluye `servers.admin`, `catalogs.write`,
# `access.admin` ni `policy.admin`: son datos de política, la llave del inventario o la
# administración del acceso, y van en
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
    Capability.ENGINE_USERS_CREDENTIALS,
    Capability.DATABASES_DROP,
    Capability.BLUEPRINTS_APPLY,
    Capability.BLUEPRINTS_CAPTURES,
    Capability.SCHEMA_DIFF_EXECUTE,
    Capability.EXPORTS_DOWNLOAD,
    Capability.SQL_CONSOLE_EXECUTE,
    Capability.COLLATION_EXECUTE,
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
        GlobalCapability.ACCESS_ADMIN: frozenset({Capability.ACCESS_ADMIN_CAP}),
        GlobalCapability.SECURITY_OFFICER: frozenset(
            {
                Capability.POLICY_ADMIN,
                Capability.SERVERS_ADMIN,
                Capability.CATALOGS_WRITE,
                Capability.ENVIRONMENTS_WRITE,
            }
        ),
    }
)

#: Lo que ``owner`` tiene y ``operator`` no. Una capacidad puntual de este conjunto es ``owner``
#: en sustancia, y por eso cuenta para la regla de separación de deberes ``SOD_RULE_OWNER``.
OWNER_ONLY_CAPABILITIES: frozenset[Capability] = _OWNER - _OPERATOR

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
#: Un administrador intentó cambiar SU PROPIO rol, su propio acceso (globales o alcances) o
#: desactivarse. Lo tiene que hacer otra persona con ``access.admin``. Ver
#: ``GatewayUserController._guard_not_self``.
CODE_SELF_MODIFICATION = "access.self_modification_forbidden"
#: El actor no tiene la función que asigna eso (``ASSIGNABLE_BY``): hoy, no es
#: ``access_admin``. Reemplaza al viejo techo por TENENCIA (``access.grant_ceiling_exceeded``,
#: retirado en C3): ya no importa qué tiene quien asigna, sino qué le deja asignar su función.
#: Las rutas ya exigen ``access.admin``, así que en la práctica solo lo ve un llamador interno. 409.
CODE_NOT_ASSIGNABLE = "access.not_assignable"

# -- Capacidades puntuales (``capability_grants``) ------------------------------------------ #
#: La capacidad pedida es del eje global o no existe: no se puede otorgar suelta. 422.
CODE_CAPABILITY_NOT_GRANTABLE = "access.capability_not_grantable"
#: Ya hay una capacidad puntual viva (pendiente o activa) para ese usuario, capacidad y alcance. 409.
CODE_GRANT_DUPLICATE = "access.grant_duplicate"
#: El entorno o servidor del alcance no existe. 404 en ``capability_grants``; 422 en el PUT de
#: accesos (``set_access``), donde es un campo inválido del payload.
CODE_GRANT_SCOPE_NOT_FOUND = "access.grant_scope_not_found"
#: Borrar un entorno o servidor al que todavía apuntan accesos por alcance (``access_grants``)
#: o capacidades puntuales vivas (``pending``/``active``). ``scope_id`` no tiene FK (es
#: polimórfico), así que sin este 409 el grant sobreviviría a su destino y se pegaría al
#: próximo objeto que reutilice el id. 409.
CODE_SCOPE_HAS_GRANTS = "access.scope_has_grants"
#: El destinatario está desactivado: no se le otorga nada hasta reactivarlo. 409.
CODE_GRANT_USER_INACTIVE = "access.grant_user_inactive"
#: Quien pidió una capacidad sensible no puede aprobarla él mismo. 409.
CODE_SELF_APPROVAL = "access.self_approval_forbidden"
#: La solicitud ya no está pendiente (decidida, vencida o cancelada). 409.
CODE_GRANT_NOT_PENDING = "access.grant_not_pending"
#: La capacidad puntual no existe (o no pertenece a ese usuario). 404.
CODE_GRANT_NOT_FOUND = "access.grant_not_found"

# -- Separación de deberes (``app/core/separation_of_duties.py``) --------------------------- #
#: El estado RESULTANTE de la cuenta junta ``security_officer`` con ``owner`` (en cualquier
#: forma) o con ``access_admin``, sin una excepción viva que lo cubra ni ``sod_override``. 409.
#: ``public_context`` lleva ``rules`` y ``conflicts`` (qué fuente choca con qué).
CODE_SOD_CONFLICT = "access.sod_conflict"
#: ``sod_override`` mal formado: motivo demasiado corto o duración fuera de rango. 422.
CODE_SOD_OVERRIDE_INVALID = "access.sod_override_invalid"

# -- Solicitudes de acceso con segundo aprobador (``access_change_requests``, C3) ------------ #
#: NO es un error: va en ``data.code`` de la respuesta ``202``. La parte del cambio que eleva
#: (``needs_second_approver``) quedó PENDIENTE de otro ``access_admin``; el resto se aplicó.
CODE_ELEVATION_PENDING = "access.elevation_pending"
#: El acceso de la persona cambió desde que se pidió la elevación (``before_hash`` distinto): la
#: solicitud se cancela y hay que pedirla de nuevo sobre el estado actual. 409.
CODE_REQUEST_STALE = "access.request_stale"
#: La solicitud de acceso no existe. 404.
CODE_REQUEST_NOT_FOUND = "access.request_not_found"
#: La solicitud ya no está pendiente (aplicada, rechazada, cancelada o vencida). 409.
CODE_REQUEST_NOT_PENDING = "access.request_not_pending"
#: Solo quien pidió la elevación puede cancelarla (los demás la rechazan). 409.
CODE_REQUEST_NOT_REQUESTER = "access.request_not_requester"

#: Reglas de separación de deberes: el valor de ``sod_exceptions.rule``. Vocabulario CERRADO.
#: ``owner`` cuenta en cualquier forma: rol base, rol ``owner`` por alcance o una capacidad
#: puntual exclusiva de ``owner`` (``OWNER_ONLY_CAPABILITIES``): es ``owner`` en sustancia.
SOD_RULE_OWNER = "owner_security_officer"
#: ``access_admin`` + ``security_officer`` en una cuenta reconstruye al administrador combinado
#: que la partición de ``gateway.admin`` vino a deshacer.
SOD_RULE_ACCESS_ADMIN = "access_admin_security_officer"
SOD_RULES: tuple[str, ...] = (SOD_RULE_OWNER, SOD_RULE_ACCESS_ADMIN)

# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #


def spec(capability: Capability) -> CapabilitySpec:
    return _BY_ID[capability]


def is_grantable(capability: Capability | str) -> bool:
    """
    ¿Se puede otorgar SUELTA, sobre un entorno o servidor? Todo salvo el eje global.

    Las globales (``environments.write``, ``catalogs.write``, ``servers.admin``,
    ``access.admin``, ``policy.admin``, ``self.read``, lecturas de política) no tienen un destino al que
    anclarse, y otorgarlas por separado saltearía la separación de deberes. Una capacidad
    DESCONOCIDA no es otorgable: el lector falla cerrado.
    """
    try:
        return _BY_ID[Capability(capability)].scope_axis != "global"
    except (KeyError, ValueError):
        return False


def is_sensitive(capability: Capability | str) -> bool:
    """
    ¿Una capacidad puntual exige un segundo aprobador? Si es otorgable y EXCLUSIVA de ``owner``
    (``OWNER_ONLY_CAPABILITIES``): la regla es "todo lo que solo tiene ``owner`` pide una segunda
    persona" (``needs_second_approver``).

    Antes eran las que divulgan o son de nivel ``drop`` (8). Al retirar el techo por tenencia
    (C3), ``blueprints.apply``, ``schema_diff.execute`` y ``collation.execute`` —destructivas y
    solo de ``owner``— quedaban otorgables por UN solo administrador, porque lo único que las
    frenaba era que quien otorga tuviera ``owner``. El invariante 8 fija el conjunto (11) y
    exige que siga conteniendo todo lo que divulga o es ``drop``.
    """
    try:
        cap = Capability(capability)
    except ValueError:
        return False
    return is_grantable(cap) and cap in OWNER_ONLY_CAPABILITIES


# --------------------------------------------------------------------------- #
# Política de ASIGNACIÓN (C3): quién puede asignar qué, y qué pide un segundo  #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True, slots=True)
class Assignable:
    """Lo que una función puede ASIGNAR a otra cuenta: roles, globales y capacidades puntuales."""

    roles: frozenset[GatewayRole]
    globals_: frozenset[GlobalCapability]
    capabilities: frozenset[Capability]


#: Función → lo que puede asignar. Reemplaza al techo por TENENCIA ("nunca más de lo que tienes"),
#: que obligaba a que quien administra accesos tuviera también cada deber que reparte —justo la
#: combinación que la separación de deberes deshace—. La protección que daba el techo contra el
#: títere (un admin que se crea un ``owner`` y entra con su invitación) pasa al segundo
#: aprobador: ``needs_second_approver``. Código y no tabla, por lo mismo que ``ROLE_CAPABILITIES``.
ASSIGNABLE_BY: Mapping[GlobalCapability, Assignable] = MappingProxyType(
    {
        GlobalCapability.ACCESS_ADMIN: Assignable(
            roles=frozenset(GatewayRole),
            globals_=frozenset(GlobalCapability),
            capabilities=frozenset(s.id for s in CAPABILITIES if is_grantable(s.id)),
        ),
    }
)


def can_assign(
    actor_globals,
    *,
    role: GatewayRole | str | None = None,
    global_capability: GlobalCapability | str | None = None,
    capability: Capability | str | None = None,
) -> bool:
    """
    ¿Alguna de las funciones de ``actor_globals`` asigna esto? Fail-closed: un valor desconocido
    no es asignable por nadie.
    """
    try:
        r = GatewayRole(role) if role is not None else None
        g = GlobalCapability(global_capability) if global_capability is not None else None
        c = Capability(capability) if capability is not None else None
    except ValueError:
        return False
    for fn in actor_globals or ():
        try:
            pol = ASSIGNABLE_BY.get(GlobalCapability(fn))
        except ValueError:
            continue
        if pol is None:
            continue
        if r is not None and r not in pol.roles:
            continue
        if g is not None and g not in pol.globals_:
            continue
        if c is not None and c not in pol.capabilities:
            continue
        return True
    return False


def needs_second_approver(
    *,
    role: GatewayRole | str | None = None,
    global_capability: GlobalCapability | str | None = None,
    capability: Capability | str | None = None,
) -> bool:
    """
    ¿Esta asignación es una ELEVACIÓN que pide un segundo ``access_admin``? Lo exclusivo de
    ``owner``, en cualquiera de sus formas:

    - el rol ``owner`` (base o por alcance);
    - CUALQUIER capacidad global (``access_admin``, ``security_officer``): son funciones, no
      niveles, y cada una administra o apaga algo que la otra no debería poder;
    - una capacidad puntual de ``OWNER_ONLY_CAPABILITIES`` (``is_sensitive``).

    ``operator`` no: es el trabajo diario y no abre nada que ``owner`` reserve. Las bajas
    (demociones) nunca son elevación.
    """
    if role is not None and str(getattr(role, "value", role)) == GatewayRole.OWNER.value:
        return True
    if global_capability is not None:
        return True
    if capability is not None and is_sensitive(capability):
        return True
    return False


def _derive_implied_read() -> Mapping[Capability, frozenset[Capability]]:
    """
    Capacidad otorgable → la lectura de su módulo que trae implícita.

    Otorgar ``sql_console.execute`` sin ``sql_console.history`` dejaría a la persona
    ejecutando sin poder ver nada, así que escribir/ejecutar implica la lectura. Es la
    lectura de NIVEL VIEWER del mismo módulo (no muta ni divulga): nunca escala a otra cosa.
    """
    reads: dict[str, Capability] = {}
    for cap in _VIEWER:
        sp = _BY_ID[cap]
        if sp.scope_axis != "global" and not sp.mutates and not sp.discloses:
            reads[sp.module] = cap
    return MappingProxyType(
        {
            s.id: frozenset({reads[s.module]})
            for s in CAPABILITIES
            if is_grantable(s.id) and (s.mutates or s.discloses) and s.module in reads
        }
    )


#: Capacidad otorgable → lecturas implícitas (ver ``_derive_implied_read``).
IMPLIED_READ: Mapping[Capability, frozenset[Capability]] = _derive_implied_read()


def role_at_most(role: GatewayRole, ceiling: GatewayRole) -> bool:
    """``role`` ≤ ``ceiling`` en la cadena monotónica viewer < operator < owner."""
    return _ROLE_RANK[role] <= _ROLE_RANK[ceiling]


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
            "destructive": s.destructive,
            "grantable": is_grantable(s.id),
            "sensitive": is_sensitive(s.id),
            "implies": sorted(c.value for c in IMPLIED_READ.get(s.id, ())),
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


_SENSITIVE_POLICY: frozenset[str] = frozenset(
    {
        "engine_users.secrets",
        "engine_users.credentials",
        "blueprints.captures",
        "clones.execute",
        "exports.download",
        "sql_console.execute",
        "engine_users.drop",
        "databases.drop",
        # C3: sin el techo por tenencia, estas tres (destructivas, solo de owner) las otorgaba
        # un solo administrador. Ver `is_sensitive`.
        "blueprints.apply",
        "schema_diff.execute",
        "collation.execute",
    }
)


#: Ids que existieron y NO pueden volver (invariante 12). ``scripts/check_route_capabilities.py``
#: verifica además que ninguna ruta los declare.
RETIRED_CAPABILITIES: frozenset[str] = frozenset({"gateway.admin"})


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

    # 7. Ninguna capacidad global está en la cadena de roles, y viceversa. Si `access.admin`
    #    cayera en `owner`, el rol operativo podría apagar la barrera de producción.
    globales = set().union(*GLOBAL_CAPABILITIES.values())
    for cap in globales:
        for role, caps in ROLE_CAPABILITIES.items():
            if cap in caps:
                raise AssertionError(
                    f"{cap.value} es global y además está en el rol '{role.value}'."
                )

    # 7b. `operator` no divulga. Las que divulgan no participan del orden acumulativo: si
    #     `engine_users.secrets` llegara a `operator` por arrastre desde `write`, el rol de
    #     trabajo diario leería las credenciales del motor del cliente.
    for cap in ROLE_CAPABILITIES[GatewayRole.OPERATOR]:
        if _BY_ID[cap].discloses:
            raise AssertionError(f"'operator' tiene una capacidad que divulga: {cap.value}")

    # 7c. Destructivas: subconjunto de `mutates`, solo en `owner` dentro de la cadena, y con
    #     step-up. Sin esto, "destructivo ⇒ owner ∧ step-up" valía por coincidencia de valores.
    for s in CAPABILITIES:
        if not s.destructive:
            continue
        if not s.mutates:
            raise AssertionError(f"{s.id.value} es destructive y no mutates.")
        if not s.requires_step_up:
            raise AssertionError(f"{s.id.value} es destructive y no exige step-up.")
        for role in (GatewayRole.VIEWER, GatewayRole.OPERATOR):
            if s.id in ROLE_CAPABILITIES[role]:
                raise AssertionError(f"{s.id.value} es destructive y está en '{role.value}'.")

    # 8. Capacidades puntuales. La política fija las sensibles en EXACTAMENTE estas 11 (owner
    #    menos operator, otorgables): si el catálogo crece, esto obliga a decidirlo a propósito.
    #    Y el criterio viejo (divulga o es `drop`) sigue contenido: ninguna otorgable que divulgue
    #    o borre puede quedar sin segundo aprobador.
    sensibles = {s.id.value for s in CAPABILITIES if is_sensitive(s.id)}
    if sensibles != _SENSITIVE_POLICY:
        raise AssertionError(f"Conjunto sensible fuera de la política: {sorted(sensibles)}")
    if sensibles != {c.value for c in OWNER_ONLY_CAPABILITIES if is_grantable(c)}:
        raise AssertionError("Las sensibles tienen que ser exactamente owner − operator otorgables.")
    for s in CAPABILITIES:
        if is_grantable(s.id) and (s.discloses or s.level == "drop") and not is_sensitive(s.id):
            raise AssertionError(f"{s.id.value} divulga o es drop y no pide segundo aprobador.")
    #    Política de asignación: `access_admin` asigna TODO lo asignable (es la única función que
    #    administra accesos), y toda capacidad que asigna es otorgable (nunca una global suelta).
    pol = ASSIGNABLE_BY.get(GlobalCapability.ACCESS_ADMIN)
    if pol is None or pol.roles != frozenset(GatewayRole) or pol.globals_ != frozenset(
        GlobalCapability
    ):
        raise AssertionError("'access_admin' tiene que poder asignar todos los roles y globales.")
    for fn, p in ASSIGNABLE_BY.items():
        if Capability.ACCESS_ADMIN_CAP not in GLOBAL_CAPABILITIES[fn]:
            raise AssertionError(f"'{fn.value}' asigna accesos sin tener 'access.admin'.")
        for c in p.capabilities:
            if not is_grantable(c):
                raise AssertionError(f"'{fn.value}' asigna {c.value}, que no es otorgable.")
    #    Ninguna capacidad global es otorgable, y toda lectura implícita es de nivel viewer.
    for s in CAPABILITIES:
        if s.scope_axis == "global" and is_grantable(s.id):
            raise AssertionError(f"{s.id.value} es global y otorgable.")
    for cap, implied in IMPLIED_READ.items():
        for r in implied:
            if r not in _VIEWER or _BY_ID[r].mutates or _BY_ID[r].discloses:
                raise AssertionError(f"{cap.value} implica {r.value}, que no es lectura viewer.")

    # 11. Step-up ⇒ NO agente. Un token no tiene contraseña que reconfirmar: una capacidad con
    #     step-up en el techo de agente sería una operación que el agente nunca puede completar
    #     o, peor, un incentivo a eximir a los tokens del step-up. Ver `app/core/step_up.py`.
    for s in CAPABILITIES:
        if s.requires_step_up and s.agent_allowed:
            raise AssertionError(f"{s.id.value} exige step-up y es agent_allowed.")

    # 9. Los conjuntos de las globales son DISJUNTOS de a pares. Es la separación de deberes
    #    hecha forma: si una capacidad viviera en dos globales —como `gateway.admin`, que
    #    tenían `access_admin` y `security_officer`—, quien tiene solo una de las dos haría
    #    también el trabajo de la otra.
    vistas: dict[Capability, GlobalCapability] = {}
    for g, caps in GLOBAL_CAPABILITIES.items():
        for cap in caps:
            if cap in vistas:
                raise AssertionError(
                    f"{cap.value} está en dos globales: '{vistas[cap].value}' y '{g.value}'."
                )
            vistas[cap] = g

    # 10. `access_admin` es EXACTAMENTE `access.admin`. Es el rol explícitamente NO operativo:
    #     cualquier capacidad que se le sume (crypto, entornos, catálogos) vuelve a juntar la
    #     administración del acceso con la política que el acceso no debería poder desarmar.
    if GLOBAL_CAPABILITIES[GlobalCapability.ACCESS_ADMIN] != frozenset(
        {Capability.ACCESS_ADMIN_CAP}
    ):
        raise AssertionError("'access_admin' tiene que ser exactamente {access.admin}.")

    # 12. `gateway.admin` está RETIRADA. Reintroducirla, aunque sea con otro significado,
    #     reabre la confusión de quién administra qué y le devuelve sentido a filas viejas de
    #     `api_tokens.scopes`/`capability_grants` que hoy los lectores descartan.
    retiradas = {c.value for c in Capability} & RETIRED_CAPABILITIES
    if retiradas:
        raise AssertionError(f"Capacidades retiradas reintroducidas: {sorted(retiradas)}")


_assert_invariants()
