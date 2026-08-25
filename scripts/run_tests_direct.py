"""
Ejecutor directo de tests, SIN pytest.

Este repo NO corre ``pytest`` (ver "Ejecución de tests (pytest) — NO por defecto" en
``CLAUDE.md``: la suite tarda varios minutos y correrla como chequeo automático genera carga
real en la máquina de quien trabaja). Mientras esa política esté en pie, **este script es LA
herramienta de verificación**, y por eso vive en el repo en vez de reescribirse por sesión.

Uso::

    .venv/bin/python scripts/run_tests_direct.py tests.test_api_servers
    .venv/bin/python scripts/run_tests_direct.py tests.test_health cors   # filtro por nombre

Salida: una línea por test (``ok`` / ``FALLA`` / ``skip``), el total, y el traceback completo
de cada falla. Código de salida 1 si hubo alguna falla.

REGLA DE DISEÑO: no reimplementar nada de pytest que se pueda CARGAR del repo
-----------------------------------------------------------------------------
Las fixtures salen de ``tests/conftest.py`` y del módulo de test, nunca de una copia escrita
acá. Esa fue la causa raíz de los huecos más caros de la versión efímera anterior: una
``server_payload`` fabricada con valores distintos a la real dejó **dos aserciones de
seguridad sin poder fallar** — la que verifica que la contraseña no se filtre buscaba un
string que el payload falso jamás enviaba, y la del bloqueo de loopback usaba un host que no
es loopback. No solo dejó tests sin correr: cambió en silencio lo que verificaban, y
``server_payload`` la usan 56 tests en 12 archivos.

Los cinco huecos que este script cubre, todos encontrados en producción de la herramienta:

1. Construía el ``TestClient`` para TODOS los tests ⇒ ``create_all`` como efecto secundario,
   y un test que dependiera del esquema sin declararlo pasaba acá y fallaba bajo pytest.
2. Fabricaba fixtures en vez de cargarlas (ver arriba).
3. No soportaba fixtures LOCALES del módulo ⇒ el archivo abortaba entero.
4. No expandía ``@pytest.mark.parametrize`` ni ``@pytest.fixture(params=...)`` ⇒ ídem.
5. Un ``pytest.skip`` **abortaba el archivo entero**: ``Skipped`` deriva de
   ``BaseException``, así que el ``except Exception`` del bucle no lo veía y la excepción se
   escapaba de ``run()`` matando el proceso — los tests posteriores al que saltó nunca
   corrían.

Y un sexto que no es hueco sino orden: el login tiene que ser PEREZOSO. Hacerlo siempre que
exista el cliente le da sesión a ``test_requires_auth(client)``, que verifica justamente el
401. Y la versión intermedia de este arnés lo tenía **medio corregido**: no logueaba al
construir el cliente, pero sí al resolver cualquier fixture cuya cadena mencionara ``client``
—y ``client`` es el ANÓNIMO—, así que el mismo defecto seguía vivo un nivel más abajo. La
forma correcta es no tratar ``admin_client`` aparte: resolverlo como una fixture cualquiera
hace que el login ocurra si y solo si alguien lo pidió.

LÍMITES CONOCIDOS (no finjas que no están)
------------------------------------------
- Una fixture con ``params=[...]`` se resuelve con su PRIMER valor: el test NO se multiplica
  por sus parámetros como haría pytest. Los ``@pytest.mark.parametrize`` del test sí se
  expanden. Hoy esto afecta a **un solo archivo**, ``tests/test_grants_integration.py`` (su
  fixture ``target`` parametriza los tres motores), y ahí sus dos tests se saltean igual
  cuando los motores no están alcanzables — o sea que la cobertura perdida es cero mientras
  no haya Docker. Si aparece un segundo archivo, esto pasa a valer la pena de implementar.
- De ``request`` solo existe ``.param``.
- No hay ``conftest.py`` por directorio, ni fixtures de scope ``session``/``module`` (todo se
  resuelve por test), ni marcas más allá de ``parametrize``.

Cuando un test nuevo necesite algo de esta lista, la respuesta correcta es AGREGARLO acá y
sumarle un caso a ``tests/test_run_tests_direct.py`` — no volver a escribir un arnés propio en
un scratchpad, que es cómo volvieron los huecos 3 y 4.
"""

import contextlib
import importlib
import inspect
import pathlib
import sys
import tempfile
import traceback

# La RAÍZ DEL REPO derivada de la ubicación de ESTE archivo, no de `os.getcwd()`. Hace falta
# para importar `tests.conftest` y `app.*`, y con el cwd el script solo funcionaba invocado
# desde la raíz: desde cualquier otro directorio moría con `No module named 'tests'`.
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

