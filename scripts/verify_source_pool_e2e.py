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

Las cuatro propiedades que importan:
  1. Sin pool, cada ``with`` estrena conexión (el comportamiento de hoy, como línea de base).
  2. Con pool, N tablas reusan UNA sola conexión.
  3. ``dispose()`` a mitad estrena una nueva — es el camino de fallo, y sin él una tabla
     cancelada dejaría un cursor a medio drenar envenenando a la siguiente.
  4. Al salir del scope NO queda ninguna conexión ``sleep`` en el servidor ajeno, que es la
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

    def conn_id(*, pooled: bool) -> int:
        with database_connection(target, "mysql", bulk=True, pooled=pooled) as c:
            return c.execute(text("SELECT CONNECTION_ID()")).scalar()

    print("\n== Línea de base: sin pool, cada tabla estrena conexión ==")
    ids = [conn_id(pooled=False) for _ in range(4)]
    check("4 aperturas -> 4 conexiones distintas", len(set(ids)) == 4, str(ids))

    print("\n== Con pool: las tablas comparten UNA conexión ==")
    with pooled_source_scope(target, "mysql", bulk=True) as pool:
        ids = [conn_id(pooled=True) for _ in range(4)]
        check("4 aperturas -> 1 sola conexión", len(set(ids)) == 1, str(set(ids)))

        print("\n== Camino de fallo: dispose() tira la conexión sucia ==")
        antes = conn_id(pooled=True)
        pool.dispose()
        despues = conn_id(pooled=True)
        check("tras dispose() la siguiente tabla estrena conexión", antes != despues,
              f"{antes} == {despues}")
        viva = despues

    print("\n== Al salir del scope no queda una conexión sleep en el servidor ajeno ==")
    time.sleep(0.5)
    with database_connection(target, "mysql") as c:
        sigue = c.execute(
            text("SELECT ID FROM information_schema.PROCESSLIST WHERE ID = :i"),
            {"i": viva},
        ).fetchall()
    check("la conexión del pool está cerrada", not sigue, f"sigue viva: {viva}")

    print(f"\n== Costo del handshake, {TABLAS} tablas ==")

    def corrida(pooled: bool) -> float:
        t0 = time.perf_counter()
        for _ in range(TABLAS):
            with database_connection(target, "mysql", bulk=True, pooled=pooled) as c:
                c.execute(text("SELECT 1"))
        return (time.perf_counter() - t0) * 1000

    sin = corrida(False)
    with pooled_source_scope(target, "mysql", bulk=True):
        con = corrida(True)
    print(f"  sin pool: {sin:7.0f} ms  ({sin / TABLAS:.1f} ms/tabla)")
    print(f"  con pool: {con:7.0f} ms  ({con / TABLAS:.1f} ms/tabla)")
    check("el pool es más barato", con < sin, f"{con:.0f} vs {sin:.0f}")
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
