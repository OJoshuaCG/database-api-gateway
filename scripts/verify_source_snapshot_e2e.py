r"""
Verificación end-to-end MANUAL de que el clon NO produce un destino referencialmente inválido.

EL DEFECTO. La fase de datos leía cada tabla en ``READ COMMITTED`` con su propio read view: N
tablas, N fotos distintas del origen. En el destino las FKs se apagan durante toda la fase y
**nunca se vuelven a validar**. Si entre que se copia ``padre`` y se copia ``hijo`` el origen
inserta las dos filas, el destino queda con un hijo huérfano — y el clon lo reporta ``applied``.

No es "no es point-in-time": es **puede producir datos inválidos y decir que salió bien**.

EL ARREGLO. ``pooled_source_scope(consistent=True)`` sostiene una sola conexión con
``START TRANSACTION WITH CONSISTENT SNAPSHOT``, así que las N tablas salen de la misma foto.

Este script escribe en el ORIGEN a mitad de la copia —justo entre una tabla y la siguiente— y
comprueba las dos cosas: que sin snapshot el destino queda roto, y que con snapshot no.

NO es un test de pytest (requiere Docker; se ejecuta a mano).

Uso:
    docker run -d --rm --name gw_snap_mysql -e MYSQL_ROOT_PASSWORD=rootpw \
        -e MYSQL_ROOT_HOST=% -p 13401:3306 mysql:8.0
    docker exec gw_snap_mysql mysql -uroot -prootpw -e "SET GLOBAL local_infile=1"
    PYTHONPATH=. uv run python scripts/verify_source_snapshot_e2e.py
"""

import os
import sys
import tempfile

_TMP = tempfile.mkdtemp(prefix="e2e_gw_snapc_")
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

from contextlib import contextmanager  # noqa: E402

from sqlalchemy import create_engine, text  # noqa: E402

import app.services.db_admin.data_copy as dc  # noqa: E402
from app.core.remote_engine import ServerTarget  # noqa: E402
from app.services.db_admin.data_copy import TableCopySpec, copy_tables  # noqa: E402

PORT = int(os.getenv("SNAPC_E2E_PORT", "13401"))
URL = f"mysql+pymysql://root:rootpw@127.0.0.1:{PORT}"
SRC, DST = "gw_snapc_src", "gw_snapc_dst"

failures: list[str] = []


