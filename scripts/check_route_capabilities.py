#!/usr/bin/env python3
"""Verifica que NINGUNA ruta del gateway quede sin guard de autorización.

Existe por el modo de fallo real de un sistema de permisos sobre 157 rutas: no es un modelo
malo, es **un endpoint que se olvidó de declarar su capacidad**. Y hoy hay 153 oportunidades de
equivocarse, una por firma. Un endpoint nuevo copiado de uno viejo, o escrito de cero sin
ningún parámetro de actor, nace **público** y nada falla: no lo ve el linter, no lo ve el
compilador, y no lo ve quien revisa el PR porque la ausencia de un parámetro no se lee como un
error. El único momento en que aparecería es cuando alguien lo explota.

Precedente exacto en este repo: ``app/routes/v1/test.py`` tenía **siete rutas sin
autenticación** —dos de ellas subiendo archivos a disco— montadas en la API durante meses,
porque nadie las miró de nuevo después de escribirlas.

LA REGLA
--------
**Toda ruta declara una capacidad del catálogo** (el marcador ``__gw_capability__`` que estampa
``require()``) o está en ``PUBLIC_ROUTES``. No hay tercera opción.

Durante el swap sí la había —``AdminDep``, el guard que solo verificaba sesión— y eso es lo que
hizo seguro migrar de a un módulo: una ruta sin ninguna de las dos rompía el chequeo, así que
**nunca existió un commit donde algo quedara sin guard**. Terminado el swap, ``AdminDep`` se
retiró del código en vez de deprecarse, para que un endpoint nuevo copiado de uno viejo falle al
importar; así que acá quedó el invariante, sin la rama de transición.

EL TRINQUETE
------------
Las rutas migradas solo pueden CRECER (``MIN_MIGRATED_ROUTES``). Un umbral que solo se mueve en
una dirección es un trinquete en vez de una intención: un revert parcial que deje N rutas sin
capacidad falla en el conteo aunque el chequeo 1 no lo vea —por ejemplo si alguien las mete en
``PUBLIC_ROUTES`` para "arreglar" el build—, en vez de pasar inadvertido.

POR QUÉ IMPORTA LA APP Y NO LEE LOS FUENTES CON ``ast``
--------------------------------------------------------
Al contrario de ``check_migration_graph.py``, que sí usa ``ast``: acá lo que hay que inspeccionar
es el **grafo de dependencias resuelto**, y una dependencia puede venir de una sub-dependencia,
de un alias re-exportado o de un router anidado. Con ``ast`` habría que reimplementar la
resolución de FastAPI, que es exactamente la clase de segunda implementación que diverge. Y hay
un motivo adicional: el conjunto de rutas depende de la configuración (``DOCS_ENABLED`` monta y
desmonta rutas), así que el inventario tiene que salir de la app REAL, montada.

Importar ``main`` no abre ninguna conexión: la BD se instancia perezosamente. El script fija
por su cuenta las variables mínimas para saltear los guards de arranque de producción, así que
corre sin ``.env``.

POR QUÉ NO ES UN ASSERT DE ARRANQUE
-----------------------------------
Sería tentador abortar el ``lifespan`` si alguna ruta no declara capacidad, y así el endpoint
sin guard no podría servir ni una respuesta. Se descartó por tres modos de fallo: (1) el
conjunto de rutas **depende de variables de entorno**, así que un assert cuyo resultado cambia
con la configuración no es verificable en CI —pasa con la del runner y falla al bootear con la
de producción—; (2) las rutas registradas después del ``lifespan`` y las sub-apps montadas
quedarían invisibles al inventario, con el agravante de que el assert da la sensación de que
eso es imposible; (3) un crashloop en un reinicio **no relacionado** (OOM kill, drain de nodo,
scale-up) deja un pod que no puede volver con el binario que andaba bien hace un minuto.

La propiedad de "no puede servir una respuesta" se consigue mejor con una **dependencia global
de la sub-app**, evaluada en runtime, que corre DESPUÉS del routing y por eso sí ve la ruta
resuelta. Ya no hay nada que la bloquee: es el siguiente endurecimiento posible sobre esta base.

Uso::

    python scripts/check_route_capabilities.py          # 0 si está sano, 1 si no
    python scripts/check_route_capabilities.py --list   # además imprime el inventario
"""