# NO escribir `.pyc`. Cuesta ~7% de arranque (≈0,05 s por archivo) y cierra una trampa que ya
# produjo una auditoría entera con resultados falsos.
#
# La verificación por MUTACIÓN —mutar un archivo de producción, correr, restaurar— es la forma
# de probar que un test cubre lo que dice. Pero Python valida el `.pyc` contra el par
# (mtime del fuente **en segundos enteros**, tamaño en bytes). Una mutación que intercambia dos
# líneas deja el tamaño IDÉNTICO, y un script que muta, corre y restaura lo hace dentro del
# mismo segundo: el par no cambia, así que Python **nunca recompila** y todo proceso posterior
# carga el bytecode MUTADO desde un fuente que `git diff` reporta limpio.
#
# Pasó exactamente así al verificar `test_order_statements_respects_class_order`: el `.pyc`
# quedó con `_CLASS_ORDER` invertido y la auditoría siguiente reportó dos tests rojos —uno de
# ellos sano— sobre código intacto. Es un peligro específico de scripts, no de humanos: a mano
# nadie muta y restaura en menos de un segundo.

sys.dont_write_bytecode = True

# conftest.py fija el entorno al importarse (environments.py lo lee AL IMPORTAR), así que
# importarlo primero es también la forma de heredar ESA configuración en vez de copiarla.
#
# Se busca en `sys.modules` ANTES de importarlo, y eso no es microoptimización: es
# corrección. `tests/` no es un paquete (no tiene `__init__.py`), así que pytest lo importa
# como `conftest` mientras un `import tests.conftest` crea una SEGUNDA instancia del mismo
# archivo — con su propio `tempfile.mkdtemp()`, que pisa `DB_NAME` en el entorno a mitad de
# la sesión. `app/core/environments.py` ya leyó el valor viejo, así que el resultado es un
# directorio temporal huérfano y dos módulos que dicen cosas distintas sobre dónde está la
# BD. Pasa justo cuando `tests/test_run_tests_direct.py` corre BAJO pytest e importa este
# arnés. Reutilizar el que ya está cargado lo evita, y sin pytest cae al import normal.
conftest = sys.modules.get("conftest") or sys.modules.get("tests.conftest")
if conftest is None:
    import tests.conftest as conftest  # noqa: E402

from _pytest.monkeypatch import MonkeyPatch  # noqa: E402
from _pytest.outcomes import Skipped  # noqa: E402


def _raw(f):
    """
    La función CRUDA detrás de `@pytest.fixture` (pytest prohíbe llamarla directo).

    Los nombres cambian entre versiones de pytest, así que se prueban las tres formas: en
    pytest 9 el decorador devuelve un `FixtureFunctionDefinition` con `_fixture_function`;
    antes era `__pytest_wrapped__.obj` o `__wrapped__`.
    """
    if f is None:
        return None
    ff = getattr(f, "_fixture_function", None)
    if ff is not None:
        return ff
    w = getattr(f, "__pytest_wrapped__", None)
    return getattr(w, "obj", None) or getattr(f, "__wrapped__", None) or f


def _find_fixture(mod, nombre):
    """Fixture por nombre: módulo de test primero, después conftest. Nunca inventada."""
    for fuente in (mod, conftest):
        f = getattr(fuente, nombre, None)
        if f is not None and (_raw(f) is not None and callable(_raw(f))):
            return _raw(f), f
    return None, None


def _fixture_params(holder):
    """Los `params=[...]` de `@pytest.fixture(params=...)`, o `[None]` si no tiene."""
    for attr in ("_fixture_function_marker", "_pytestfixturefunction"):
        fm = getattr(holder, attr, None)
        ps = getattr(fm, "params", None) if fm is not None else None
        if ps:
            return list(ps)
    return [None]


class _Request:
    """Lo mínimo de la fixture `request` que este repo usa: `.param`."""

    def __init__(self, param=None):
        self.param = param


def _parametrize_cases(fn):
    """Producto cartesiano de las marcas `@pytest.mark.parametrize`."""
    marcas = [m for m in getattr(fn, "pytestmark", []) if m.name == "parametrize"]
    casos = [{}]
    for m in reversed(marcas):
        argnames, argvalues = m.args[0], m.args[1]
        nombres = ([a.strip() for a in argnames.split(",")]
                   if isinstance(argnames, str) else list(argnames))
        nuevos = []
        for base in casos:
            for v in argvalues:
                vals = list(v.values) if hasattr(v, "values") else (
                    list(v) if len(nombres) > 1 else [v])
                nuevos.append({**base, **dict(zip(nombres, vals))})
        casos = nuevos
    return casos