def check(nombre: str, ok: bool, detalle: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FALLA'}  {nombre}{'' if ok else f' — {detalle}'}")
    if not ok:
        failures.append(nombre)


def preparar() -> None:
    e = create_engine(URL, isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        for db in (SRC, DST):
            c.execute(text(f"DROP DATABASE IF EXISTS {db}"))
            c.execute(text(f"CREATE DATABASE {db} CHARACTER SET utf8mb4"))
    e.dispose()
    for db in (SRC, DST):
        eng = create_engine(f"{URL}/{db}", isolation_level="AUTOCOMMIT")
        with eng.connect() as c:
            c.execute(text("CREATE TABLE padre (id INT PRIMARY KEY) ENGINE=InnoDB"))
            c.execute(text(
                "CREATE TABLE hijo (id INT PRIMARY KEY, pid INT, "
                "CONSTRAINT fk_h FOREIGN KEY (pid) REFERENCES padre(id)) ENGINE=InnoDB"
            ))
        eng.dispose()
    eng = create_engine(f"{URL}/{SRC}", isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text("INSERT INTO padre VALUES (1)"))
        c.execute(text("INSERT INTO hijo VALUES (1,1)"))
    eng.dispose()


def escribir_en_el_origen() -> None:
    """El cliente sigue trabajando: inserta un padre con su hijo, atómicamente."""
    eng = create_engine(f"{URL}/{SRC}", isolation_level="AUTOCOMMIT")
    with eng.connect() as c:
        c.execute(text("INSERT INTO padre VALUES (2)"))
        c.execute(text("INSERT INTO hijo VALUES (2,2)"))
    eng.dispose()


def contar(db: str, tabla: str) -> int:
    eng = create_engine(f"{URL}/{db}")
    with eng.connect() as c:
        n = c.execute(text(f"SELECT COUNT(*) FROM {tabla}")).scalar()
    eng.dispose()
    return n


def correr(modo: str) -> tuple[int, int]:
    """
    Copia padre e hijo, escribiendo en el ORIGEN entre una tabla y la otra.

    ``modo``:
      - ``"por_tabla"``  reproduce el comportamiento VIEJO: una conexión nueva por tabla, en
        READ COMMITTED. Es el que produce el huérfano.
      - ``"sostenida"``  conexión sostenida sin snapshot explícito. Resulta coherente igual,
        porque el default de MySQL es REPEATABLE READ y el read view se fija en la primera
        lectura — pero es coherencia por ACCIDENTE, dependiente del default del servidor.
      - ``"snapshot"``   conexión sostenida con START TRANSACTION WITH CONSISTENT SNAPSHOT:
        coherente por CONSTRUCCIÓN, sin depender de cómo esté configurado el origen.
    """
    preparar()
    target = ServerTarget(
        server_id=1, dialect="mysql", host="127.0.0.1", port=PORT,
        admin_user="root", admin_password="rootpw", ssl_mode="disable",
    )
    # El gancho: se dispara cuando `padre` terminó y `hijo` todavía no empezó.
    original = dc._copy_one_table
    disparado = [False]

    def con_escritura(spec, **kw):
        res = original(spec, **kw)
        if spec.table == "padre" and not disparado[0]:
            disparado[0] = True
            escribir_en_el_origen()
        return res

    dc._copy_one_table = con_escritura
    dc.CLONE_CONSISTENT_SNAPSHOT = modo == "snapshot"

    scope_original = dc.pooled_source_scope
    if modo == "por_tabla":
        # Sin conexión prestada, cada tabla abre la suya con READ COMMITTED: el camino viejo.
        @contextmanager
        def sin_prestar(*a, **kw):
            yield None

        dc.pooled_source_scope = sin_prestar
    try:
        copy_tables(
            source_target=target, source_db=SRC, source_engine="mysql",
            dest_target=target, dest_db=DST, dest_engine="mysql",
            specs=[
                TableCopySpec(table="padre", columns=["id"], primary_key=["id"]),
                TableCopySpec(table="hijo", columns=["id", "pid"], primary_key=["id"]),
            ],
        )
    finally:
        dc._copy_one_table = original
        dc.pooled_source_scope = scope_original
    return contar(DST, "padre"), contar(DST, "hijo")


def main() -> int:
    print("\n== Comportamiento VIEJO: una conexión por tabla, READ COMMITTED ==")
    p, h = correr("por_tabla")
    print(f"  destino: padre={p} hijo={h}")
    check(
        "reproduce el defecto: el destino queda con un hijo huérfano",
        h > p,
        f"padre={p} hijo={h} — si son iguales, el escenario no se disparó y el test no prueba nada",
    )

    print("\n== Conexión sostenida SIN snapshot explícito ==")
    p, h = correr("sostenida")
    print(f"  destino: padre={p} hijo={h}")
    check(
        "sale coherente, pero por el REPEATABLE READ default del servidor",
        p == h == 1, f"padre={p} hijo={h}",
    )

    print("\n== CON snapshot consistente (el arreglo) ==")
    p, h = correr("snapshot")
    print(f"  destino: padre={p} hijo={h}")
    check("las dos tablas salen de la MISMA foto", p == h == 1, f"padre={p} hijo={h}")

    print("\n== Y las FKs del destino se validan de verdad ==")
    eng = create_engine(f"{URL}/{DST}", isolation_level="AUTOCOMMIT")
    try:
        with eng.connect() as c:
            # Si quedara un huérfano, este chequeo lo encontraría.
            huerfanos = c.execute(text(
                "SELECT COUNT(*) FROM hijo h LEFT JOIN padre p ON h.pid = p.id "
                "WHERE p.id IS NULL"
            )).scalar()
        check("cero filas huérfanas en el destino", huerfanos == 0, f"{huerfanos} huérfanas")
    finally:
        eng.dispose()

    print("\n" + "=" * 70)
    if failures:
        print(f"FALLARON {len(failures)}: " + ", ".join(failures))
        return 1
    print("TODO OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