from __future__ import annotations

import ast
import inspect
import os
import sys
import textwrap

# Antes de importar la app: los guards de arranque de `app/core/environments.py` exigen
# secretos y orígenes CORS explícitos en producción, y este script es una herramienta de
# desarrollo que tiene que correr sin `.env`.
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SECRET_KEY", "check-route-capabilities")
os.environ.setdefault("CRYPTO_KEY_SALT", "check-route-capabilities")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.routing import APIRoute  # noqa: E402

from app.core.authz import (  # noqa: E402
    declared_capability,
    declared_scope,
    declared_step_up_exempt,
)
from app.core.scope_targets import _RESOLVERS  # noqa: E402
from app.services.capability_catalog import (  # noqa: E402
    RETIRED_CAPABILITIES,
    Capability,
    GatewayRole,
    role_capabilities,
    spec,
)

#: Rutas deliberadamente SIN autorización. Lista explícita y corta: la alternativa —una
#: heurística por prefijo— es cómo `/api/v1/test/*` se quedó sin guard durante meses.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/auth/login"),  # no puede exigir sesión: la crea
        ("GET", "/health"),  # sonda del orquestador, sin versión y sin rate limit
        ("GET", "/health/ready"),
        # Aceptar una invitación NO puede exigir sesión: quien la usa todavía no tiene
        # credencial, que es justamente el punto del diseño (la password inicial no la pone
        # quien crea la cuenta). Se autoriza con el token firmado, que va atado al
        # `credential_epoch` del usuario y por eso es de un solo uso.
        ("POST", "/api/v1/gateway-users/invite/accept"),
    }
)

#: Rutas autenticadas por BEARER cuya autorización es por TOOL y no por endpoint: el servidor
#: MCP. No van en `PUBLIC_ROUTES` porque **no son públicas** —exigen un token válido— y no
#: declaran capacidad porque el scope lo verifica el registro de tools, endpoint por endpoint
#: sería una segunda copia del vocabulario.
#:
#: El chequeo 5 verifica que cada una tenga de verdad la dependencia de autenticación de
#: agentes: sin eso, esta lista sería un agujero con nombre elegante.
AGENT_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        # UNA sola entrada: `POST /mcp` (sin barra) llega acá reescrito por
        # `McpPathNormalizer`, no como una ruta aparte. Ver su docstring.
        ("POST", "/mcp/"),
    }
)

#: Capacidades que existen para consumidores que NO son rutas (tools del MCP, workers). Sin
#: esta lista, el chequeo 4 reportaría como vocabulario muerto algo que sí tiene consumidor.
#: ``data.query`` lo consume ``run_select`` (tool del MCP, scope por tool) y ninguna ruta HTTP:
#: ``data.read`` sí tiene ruta (el opt-in por base), pero el SQL libre de un agente no.
#: ``data.definitions`` es solo scope de tool del MCP (sin ruta HTTP).
NON_ROUTE_CAPABILITIES: frozenset[Capability] = frozenset(
    {Capability.DATA_QUERY, Capability.DATA_DEFINITIONS}
)

#: Cuántas rutas declaran capacidad. **Solo puede SUBIR.** Ver "EL TRINQUETE".
MIN_MIGRATED_ROUTES = 196

