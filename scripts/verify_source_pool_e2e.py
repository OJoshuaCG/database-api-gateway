"""
Verificación end-to-end MANUAL del POOL de la conexión de ORIGEN, contra un motor REAL.

La fase de datos del clon abría una conexión nueva al origen POR TABLA (``NullPool``): con 103
tablas eran 103 handshakes completos —``getaddrinfo`` sin caché incluido— contra la base de
producción de un tercero. En una medición real el costo fijo por tabla (~350 ms) dominaba el
tiempo de la copia muy por encima de los datos en sí: una tabla de 7 filas tardaba 581 ms y una
de 0 filas, 389 ms.

``pooled_source_scope`` sostiene UNA conexión mientras dura la fase. Lo que este script verifica
es justamente lo que NINGÚN test unitario puede: los tests de ``test_data_copy`` corren sobre
SQLite, donde no hay pool de red, ni handshake, ni ``PROCESSLIST``.

Las tres propiedades que importan:
  1. Sin el scope, cada ``database_connection`` estrena conexión (la línea de base: es lo que
     hacía la fase de datos, una por tabla).
  2. Dentro del scope hay UNA sola conexión, y todas las lecturas van por ella — incluidas las
     que ocurren en el hilo escritor del FIFO.
  3. Al salir del scope NO queda ninguna conexión ``sleep`` en el servidor ajeno, que es la
     condición que hace aceptable la excepción al ``NullPool`` de todo el módulo.

NO es un test de pytest (requiere Docker; se ejecuta a mano).

Uso:
    docker run -d --rm --name gw_pool_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13401:3306 mysql:8.0
    PYTHONPATH=. uv run python scripts/verify_source_pool_e2e.py
"""

import os
import sys
import tempfile
import threading
import time

_TMP = tempfile.mkdtemp(prefix="e2e_gw_pool_")
os.environ.update({
    "DB_ENGINE": "sqlite",
    "DB_NAME": os.path.join(_TMP, "gw.db"),
    "SECRET_KEY": "e2e-secret",
    "CRYPTO_KEY_SALT": "e2e-salt",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "admin123",
    "APP_ENV": "development",
    "LOGGER_MIDDLEWARE_ENABLED": "False",
    "REMOTE_SSRF_GUARD_ENABLED": "False",
    "REMOTE_SSL_MODE": "disable",
})

from sqlalchemy import text  # noqa: E402

from app.core.remote_engine import (  # noqa: E402
    ServerTarget,
    database_connection,
    pooled_source_scope,
)

PORT = int(os.getenv("POOL_E2E_PORT", "13401"))
TABLAS = 103  # las mismas que la base real que motivó esto

failures: list[str] = []


def check(nombre: str, ok: bool, detalle: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FALLA'}  {nombre}{'' if ok else f' — {detalle}'}")
    if not ok:
        failures.append(nombre)


def main() -> int:
    target = ServerTarget(
        server_id=1, dialect="mysql", host="127.0.0.1", port=PORT,
        admin_user="root", admin_password="rootpw", ssl_mode="disable",
    )

    def conn_id_propia() -> int:
        with database_connection(target, "mysql", bulk=True) as c:
            return c.execute(text("SELECT CONNECTION_ID()")).scalar()

    print("\n== Línea de base: sin el scope, cada lectura estrena conexión ==")
    ids = [conn_id_propia() for _ in range(4)]
    check("4 aperturas -> 4 conexiones distintas", len(set(ids)) == 4, str(ids))

    print("\n== Dentro del scope: UNA sola conexión para todas las tablas ==")
    with pooled_source_scope(target, "mysql", bulk=True) as src:
        vistos = {src.execute(text("SELECT CONNECTION_ID()")).scalar() for _ in range(4)}
        check("4 lecturas -> 1 sola conexión", len(vistos) == 1, str(vistos))

        # El writer de MySQL lee el origen desde el hilo del FIFO: se comprueba que la conexión
        # prestada funcione ahí, que es la propiedad de la que depende todo el diseño.
        desde_hilo: list[int] = []

        def leer_en_hilo():
            desde_hilo.append(src.execute(text("SELECT CONNECTION_ID()")).scalar())

        h = threading.Thread(target=leer_en_hilo)
        h.start()
        h.join()
        check("la misma conexión sirve desde el hilo escritor",
              desde_hilo == list(vistos), f"{desde_hilo} vs {vistos}")
        viva = vistos.pop()

    print("\n== Al salir del scope no queda una conexión sleep en el servidor ajeno ==")
    time.sleep(0.5)
    with database_connection(target, "mysql") as c:
        sigue = c.execute(
            text("SELECT ID FROM information_schema.PROCESSLIST WHERE ID = :i"),
            {"i": viva},
        ).fetchall()
    check("la conexión del scope está cerrada", not sigue, f"sigue viva: {viva}")

    print(f"\n== Costo del handshake, {TABLAS} tablas ==")
    t0 = time.perf_counter()
    for _ in range(TABLAS):
        conn_id_propia()
    sin = (time.perf_counter() - t0) * 1000
    with pooled_source_scope(target, "mysql", bulk=True) as src:
        t0 = time.perf_counter()
        for _ in range(TABLAS):
            src.execute(text("SELECT 1"))
        con = (time.perf_counter() - t0) * 1000
    print(f"  una conexión por tabla: {sin:7.0f} ms  ({sin / TABLAS:.1f} ms/tabla)")
    print(f"  conexión sostenida    : {con:7.0f} ms  ({con / TABLAS:.1f} ms/tabla)")
    check("sostener la conexión es más barato", con < sin, f"{con:.0f} vs {sin:.0f}")
    print(
        "  (contra localhost el RTT es ~0 y no hay DNS: sobre un enlace remoto con\n"
        "   getaddrinfo sin caché, el ahorro por tabla es de otro orden)"
    )

    print("\n" + "=" * 70)
    if failures:
        print(f"FALLARON {len(failures)}: " + ", ".join(failures))
        return 1
    print("TODO OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
