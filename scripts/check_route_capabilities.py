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

import os
import sys

# Antes de importar la app: los guards de arranque de `app/core/environments.py` exigen
# secretos y orígenes CORS explícitos en producción, y este script es una herramienta de
# desarrollo que tiene que correr sin `.env`.
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("SECRET_KEY", "check-route-capabilities")
os.environ.setdefault("CRYPTO_KEY_SALT", "check-route-capabilities")

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.routing import APIRoute  # noqa: E402

from app.core.authz import declared_capability  # noqa: E402
from app.services.capability_catalog import Capability  # noqa: E402

#: Rutas deliberadamente SIN autorización. Lista explícita y corta: la alternativa —una
#: heurística por prefijo— es cómo `/api/v1/test/*` se quedó sin guard durante meses.
PUBLIC_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/api/v1/auth/login"),  # no puede exigir sesión: la crea
        ("GET", "/health"),  # sonda del orquestador, sin versión y sin rate limit
        ("GET", "/health/ready"),
    }
)

#: Capacidades que existen para consumidores que NO son rutas (tools del MCP, workers). Sin
#: esta lista, el chequeo 4 reportaría como vocabulario muerto algo que sí tiene consumidor.
NON_ROUTE_CAPABILITIES: frozenset[Capability] = frozenset()

#: Cuántas rutas declaran capacidad. **Solo puede SUBIR.** Ver "EL TRINQUETE".
MIN_MIGRATED_ROUTES = 156


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


def main() -> int:
    from main import app

    errores: list[str] = []
    sin_guard: list[str] = []
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
                continue

            if clave in PUBLIC_ROUTES:
                continue

            # Chequeo 1: ni capacidad ni pública. Nace abierta.
            sin_guard.append(f"{method} {path}")

    if sin_guard:
        errores.append(
            "Rutas SIN capacidad declarada y fuera de PUBLIC_ROUTES:\n  "
            + "\n  ".join(sorted(sin_guard))
        )

    # Chequeo 2: toda entrada de PUBLIC_ROUTES corresponde a una ruta VIVA. Sin esto la lista
    # se podre en un comodín acumulado que nadie revisa.
    vivas = {
        (m, path)
        for path, route in _iter_routes(app)
        for m in route.methods - {"HEAD", "OPTIONS"}
    }
    fantasmas = PUBLIC_ROUTES - vivas
    if fantasmas:
        errores.append(
            "Entradas de PUBLIC_ROUTES que ya no corresponden a ninguna ruta: "
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

    if "--list" in sys.argv:
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
        f"{len(PUBLIC_ROUTES)} públicas declaradas, 0 sin guard, 0 capacidad muerta."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