#: Rutas con capacidad de alcance que NO apuntan a ningún entorno, con el motivo. Es la autoría
#: de blueprints y los proyectos (escribir una versión no la ejecuta en ninguna BD), más el borrado
#: de un blueprint que ninguna BD referencia. Una entrada
#: sin motivo no tiene sentido, por eso es un dict y no un conjunto.
#: Rutas cuya capacidad exige step-up y que se EXIMEN de él (``step_up=False`` en
#: ``require``/``require_at``), con el motivo. Chequeo 7: toda ruta marcada tiene que estar acá y
#: toda entrada tiene que ser una ruta marcada, cuya capacidad pida step-up y que sea un
#: ``POST .../cancel``. Solo cancelaciones: frenar una operación destructiva nunca puede costar
#: más que lanzarla. Agregar algo que no sea cancelar es reabrir el step-up.
STEP_UP_EXEMPT: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/database-clones/{job_id}/cancel"): (
        "cancelar un clonado: frenarlo no puede costar más que lanzarlo"
    ),
    ("POST", "/api/v1/database-clone-batches/{batch_id}/cancel"): (
        "cancelar un lote de clonado: frenarlo no puede costar más que lanzarlo"
    ),
    ("POST", "/api/v1/collation-conversions/{job_id}/cancel"): (
        "cancelar una conversión de collation: frenarla no puede costar más que lanzarla"
    ),
    ("POST", "/api/v1/database-models/{model_id}/collation-conversions/{batch_id}/cancel"): (
        "cancelar un lote de conversión de collation: frenarlo no puede costar más que lanzarlo"
    ),
    ("POST", "/api/v1/access-requests/{request_id}/cancel"): (
        "retirar una elevación propia pendiente: nunca da acceso, y frenarla no puede costar "
        "más que pedirla"
    ),
}


def _is_step_up_exempt(route: APIRoute) -> bool:
    """¿Alguna dependencia del árbol resuelto exime del step-up?"""

    def walk(dependant) -> bool:
        if declared_step_up_exempt(getattr(dependant, "call", None)):
            return True
        return any(walk(sub) for sub in getattr(dependant, "dependencies", []) or [])

    return walk(route.dependant)


def step_up_errors(app, *, exempt: dict[tuple[str, str], str]) -> list[str]:
    """
    Chequeo 7: las exenciones de step-up son EXACTAMENTE ``exempt`` y todas son cancelaciones.

    Función aparte (como ``scope_errors``) para que el test la ejerza con apps sintéticas.
    """
    errores: list[str] = []
    marcadas: set[tuple[str, str]] = set()
    for path, route in _iter_routes(app):
        if not _is_step_up_exempt(route):
            continue
        cap = _capability_of(route)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            clave = (method, path)
            marcadas.add(clave)
            if clave not in exempt:
                errores.append(
                    f"{method} {path} se exime del step-up (step_up=False) y no está en "
                    "STEP_UP_EXEMPT."
                )
            if method != "POST" or not path.endswith("/cancel"):
                errores.append(
                    f"{method} {path} se exime del step-up y no es un POST .../cancel."
                )
            if cap is None or not spec(Capability(cap)).requires_step_up:
                errores.append(
                    f"{method} {path} se exime del step-up pero su capacidad no lo exige: "
                    "la entrada sobra."
                )
    fantasmas = set(exempt) - marcadas
    if fantasmas:
        errores.append(
            "Entradas de STEP_UP_EXEMPT que no corresponden a ninguna ruta eximida: "
            + ", ".join(f"{m} {p}" for m, p in sorted(fantasmas))
        )
    return errores


