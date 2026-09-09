"""
Guard de importaciones de ``app/mcp/**``: ningún módulo del paquete alcanza la capa de motor.

QUÉ COMPRA Y QUÉ NO — LEER ESTO ANTES DE CONFIAR EN ÉL
------------------------------------------------------
**Compra**: evitar la deriva accidental. Alguien que agregue una tool y necesite "solo consultar
algo rápido" tiene que romper este test para hacerlo, y ahí aparece la conversación.

**NO compra**: probar que no hay puerta de atrás. Es evadible de dos maneras conocidas y las dos
están escritas acá para que nadie lo sobreestime:

1. **Por transitividad.** ``app/mcp/context.py`` importa el resolvedor, que necesariamente importa
   la capa de motor: está siempre a un salto.
2. **Por ``importlib.import_module``**, que no produce ningún nodo ``Import`` en el AST.

La capa que de verdad cierra la puerta es ``ToolContext``: un handler no recibe nada reusable.
Este guard es la red de arriba, no la cerradura.

ES UNA ALLOWLIST, NO UNA BLOCKLIST
----------------------------------
Con una blocklist, un módulo peligroso nuevo hay que **acordarse** de agregarlo — y el día que
alguien no se acuerde, el guard pasa y no protege nada. Con allowlist, un import nuevo falla por
default y hay que declararlo a mano, que es la dirección correcta del esfuerzo.
"""

import ast
import pathlib

import pytest

_RAIZ = pathlib.Path(__file__).resolve().parents[1]
_PAQUETE = _RAIZ / "app" / "mcp"

#: Los ÚNICOS módulos del proyecto que `app/mcp/**` puede importar. Agregar uno acá es una
#: decisión que se revisa; no agregarlo es que el test falle, que es el default correcto.
PERMITIDOS = frozenset(
    {
        "app.core.actor",
        "app.core.environments",
        "app.core.logger",
        "app.core.mcp_auth",
        "app.controllers.target_resolution",
        "app.exceptions",
        # `app/exceptions/AppHttpException.py` ES un módulo, así que el import resuelve a este
        # nombre y no al paquete. Se declara aparte en vez de aflojar la allowlist a "cualquier
        # submódulo de un paquete permitido": esa relajación dejaría entrar todo `app.services`.
        "app.exceptions.AppHttpException",
        "app.middleware.ContextMiddleware",
        "app.services.audit",
        "app.services.capability_catalog",
        "app.services.mcp_catalog",
        "app.schemas.mcp",
    }
)

#: Lo que jamás puede aparecer, ni siquiera declarado arriba. Es redundante con la allowlist a
#: propósito: si alguien agrega uno de estos a `PERMITIDOS` "un rato", este chequeo lo frena.
NUNCA = frozenset(
    {
        "app.core.remote_engine",
        "app.core.database",
        "app.controllers.common",
        "app.services.db_admin.factory",
    }
)


def _modulos():
    return sorted(_PAQUETE.rglob("*.py"))


def _imports_de(path: pathlib.Path) -> set[str]:
    """
    Los módulos del proyecto que este archivo importa, **ya resueltos**.

    El detalle que hace o rompe este guard: ``from app.services import audit`` produce un nodo
    con ``module="app.services"``, así que mirar solo ``node.module`` clasificaría el import
    como "app.services" y la allowlist no podría distinguir ``audit`` de cualquier otro
    servicio. Se resuelve a ``app.services.audit`` cuando eso corresponde a un módulo real, y
    se cae al paquete cuando lo importado es un símbolo (``from app.core.actor import Actor``).
    """
    import importlib.util

    arbol = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    out: set[str] = set()
    for nodo in ast.walk(arbol):
        if isinstance(nodo, ast.Import):
            out |= {a.name for a in nodo.names}
        elif isinstance(nodo, ast.ImportFrom) and nodo.module and nodo.level == 0:
            for alias in nodo.names:
                candidato = f"{nodo.module}.{alias.name}"
                try:
                    es_modulo = importlib.util.find_spec(candidato) is not None
                except (ImportError, ModuleNotFoundError, ValueError):
                    es_modulo = False
                out.add(candidato if es_modulo else nodo.module)
    return out


def test_the_package_has_modules():
    """Sin esto, un paquete vacío haría pasar todos los demás tests del archivo."""
    assert len(_modulos()) >= 6


@pytest.mark.parametrize("path", _modulos(), ids=lambda p: p.name)
def test_no_module_imports_outside_the_allowlist(path):
    ajenos = {
        m
        for m in _imports_de(path)
        if m.startswith("app.") and not m.startswith("app.mcp") and m not in PERMITIDOS
    }
    assert not ajenos, (
        f"{path.relative_to(_RAIZ)} importa módulos del proyecto que no están en la "
        f"allowlist: {sorted(ajenos)}. Si hace falta, agregalo a PERMITIDOS "
        "explícitamente — el default es que no."
    )


@pytest.mark.parametrize("path", _modulos(), ids=lambda p: p.name)
def test_no_module_touches_the_engine_layer(path):
    prohibidos = {m for m in _imports_de(path) if m in NUNCA}
    assert not prohibidos, (
        f"{path.relative_to(_RAIZ)} importa la capa de motor: {sorted(prohibidos)}"
    )


def test_the_allowlist_and_the_blocklist_do_not_overlap():
    """
    Si un módulo estuviera en las dos, el guard diría cosas contradictorias según cuál chequeo
    corra primero — y alguien lo "arreglaría" borrando el chequeo estricto.
    """
    assert not (PERMITIDOS & NUNCA)


def test_the_blocklist_names_real_modules():
    """
    Una entrada que ya no corresponde a ningún módulo es una protección que se cree activa y no
    lo está: el import que iba a frenar hoy se llama de otra forma.
    """
    import importlib.util

    for nombre in NUNCA:
        assert importlib.util.find_spec(nombre) is not None, f"{nombre} no existe"