def _resolve(mod, nombre, cache, stack, test_name, param=None):
    """
    Resuelve una fixture recursivamente. Las generadoras entran en una ExitStack para que su
    teardown corra, como en pytest.
    """
    if nombre in cache:
        return cache[nombre]
    if nombre == "request":
        return _Request(param)
    if nombre == "tmp_path":
        import pathlib
        cache[nombre] = pathlib.Path(tempfile.mkdtemp(prefix="gw_tmp_"))
        return cache[nombre]
    fn, holder = _find_fixture(mod, nombre)
    if fn is None or not callable(fn):
        raise RuntimeError(f"fixture no soportada: {nombre} (en {test_name})")
    # Una fixture con `params=` se resuelve con su PRIMER valor: el arnés no multiplica el
    # test por sus parámetros (pytest sí). Se declara acá para no fingir cobertura.
    p0 = _fixture_params(holder)[0]
    deps = fn.__code__.co_varnames[: fn.__code__.co_argcount]
    args = {d: _resolve(mod, d, cache, stack, test_name, p0) for d in deps}
    val = (stack.enter_context(contextlib.contextmanager(fn)(**args))
           if inspect.isgeneratorfunction(fn) else fn(**args))
    cache[nombre] = val
    return val


def _needs_client(mod, params, vistos=None):
    """¿Algún eslabón de la cadena de fixtures pide `client` o `admin_client`?"""
    vistos = vistos or set()
    for p in params:
        if p in ("client", "admin_client"):
            return True
        if p in vistos or p in ("request", "tmp_path", "monkeypatch"):
            continue
        vistos.add(p)
        f, _ = _find_fixture(mod, p)
        if f is not None and hasattr(f, "__code__"):
            if _needs_client(mod, f.__code__.co_varnames[: f.__code__.co_argcount], vistos):
                return True
    return False


def run(mod_name, prefixes):
    mod = importlib.import_module(mod_name)
    names = [n for n in dir(mod) if n.startswith("test_")]
    if prefixes:
        names = [n for n in names if any(n.startswith(p) or p in n for p in prefixes)]
    names.sort(key=lambda n: getattr(mod, n).__code__.co_firstlineno)

    ok = fail = skip = 0
    fallos = []
    for name in names:
        fn = getattr(mod, name)
        params = fn.__code__.co_varnames[: fn.__code__.co_argcount]
        casos = _parametrize_cases(fn)
        for i, caso in enumerate(casos):
            etiqueta = name if len(casos) == 1 else f"{name}[{i}]"
            mp = MonkeyPatch()
            pendientes = [p for p in params if p not in caso]
            cm = (_client_ctx() if _needs_client(mod, pendientes)
                  else contextlib.nullcontext())
            try:
                with cm as c, contextlib.ExitStack() as stack:
                    cache = {"monkeypatch": mp}
                    if c is not None:
                        cache["client"] = c
                    kwargs = dict(caso)
                    for p in pendientes:
                        # `admin_client` NO se trata aparte: es una fixture de `conftest`
                        # como cualquier otra, y resolverla es lo que hace el login. Así el
                        # login ocurre si y solo si algo lo pidió — la semántica de pytest.
                        kwargs[p] = _resolve(mod, p, cache, stack, name)
                    fn(**kwargs)
                ok += 1
                print(f"  ok    {etiqueta}")
            except Skipped as exc:
                skip += 1
                print(f"  skip  {etiqueta}: {exc}")
            except Exception as exc:
                fail += 1
                fallos.append((etiqueta, traceback.format_exc()))
                print(f"  FALLA {etiqueta}: {type(exc).__name__}: {exc}")
            finally:
                mp.undo()

    extra = f" ({skip} skip)" if skip else ""
    print(f"\n{ok}/{ok + fail} en {mod_name}{extra}")
    for n, tb in fallos:
        print(f"\n=== {n} ===\n{tb}")
    return fail


@contextlib.contextmanager
def _client_ctx():
    """El `client` REAL de conftest, con su setup y teardown."""
    gen = _raw(conftest.client)()
    c = next(gen)
    try:
        yield c
    finally:
        with contextlib.suppress(StopIteration):
            next(gen)


if __name__ == "__main__":
    sys.exit(1 if run(sys.argv[1], sys.argv[2:]) else 0)