SCOPE_EXEMPT: dict[tuple[str, str], str] = {
    ("POST", "/api/v1/database-models"): (
        "autoría de blueprint: crear un modelo no apunta a ninguna BD"
    ),
    ("PATCH", "/api/v1/database-models/{model_id}"): (
        "autoría de blueprint: editar metadatos del modelo no toca ninguna BD"
    ),
    ("DELETE", "/api/v1/database-models/{model_id}"): (
        "borrar un modelo exige blueprints.apply y responde 409 si alguna BD lo referencia: "
        "cuando procede no queda ninguna BD a la que anclar el alcance"
    ),
    ("POST", "/api/v1/database-models/{model_id}/migrations"): (
        "autoría de blueprint: escribir una versión no la ejecuta en ninguna BD"
    ),
    ("PATCH", "/api/v1/database-models/{model_id}/migrations/{version}"): (
        "autoría de blueprint: editar una versión no la ejecuta en ninguna BD"
    ),
    ("POST", "/api/v1/database-models/{model_id}/migrations/{version}/edit-preview"): (
        "autoría de blueprint: la vista previa no toca ninguna BD"
    ),
    ("POST", "/api/v1/projects"): (
        "proyectos: agrupación de blueprints, sin destino remoto"
    ),
    ("DELETE", "/api/v1/projects/{project_id}"): (
        "proyectos: agrupación de blueprints, sin destino remoto"
    ),
    ("PATCH", "/api/v1/projects/{project_id}"): (
        "proyectos: agrupación de blueprints, sin destino remoto"
    ),
    ("POST", "/api/v1/projects/{project_id}/blueprints"): (
        "proyectos: agrupación de blueprints, sin destino remoto"
    ),
    ("DELETE", "/api/v1/projects/{project_id}/blueprints/{model_id}"): (
        "proyectos: agrupación de blueprints, sin destino remoto"
    ),
}

#: Rutas con capacidad de alcance que todavía NO declaran destino con ``require_at``. VACÍO desde
#: la última unidad de trabajo y tiene que seguir así: ``main`` falla (chequeo 6 estricto) si
#: tiene una sola entrada. Existe, vacío, solo porque ``scope_errors`` lo recibe por parámetro y
#: los tests la ejercen con apps sintéticas y listas propias.
SCOPE_PENDING: frozenset[tuple[str, str]] = frozenset()

#: Tope de ``SCOPE_PENDING``: cero. Ya no hay camino para "migrar después".
MAX_SCOPE_PENDING = 0


def _iter_routes(app, prefix: str = ""):
    """Aplana la app y sus sub-apps montadas. Las versiones viven en mounts."""
    for route in getattr(app, "routes", []):
        if isinstance(route, APIRoute):
            yield prefix + route.path, route
        elif hasattr(route, "app"):
            yield from _iter_routes(route.app, prefix + getattr(route, "path", ""))


def _capability_of(route: APIRoute) -> str | None:
    """
    La capacidad que declara una ruta, recorriendo el árbol de dependencias RESUELTO.

    Recursivo a propósito: una dependencia puede tener sub-dependencias, y el marcador puede
    estar en cualquier nivel.
    """

    def walk(dependant) -> str | None:
        cap = declared_capability(getattr(dependant, "call", None))
        if cap:
            return cap
        for sub in getattr(dependant, "dependencies", []) or []:
            found = walk(sub)
            if found:
                return found
        return None

    return walk(route.dependant)


def _uses_agent_auth(route: APIRoute) -> bool:
    """``True`` si la ruta cuelga de ``authenticate_agent``. Se detecta por el callable."""
    from app.core.mcp_auth import authenticate_agent
    from app.mcp.app_factory import _agente

    objetivos = {authenticate_agent, _agente}

    def walk(dependant) -> bool:
        if getattr(dependant, "call", None) in objetivos:
            return True
        return any(walk(s) for s in (getattr(dependant, "dependencies", []) or []))

    return walk(route.dependant)


def _scope_of(route: APIRoute) -> str | None:
    """El tipo de destino que declara una ruta (``require_at``), recorriendo el árbol resuelto."""

    def walk(dependant) -> str | None:
        kind = declared_scope(getattr(dependant, "call", None))
        if kind:
            return kind
        for sub in getattr(dependant, "dependencies", []) or []:
            found = walk(sub)
            if found:
                return found
        return None

    return walk(route.dependant)


def _calls_assert_capability(route: APIRoute) -> bool:
    """
    ``True`` si el endpoint llama a ``assert_capability(...)``. Se lee el AST del fuente y no el
    texto: un docstring que mencione el nombre no es una llamada.
    """
    try:
        arbol = ast.parse(textwrap.dedent(inspect.getsource(route.endpoint)))
    except (OSError, TypeError, SyntaxError):
        return False
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Call):
            f = nodo.func
            nombre = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if nombre == "assert_capability":
                return True
    return False


