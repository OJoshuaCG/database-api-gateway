"""
Tests del propio arnés de verificación (``scripts/run_tests_direct.py``).

POR QUÉ EXISTE ESTE ARCHIVO
---------------------------
El repo no corre ``pytest`` por política, así que ese script es LA herramienta con la que se
verifica todo. Antes vivía en un scratchpad y se reescribía por sesión: acumuló **cinco
huecos**, y dos de ellos volvieron después de haber sido corregidos porque el parche se perdía
con la sesión.

El más caro no fue un test que no corría, sino uno que corría **verificando otra cosa**: el
arnés fabricaba ``server_payload`` a mano con valores distintos de los de ``conftest``, así que
``assert "supersecret" not in r.text`` buscaba un string que su payload jamás enviaba, y el
test del bloqueo de loopback usaba un host que no es loopback. Dos aserciones de seguridad sin
poder fallar, y ``server_payload`` la usan 56 tests en 12 archivos.

Cada test de acá corresponde a un hueco concreto y **falla si el hueco se reintroduce**. Están
verificados por mutación, no solo por su verde.
"""

import sys
import types

import pytest

sys.path.insert(0, "scripts")

import run_tests_direct as arnes  # noqa: E402


def _modulo_sintetico(nombre, **objetos):
    """Un módulo importable armado al vuelo, para ejercitar ``arnes.run`` de punta a punta."""
    mod = types.ModuleType(nombre)
    for k, v in objetos.items():
        setattr(mod, k, v)
    sys.modules[nombre] = mod
    return mod


# --------------------------------------------------------------------------- #
# Hueco 2 — el más caro: fabricar fixtures en vez de cargarlas                 #
# --------------------------------------------------------------------------- #
def test_server_payload_sale_del_conftest_real():
    """
    Los valores tienen que ser los de ``tests/conftest.py``, no unos parecidos.

    Se anclan los DOS que sostienen aserciones de seguridad: ``root_password`` es el string
    exacto que ``test_create_server_does_not_leak_password`` busca en la respuesta, y ``host``
    tiene que ser loopback para que ``test_create_server_blocks_loopback_via_api`` pruebe lo
    que dice. Con valores inventados los dos tests pasan sin verificar nada.
    """
    mod = _modulo_sintetico("_arnes_vacio")
    fn, _ = arnes._find_fixture(mod, "server_payload")
    assert fn is not None, "no encontró server_payload en el conftest real"

    payload = fn()()
    assert payload["root_password"] == "supersecret"
    assert payload["host"] == "127.0.0.1"
    assert payload["port"] == 3399
    assert payload["name"] == "srv-test"


def test_la_fixture_local_del_modulo_gana_sobre_el_conftest():
    """Orden de búsqueda: módulo de test primero, conftest después."""
    @pytest.fixture
    def server_payload():
        return "la del modulo"

    mod = _modulo_sintetico("_arnes_override", server_payload=server_payload)
    fn, _ = arnes._find_fixture(mod, "server_payload")
    assert fn() == "la del modulo"


# --------------------------------------------------------------------------- #
# Hueco 1 — TestClient incondicional                                          #
# --------------------------------------------------------------------------- #
def test_no_construye_cliente_si_la_cadena_no_lo_declara():
    """
    Construirlo siempre corre ``create_all`` como efecto secundario, así que un test que
    dependa del esquema del gateway sin pedirlo pasa acá y falla bajo pytest. Pasó de verdad:
    dos tests reportados como "19/19" eran verde falso.
    """
    mod = _modulo_sintetico("_arnes_sin_cliente")
    assert arnes._needs_client(mod, ["monkeypatch"]) is False
    assert arnes._needs_client(mod, []) is False
    assert arnes._needs_client(mod, ["client"]) is True
    assert arnes._needs_client(mod, ["admin_client"]) is True


def test_detecta_el_cliente_a_traves_de_una_fixture_local():
    """La dependencia puede estar a dos saltos: se sigue la cadena, no solo el primer nivel."""
    @pytest.fixture
    def intermedia(client):
        return client

    @pytest.fixture
    def de_afuera(intermedia):
        return intermedia

    mod = _modulo_sintetico("_arnes_cadena", intermedia=intermedia, de_afuera=de_afuera)
    assert arnes._needs_client(mod, ["de_afuera"]) is True


def test_la_deteccion_de_cliente_no_cicla_con_fixtures_mutuas():
    """Un ciclo entre fixtures no debe colgar la detección."""
    @pytest.fixture
    def a(b):
        return b

    @pytest.fixture
    def b(a):
        return a

    mod = _modulo_sintetico("_arnes_ciclo", a=a, b=b)
    assert arnes._needs_client(mod, ["a"]) is False


# --------------------------------------------------------------------------- #
# Hueco 4 — parametrize y fixtures con params                                  #
# --------------------------------------------------------------------------- #
def test_expande_parametrize_simple():
    @pytest.mark.parametrize("valor", [1, 2, 3])
    def un_test(valor):
        pass

    assert arnes._parametrize_cases(un_test) == [{"valor": 1}, {"valor": 2}, {"valor": 3}]


