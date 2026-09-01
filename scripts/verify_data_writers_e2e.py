r"""
Verificación end-to-end MANUAL de los DOS writers de datos contra un motor REAL.

Es la cobertura que faltaba, y su ausencia costó un bug de producción. Los tests de
``tests/test_data_copy.py`` corren el round-trip sobre SQLite, donde ``_resolve_writer`` SIEMPRE
elige el writer legacy: **el camino FIFO/LOAD DATA —el que corre en producción— no tenía ni un
solo test end-to-end**. Por eso sobrevivió que el TIME se copiara truncado, perdiendo la
fracción de segundo, sin que nada fallara.

Copia la MISMA tabla por los dos caminos y compara el destino contra el origen fila a fila. El
despacho se fuerza por tamaño: pocas filas van por el INSERT extendido, y con 2.000 filas de
relleno la misma tabla se va por el FIFO.

Los valores son los que históricamente rompieron o pueden romper: ``TIME(3)``/``TIME(6)`` con
fracción, TIME negativo con minutos (que ``pymysql.escape_timedelta`` corrompe), ``VARBINARY``
con NUL/tab/newline/backslash, texto con tabuladores y con el literal ``\N``, ``DECIMAL``
negativo y ``ENUM``.

LIMITACIÓN CONOCIDA Y PREEXISTENTE que este script deja a la vista: el camino FIFO **no puede
copiar bytes que no sean UTF-8 válido** (``VARBINARY`` con ``\xff\xfe``) y falla con
``(1300, "Invalid utf8mb4 character string")``. El ``LOAD DATA`` lee el archivo con el charset
de la conexión y valida. Falla RUIDOSAMENTE, así que no hay pérdida silenciosa — pero es una
tabla que no se puede clonar por el camino bulk. Verificado que es anterior a los cambios de
esta serie (contra ``b91fb59`` fallaba igual, y ahí fallaban los DOS caminos). El arreglo
—``CHARACTER SET binary`` en el LOAD DATA— tiene implicancias de conversión de charset entre
origen y destino que merecen su propio análisis, así que queda registrado y no resuelto acá.

NO es un test de pytest (requiere Docker; se ejecuta a mano).

Uso:
    docker run -d --rm --name gw_wr_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13401:3306 mysql:8.0
    docker exec gw_wr_mysql mysql -uroot -prootpw -e "SET GLOBAL local_infile=1"
    PYTHONPATH=. uv run python scripts/verify_data_writers_e2e.py
"""
import os, tempfile
_T = tempfile.mkdtemp()
os.environ.update({"DB_ENGINE":"sqlite","DB_NAME":os.path.join(_T,"gw.db"),
    "SECRET_KEY":"x","CRYPTO_KEY_SALT":"x","ADMIN_USERNAME":"a","ADMIN_PASSWORD":"a1234567",
    "APP_ENV":"development","REMOTE_SSRF_GUARD_ENABLED":"False","REMOTE_SSL_MODE":"disable"})
from sqlalchemy import create_engine, text
from app.core.remote_engine import ServerTarget
from app.services.db_admin.data_copy import TableCopySpec, copy_tables

URL = "mysql+pymysql://root:rootpw@127.0.0.1:13401"
SRC, DST = "gw_cp_src", "gw_cp_dst"
t = ServerTarget(server_id=1, dialect="mysql", host="127.0.0.1", port=13401,
                 admin_user="root", admin_password="rootpw", ssl_mode="disable")

DDL = """CREATE TABLE valores (
  id INT NOT NULL AUTO_INCREMENT,
  t3 TIME(3) NULL,
  t6 TIME(6) NULL,
  tneg TIME NULL,
  cuerpo VARBINARY(64) NULL,
  txt VARCHAR(80) NULL,
  monto DECIMAL(12,4) NULL,
  estado ENUM('a','b') NULL,
  PRIMARY KEY (id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"""

FILAS = [
    (1, "01:02:03.123", "01:02:03.123456", "-01:30:00", b"\x00\ttab\nnl\\bs", "hola\ttab", "12.3456", "a"),
    (2, "-00:00:00.500", "-00:00:00.500000", "-00:00:01", b"\x01\x02", "\\N literal", "-0.0001", "b"),
    (3, None, None, "838:00:00", None, None, None, None),
]

def preparar(n_filas_extra=0):
    e = create_engine(URL, isolation_level="AUTOCOMMIT")
    with e.connect() as c:
        for db in (SRC, DST):
            c.execute(text(f"DROP DATABASE IF EXISTS {db}"))
            c.execute(text(f"CREATE DATABASE {db} CHARACTER SET utf8mb4"))
    e.dispose()
    es = create_engine(f"{URL}/{SRC}", isolation_level="AUTOCOMMIT")
    ed = create_engine(f"{URL}/{DST}", isolation_level="AUTOCOMMIT")
    for eng in (es, ed):
        with eng.connect() as c:
            c.execute(text(DDL))
    with es.connect() as c:
        for f in FILAS:
            c.execute(text(
                "INSERT INTO valores (id,t3,t6,tneg,cuerpo,txt,monto,estado) "
                "VALUES (:a,:b,:c,:d,:e,:f,:g,:h)"),
                dict(zip("abcdefgh", f)))
        for i in range(n_filas_extra):
            c.execute(text("INSERT INTO valores (id,txt) VALUES (:a,:b)"),
                      {"a": 100+i, "b": f"r{i}"})
    es.dispose(); ed.dispose()

def leer(db):
    e = create_engine(f"{URL}/{db}")
    with e.connect() as c:
        r = c.execute(text("SELECT id,t3,t6,tneg,cuerpo,txt,monto,estado FROM valores ORDER BY id")).fetchall()
    e.dispose()
    return [tuple(x) for x in r]

spec = TableCopySpec(table="valores",
    columns=["id","t3","t6","tneg","cuerpo","txt","monto","estado"],
    primary_key=["id"])

fallos = []
for nombre, extra in [("camino LEGACY (tabla chica -> INSERT unico)", 0),
                      ("camino BULK/FIFO (tabla grande -> LOAD DATA)", 2000)]:
    preparar(extra)
    origen = leer(SRC)
    res = copy_tables(source_target=t, source_db=SRC, source_engine="mysql",
                      dest_target=t, dest_db=DST, dest_engine="mysql",
                      specs=[spec], batch_rows=1000)
    destino = leer(DST)
    ok_status = res[0].status == "applied"
    ok_filas = origen == destino
    print(f"\n== {nombre} ==")
    print(f"  status={res[0].status} filas={res[0].rows_copied} (origen tiene {len(origen)})")
    if res[0].error: print(f"  ERROR: {res[0].error}")
    print(f"  {'ok  ' if ok_status else 'FALLA'} la tabla se aplico")
    print(f"  {'ok  ' if ok_filas else 'FALLA'} los datos son IDENTICOS al origen")
    if not (ok_status and ok_filas):
        fallos.append(nombre)
        for o, d in zip(origen, destino):
            if o != d:
                print(f"      origen : {o}")
                print(f"      destino: {d}")
    else:
        # Mostrar los TIME, que son el bug que perseguimos
        print(f"      t3/t6/tneg fila 1: {destino[0][1]} | {destino[0][2]} | {destino[0][3]}")
        print(f"      t3/t6/tneg fila 2: {destino[1][1]} | {destino[1][2]} | {destino[1][3]}")

print("\n" + "="*66)
print("TODO OK" if not fallos else f"FALLARON: {fallos}")

import sys
sys.exit(1 if fallos else 0)