def _in_scope(cap: str) -> bool:
    """Capacidad con eje de alcance y por encima del piso ``viewer``: la que exige destino."""
    try:
        c = Capability(cap)
    except ValueError:
        return False
    return (
        spec(c).scope_axis != "global"
        and c not in role_capabilities(GatewayRole.VIEWER)
    )


def scope_errors(
    app,
    *,
    pending: frozenset[tuple[str, str]],
    exempt: dict[tuple[str, str], str],
    max_pending: int,
    resolvers: dict | None = None,
) -> list[str]:
    """
    Chequeo 6 (y 6b): toda ruta de alcance declara su destino o está en una lista con nombre.

    Es una función y no un bloque de ``main`` para que el test de cobertura la ejerza con apps
    sintéticas y listas propias: la prueba de que el chequeo SIRVE no puede depender de que la
    app real esté rota.
    """
    resolvers = _RESOLVERS if resolvers is None else resolvers
    errores: list[str] = []
    vivas: set[tuple[str, str]] = set()

    for path, route in _iter_routes(app):
        cap = _capability_of(route)
        kind = _scope_of(route)
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            clave = (method, path)
            vivas.add(clave)
            if kind is not None:
                if kind not in resolvers:
                    errores.append(
                        f"{method} {path} declara el destino '{kind}', que no está registrado."
                    )
                # Entrada vieja: la ruta ya migró y la lista sigue nombrándola.
                if clave in pending or clave in exempt:
                    errores.append(
                        f"{method} {path} ya declara destino pero sigue en "
                        "SCOPE_PENDING/SCOPE_EXEMPT (entrada vieja)."
                    )
                # 6b: dentro de una ruta con capa 2, ``assert_capability`` es solo capa 1.
                if _calls_assert_capability(route):
                    errores.append(
                        f"{method} {path} llama a assert_capability() pero tiene destino "
                        "declarado: usá assert_at() (6b)."
                    )
            elif cap is not None and _in_scope(cap):
                if clave not in pending and clave not in exempt:
                    errores.append(
                        f"{method} {path} ({cap}) no declara destino con require_at y no está "
                        "en SCOPE_EXEMPT ni en SCOPE_PENDING."
                    )

    fantasmas = (pending | set(exempt)) - vivas
    if fantasmas:
        errores.append(
            "Entradas de SCOPE_PENDING/SCOPE_EXEMPT que no corresponden a ninguna ruta: "
            + ", ".join(f"{m} {p}" for m, p in sorted(fantasmas))
        )
    if len(pending) > max_pending:
        errores.append(
            f"SCOPE_PENDING CRECIÓ: {len(pending)} entradas, el máximo es {max_pending}. "
            "La lista solo puede encoger."
        )
    return errores