def test_expande_parametrize_de_varios_argumentos_y_apilado():
    @pytest.mark.parametrize("a", [1, 2])
    @pytest.mark.parametrize("b,c", [("x", "y")])
    def un_test(a, b, c):
        pass

    casos = arnes._parametrize_cases(un_test)
    assert len(casos) == 2
    assert {"a": 1, "b": "x", "c": "y"} in casos
    assert {"a": 2, "b": "x", "c": "y"} in casos


def test_sin_parametrize_hay_un_solo_caso():
    def un_test():
        pass

    assert arnes._parametrize_cases(un_test) == [{}]


def test_lee_los_params_de_una_fixture_parametrizada():
    """
    Regresión de VERSIÓN de pytest, no de lógica.

    En pytest 9 el decorador devuelve un ``FixtureFunctionDefinition`` y los ``params`` viven
    en ``_fixture_function_marker``; antes estaban en ``_pytestfixturefunction``. El arnés
    leía solo el nombre viejo, así que ``request.param`` llegaba ``None`` y los dos tests de
    ``test_grants_integration`` morían con ``KeyError: None`` en vez de saltarse.
    """
    @pytest.fixture(params=["mysql", "mariadb", "postgresql"])
    def target(request):
        return request.param

    mod = _modulo_sintetico("_arnes_params", target=target)
    _, holder = arnes._find_fixture(mod, "target")
    assert arnes._fixture_params(holder) == ["mysql", "mariadb", "postgresql"]

    @pytest.fixture
    def simple():
        return 1

    mod = _modulo_sintetico("_arnes_sin_params", simple=simple)
    _, holder = arnes._find_fixture(mod, "simple")
    assert arnes._fixture_params(holder) == [None]


def test_request_param_llega_a_la_fixture():
    """El valor de ``params`` tiene que atravesar ``request.param``, no quedar en ``None``."""
    @pytest.fixture(params=["mysql", "mariadb"])
    def motor(request):
        return request.param

    vistos = []

    def test_usa_motor(motor):
        vistos.append(motor)

    mod = _modulo_sintetico("_arnes_req", motor=motor, test_usa_motor=test_usa_motor)
    assert arnes.run("_arnes_req", []) == 0
    # Se resuelve con el PRIMER param (límite declarado del arnés: no multiplica el test).
    assert vistos == ["mysql"]


# --------------------------------------------------------------------------- #
# Hueco 3 — fixtures locales, y built-ins alcanzables DESDE una local          #
# --------------------------------------------------------------------------- #
def test_una_fixture_local_puede_pedir_una_builtin():
    """
    ``tmp_path`` pedida desde una fixture LOCAL. Sin esto ``test_introspection`` abortaba
    entero (6 tests) porque su fixture local pide ``tmp_path``.
    """
    @pytest.fixture
    def archivo(tmp_path):
        p = tmp_path / "x.txt"
        p.write_text("hola")
        return p

    leido = []

    def test_lee(archivo):
        leido.append(archivo.read_text())

    mod = _modulo_sintetico("_arnes_tmp", archivo=archivo, test_lee=test_lee)
    assert arnes.run("_arnes_tmp", []) == 0
    assert leido == ["hola"]


def test_una_fixture_generadora_corre_su_teardown():
    eventos = []

    @pytest.fixture
    def recurso():
        eventos.append("setup")
        yield "r"
        eventos.append("teardown")

    def test_usa(recurso):
        eventos.append(f"test:{recurso}")

    mod = _modulo_sintetico("_arnes_gen", recurso=recurso, test_usa=test_usa)
    assert arnes.run("_arnes_gen", []) == 0
    assert eventos == ["setup", "test:r", "teardown"]


# --------------------------------------------------------------------------- #
# Hueco 5 — skip no es falla                                                   #
# --------------------------------------------------------------------------- #
def test_un_skip_no_aborta_la_corrida():
    """
    ``tests/test_grants_integration.py`` llama ``pytest.skip`` cuando el motor no está
    alcanzable, y sin manejo explícito eso no era "una falla": era **el archivo entero
    abortado**.

    El motivo es que ``Skipped`` deriva de ``BaseException``, no de ``Exception`` (MRO:
    ``Skipped → OutcomeException → BaseException``), así que el ``except Exception`` del
    bucle no lo ve y la excepción se escapa de ``run()`` matando el proceso — los tests que
    venían después del que saltó no llegan a correr. Convierte "no hay Docker" en "no hay
    resultados".

    El ``except BaseException`` de acá existe para que, si la cláusula ``Skipped`` del arnés
    se rompe, este test dé una FALLA legible en vez de arrastrar la corrida entera con él.
    """
    def test_salta():
        pytest.skip("motor no alcanzable")

    def test_despues_del_skip():
        pass

    mod = _modulo_sintetico(
        "_arnes_skip", test_salta=test_salta, test_despues_del_skip=test_despues_del_skip
    )
    try:
        fallas = arnes.run("_arnes_skip", [])
    except BaseException as exc:  # noqa: BLE001 - ver docstring
        raise AssertionError(
            f"el skip se escapó de run() y habría abortado el archivo: {exc!r}"
        ) from exc
    assert fallas == 0