def main() -> int:
    from main import app

    errores: list[str] = []
    sin_guard: list[str] = []
    agente: list[str] = []
    migradas: list[str] = []
    usadas: set[str] = set()
    validas = {c.value for c in Capability}

    for path, route in _iter_routes(app):
        for method in sorted(route.methods - {"HEAD", "OPTIONS"}):
            clave = (method, path)
            cap = _capability_of(route)

            if cap is not None:
                migradas.append(f"{method} {path}")
                usadas.add(cap)
                # Chequeo 3: la capacidad declarada es del catálogo cerrado.
                if cap not in validas:
                    errores.append(
                        f"{method} {path} declara '{cap}', que no está en el catálogo."
                    )
                # Chequeo 8 (trinquete): ninguna ruta declara una capacidad RETIRADA. Hoy el
                # chequeo 3 ya la atrapa porque no está en el catálogo; este existe para el día
                # que alguien la reintroduzca en el enum "para que compile" y lo ponga verde.
                if cap in RETIRED_CAPABILITIES:
                    errores.append(
                        f"{method} {path} declara '{cap}', que está RETIRADA: "
                        "usá access.admin (accesos) o policy.admin (política)."
                    )
                continue

            if clave in PUBLIC_ROUTES:
                continue

            if clave in AGENT_ROUTES:
                agente.append(f"{method} {path}")
                # Chequeo 5: una ruta de agente TIENE que exigir el bearer. Sin esto, la lista
                # de arriba sería una forma de declarar "sin guard" que suena bien.
                if not _uses_agent_auth(route):
                    errores.append(
                        f"{method} {path} está en AGENT_ROUTES y NO exige la autenticación "
                        "de agentes (app.core.mcp_auth.authenticate_agent)."
                    )
                continue

            # Chequeo 1: ni capacidad ni pública. Nace abierta.
            sin_guard.append(f"{method} {path}")

    if sin_guard:
        errores.append(
            "Rutas SIN capacidad declarada y fuera de PUBLIC_ROUTES/AGENT_ROUTES:\n  "
            + "\n  ".join(sorted(sin_guard))
        )

    # Chequeo 2: toda entrada de PUBLIC_ROUTES corresponde a una ruta VIVA. Sin esto la lista
    # se podre en un comodín acumulado que nadie revisa.
    vivas = {
        (m, path)
        for path, route in _iter_routes(app)
        for m in route.methods - {"HEAD", "OPTIONS"}
    }
    fantasmas = (PUBLIC_ROUTES | AGENT_ROUTES) - vivas
    if fantasmas:
        errores.append(
            "Entradas de PUBLIC_ROUTES/AGENT_ROUTES que ya no corresponden a ninguna ruta: "
            + ", ".join(f"{m} {p}" for m, p in sorted(fantasmas))
        )

    # Chequeo 4: vocabulario muerto. Una capacidad que ninguna ruta usa y que no está
    # declarada como no-ruta es una promesa que `/auth/me` publica y nadie puede ejercer.
    muertas = validas - usadas - {c.value for c in NON_ROUTE_CAPABILITIES}
    aviso_muertas = sorted(muertas)
    if aviso_muertas:
        errores.append(
            "Capacidades que ninguna ruta declara (vocabulario muerto): "
            + ", ".join(aviso_muertas)
        )

    # El trinquete.
    if len(migradas) < MIN_MIGRATED_ROUTES:
        errores.append(
            f"Las rutas migradas BAJARON: {len(migradas)}, el mínimo es "
            f"{MIN_MIGRATED_ROUTES}. Un revert parcial no puede pasar inadvertido."
        )

    # Chequeo 6 ESTRICTO: la lista de pendientes no admite ni una entrada. Una ruta de alcance
    # sin destino y sin motivo en ``SCOPE_EXEMPT`` es un error, sin período de gracia.
    if SCOPE_PENDING or MAX_SCOPE_PENDING:
        errores.append(
            "SCOPE_PENDING tiene que estar VACÍO y MAX_SCOPE_PENDING valer 0: declará el destino "
            "con require_at o, si no apunta a ninguna BD, ponelo en SCOPE_EXEMPT con su motivo."
        )

    errores.extend(
        scope_errors(
            app,
            pending=SCOPE_PENDING,
            exempt=SCOPE_EXEMPT,
            max_pending=MAX_SCOPE_PENDING,
        )
    )

    errores.extend(step_up_errors(app, exempt=STEP_UP_EXEMPT))

    if "--list" in sys.argv:
        print(f"SCOPE_PENDING ({len(SCOPE_PENDING)}), SCOPE_EXEMPT ({len(SCOPE_EXEMPT)})")
        print(f"Migradas ({len(migradas)}):")
        for r in sorted(migradas):
            print(f"  {r}")

        print()

    if errores:
        for e in errores:
            print(f"ERROR: {e}", file=sys.stderr)
        return 1

    print(
        f"OK: cobertura de autorización sana — {len(migradas)} ruta(s) con capacidad, "
        f"{len(PUBLIC_ROUTES)} públicas declaradas, {len(agente)} de agente, "
        f"0 sin guard, 0 capacidad muerta, 0 con destino pendiente."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