def test_una_excepcion_de_verdad_si_cuenta_como_falla():
    """El contrapeso del test anterior: no vale tragarse los errores para que dé verde."""
    def test_rompe():
        raise AssertionError("esto es un bug")

    mod = _modulo_sintetico("_arnes_falla", test_rompe=test_rompe)
    assert arnes.run("_arnes_falla", []) == 1


# --------------------------------------------------------------------------- #
# El sexto detalle: el login es PEREZOSO                                       #
# --------------------------------------------------------------------------- #
def test_pedir_client_no_deja_sesion_iniciada():
    """
    ``test_requires_auth(client)`` verifica el 401. Si el arnés loguea cada vez que construye
    el cliente, ese test recibe una sesión válida y **deja de poder fallar**.
    """
    codigos = []

    def test_sin_sesion(client):
        codigos.append(client.get("/api/v1/servers").status_code)

    mod = _modulo_sintetico("_arnes_anon", test_sin_sesion=test_sin_sesion)
    assert arnes.run("_arnes_anon", []) == 0
    assert codigos == [401], f"esperaba 401 sin login, salió {codigos}"


def test_una_fixture_local_que_pide_client_tampoco_recibe_sesion():
    """
    El hueco 6, un nivel más abajo — donde estuvo vivo después de "corregirlo".

    La versión intermedia del arnés no logueaba al construir el cliente (eso ya estaba
    arreglado), pero sí lo hacía al resolver **cualquier** fixture cuya cadena mencionara
    ``client``. Y ``client`` es el ANÓNIMO: ``admin_client`` es el autenticado. Así que una
    fixture local que pide el cliente sin sesión —para probar un 401, un 403, o el
    comportamiento de un endpoint público— la recibía autenticada.

    Es el mismo modo de fallo que el arnés entero pretende cerrar: la herramienta cambia en
    silencio lo que el test verifica.
    """
    @pytest.fixture
    def anonimo(client):
        return client

    codigos = []

    def test_sin_sesion(anonimo):
        codigos.append(anonimo.get("/api/v1/servers").status_code)

    mod = _modulo_sintetico(
        "_arnes_anon_indirecto", anonimo=anonimo, test_sin_sesion=test_sin_sesion
    )
    assert arnes.run("_arnes_anon_indirecto", []) == 0
    assert codigos == [401], f"esperaba 401 sin login, salió {codigos}"


def test_admin_client_si_queda_autenticado():
    """El contrapeso: quitar el login de más no puede quitar el que sí corresponde."""
    codigos = []

    def test_con_sesion(admin_client):
        codigos.append(admin_client.get("/api/v1/servers").status_code)

    mod = _modulo_sintetico("_arnes_admin", test_con_sesion=test_con_sesion)
    assert arnes.run("_arnes_admin", []) == 0
    assert codigos == [200], f"esperaba 200 con admin_client, salió {codigos}"


# --------------------------------------------------------------------------- #
# La trampa del bytecode cacheado                                              #
# --------------------------------------------------------------------------- #
def test_el_arnes_no_escribe_bytecode():
    """
    ``sys.dont_write_bytecode`` tiene que quedar en ``True`` al importar el arnés.

    No es una preferencia de limpieza: sin eso, la verificación por MUTACIÓN produce
    resultados falsos que sobreviven al restore. Python valida el ``.pyc`` contra el par
    (mtime del fuente **en segundos enteros**, tamaño en bytes); una mutación que intercambia
    dos líneas no cambia el tamaño, y un script que muta, corre y restaura no cambia el
    segundo. El par queda igual, Python no recompila, y los procesos siguientes cargan el
    bytecode MUTADO desde un fuente que ``git diff`` reporta limpio.

    Ya ocurrió: al verificar ``test_order_statements_respects_class_order`` el ``.pyc`` quedó
    con ``_CLASS_ORDER`` invertido, y la auditoría siguiente reportó dos tests rojos —uno de
    ellos perfectamente sano— sobre código intacto.
    """
    assert sys.dont_write_bytecode is True, (
        "el arnés volvió a escribir .pyc: una mutación puede envenenar la caché y sobrevivir "
        "al restore"
    )


# --------------------------------------------------------------------------- #
# Filtro por nombre                                                            #
# --------------------------------------------------------------------------- #
def test_el_filtro_por_nombre_acota_la_corrida():
    corridos = []

    def test_alfa():
        corridos.append("alfa")

    def test_beta():
        corridos.append("beta")

    mod = _modulo_sintetico("_arnes_filtro", test_alfa=test_alfa, test_beta=test_beta)
    assert arnes.run("_arnes_filtro", ["alfa"]) == 0
    assert corridos == ["alfa"]
