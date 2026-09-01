"""
Verificación end-to-end MANUAL de que el snapshot BATEADO da lo mismo que el N+1.

``structural_snapshot`` consultaba ``information_schema`` **una vez por tabla**
(``_column_extras`` + ``_table_storage_options``, y ésta además repetía la fila de
``SCHEMATA`` en cada iteración). Ahora hay un prefetch que trae lo mismo para toda la base
en dos consultas. El riesgo del cambio no es que falle: es que devuelva algo **casi** igual
y el diff empiece a ver cambios fantasma, o el DDL salga corrupto en un caso raro
(``ENUM`` sin su lista, ``UNSIGNED`` perdido, una columna generada con paréntesis en el
COMMENT). Nada de eso da error: da datos mal.

Así que la verificación es una IGUALDAD, contra un motor real y sobre una base con los
casos que históricamente rompieron: ENUM, SET, UNSIGNED, generadas con paréntesis en el
comentario, collations mezcladas por columna, engines distintos, FKs, índices compuestos,
vistas, rutinas y triggers.

Compara:
  1. ``structural_snapshot`` con prefetch (el camino nuevo).
  2. ``structural_snapshot`` con el prefetch DESACTIVADO (el camino viejo, por tabla).
  3. Cuenta las consultas de cada uno, para probar que además de igual es más barato.

NO es un test de pytest (requiere Docker; se ejecuta a mano).

Uso:
    docker run -d --rm --name gw_snap_mysql -e MYSQL_ROOT_PASSWORD=rootpw \\
        -e MYSQL_ROOT_HOST=% -p 13401:3306 mysql:8.0
    # o mariadb:11
    PYTHONPATH=. uv run python scripts/verify_snapshot_batch_e2e.py
"""

import os
import sys
import tempfile
import time

_TMP = tempfile.mkdtemp(prefix="e2e_gw_snap_")
os.environ.update({
    "DB_ENGINE": "sqlite",
    "DB_NAME": os.path.join(_TMP, "gw.db"),
    "SECRET_KEY": "e2e-secret",
    "CRYPTO_KEY_SALT": "e2e-salt",
    "ADMIN_USERNAME": "admin",
    "ADMIN_PASSWORD": "admin123",
    "APP_ENV": "development",
    "LOGGER_MIDDLEWARE_ENABLED": "False",
    "LOGGER_EXCEPTIONS_ENABLED": "False",
    "REMOTE_SSRF_GUARD_ENABLED": "False",
    "REMOTE_SSL_MODE": "disable",
})

from dataclasses import asdict, is_dataclass  # noqa: E402

from pydantic import BaseModel  # noqa: E402

from sqlalchemy import create_engine, event, text  # noqa: E402

from app.core.remote_engine import ServerTarget  # noqa: E402
from app.services.db_admin.mysql_adapter import MySQLAdapter  # noqa: E402

PORT = int(os.getenv("SNAP_E2E_PORT", "13401"))
URL = f"mysql+pymysql://root:rootpw@127.0.0.1:{PORT}"
DB = "gw_snap_e2e"

failures: list[str] = []


def check(nombre: str, ok: bool, detalle: str = "") -> None:
    print(f"  {'ok  ' if ok else 'FALLA'}  {nombre}{'' if ok else f' — {detalle}'}")
    if not ok:
        failures.append(f"{nombre}: {detalle}")


# ── La base de prueba: los casos que históricamente corrompieron el snapshot ──────────
DDL = [
    # ENUM y SET: ``str(reflected_type)`` los pierde y el DDL sale inválido.
    # UNSIGNED: se pierde el rango. tinyint(1): display width.
    """CREATE TABLE tipos (
         id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
         estado ENUM('alta','baja','en revisión') NOT NULL DEFAULT 'alta',
         banderas SET('a','b','c') NULL,
         flag TINYINT(1) NOT NULL DEFAULT 0,
         monto DECIMAL(12,4) UNSIGNED NULL,
         PRIMARY KEY (id)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci""",
    # Collations MEZCLADAS por columna, que es lo que el prefetch tiene que respetar por
    # columna y no por tabla.
    """CREATE TABLE textos (
         id INT NOT NULL AUTO_INCREMENT,
         a VARCHAR(50) CHARACTER SET latin1 COLLATE latin1_swedish_ci NULL,
         b VARCHAR(50) CHARACTER SET utf8mb4 COLLATE utf8mb4_bin NULL,
         c VARCHAR(50) NULL COMMENT 'comentario con (paréntesis) y ; punto y coma',
         tocado TIMESTAMP NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
         PRIMARY KEY (id),
         KEY idx_ab (a, b),
         UNIQUE KEY uq_c (c)
       ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_general_ci""",
    # Columna GENERADA con paréntesis en el COMMENT: el caso exacto que corrompía la
    # captura de SQLAlchemy y por el que existe ``generation_expression``.
    """CREATE TABLE generadas (
         id INT NOT NULL AUTO_INCREMENT,
         base INT NOT NULL,
         doble INT AS (base * 2) STORED COMMENT 'el doble (x2) de base',
         triple INT AS (base * 3) VIRTUAL,
         PRIMARY KEY (id)
       ) ENGINE=InnoDB""",
    # Otro ENGINE, para que el prefetch no asuma uno solo por base.
    """CREATE TABLE memoria (
         id INT NOT NULL,
         v VARCHAR(20) NULL,
         PRIMARY KEY (id)
       ) ENGINE=MEMORY""",
    # FK + tabla hija, para el orden topológico y las FKs sueltas.
    """CREATE TABLE hija (
         id INT NOT NULL AUTO_INCREMENT,
         tipo_id BIGINT UNSIGNED NOT NULL,
         PRIMARY KEY (id),
         KEY fk_tipo (tipo_id),
         CONSTRAINT fk_hija_tipo FOREIGN KEY (tipo_id) REFERENCES tipos (id) ON DELETE CASCADE
       ) ENGINE=InnoDB""",
    # SIN clave primaria: es lo que decide si la copia de datos usa tabla de staging, y el
    # prefetch de PKs tiene que reportarlo bien o una tabla se cargaría por el camino
    # equivocado.
    """CREATE TABLE sin_pk (
         id INT NOT NULL,
         v VARCHAR(20) NULL,
         KEY idx_v (v)
       ) ENGINE=InnoDB""",
    "CREATE VIEW v_altas AS SELECT id, estado FROM tipos WHERE estado = 'alta'",
    "CREATE VIEW v_encadenada AS SELECT id FROM v_altas",
    """CREATE PROCEDURE p_contar(IN limite INT, OUT total INT)
       BEGIN SELECT COUNT(*) INTO total FROM tipos WHERE id < limite; END""",
    """CREATE FUNCTION f_doble(x INT) RETURNS INT DETERMINISTIC
       BEGIN RETURN x * 2; END""",
    """CREATE TRIGGER t_antes BEFORE INSERT ON hija FOR EACH ROW
       BEGIN SET NEW.tipo_id = NEW.tipo_id; END""",
]


def preparar() -> None:
    eng = create_engine(URL, isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        conn.execute(text(f"DROP DATABASE IF EXISTS {DB}"))
        conn.execute(text(f"CREATE DATABASE {DB} CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"))
    eng.dispose()

    eng = create_engine(f"{URL}/{DB}", isolation_level="AUTOCOMMIT")
    with eng.connect() as conn:
        # Una tabla del gateway, para comprobar que el prefetch la sigue excluyendo.
        conn.execute(text("CREATE TABLE _gw_v_algo (version VARCHAR(32) NOT NULL)"))
        for sentencia in DDL:
            conn.execute(text(sentencia))
        conn.execute(text("INSERT INTO tipos (estado) VALUES ('alta'), ('baja')"))
    eng.dispose()


def normalizar(obj):
    """Snapshot → estructura comparable, sin depender de la identidad de los dataclasses."""
    if isinstance(obj, BaseModel):
        return {k: normalizar(v) for k, v in obj.model_dump().items()}
    if is_dataclass(obj) and not isinstance(obj, type):
        return {k: normalizar(v) for k, v in asdict(obj).items()}
    if isinstance(obj, dict):
        return {k: normalizar(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [normalizar(v) for v in obj]
    return obj


def main() -> int:
    target = ServerTarget(
        server_id=1, dialect="mysql", host="127.0.0.1", port=PORT,
        admin_user="root", admin_password="rootpw", ssl_mode="disable",
    )

    print(f"\n== Preparando {DB} en 127.0.0.1:{PORT} ==")
    preparar()

    adapter = MySQLAdapter(target)

    # Camino NUEVO (bateado). Se cuentan las consultas con un listener de SQLAlchemy.
    consultas_batch: list[str] = []
    consultas_n1: list[str] = []

    def contar(bolsa):
        def _handler(conn, cursor, statement, parameters, context, executemany):
            bolsa.append(statement)
        return _handler

    from app.core import remote_engine

    eng = remote_engine.get_engine(target, DB)
    h_batch = contar(consultas_batch)
    event.listen(eng, "before_cursor_execute", h_batch)
    t0 = time.perf_counter()
    snap_batch = adapter.structural_snapshot(DB)
    ms_batch = int((time.perf_counter() - t0) * 1000)
    event.remove(eng, "before_cursor_execute", h_batch)

    # Camino VIEJO: se desactiva el prefetch, que es exactamente lo que hacía antes.
    adapter_n1 = MySQLAdapter(target)
    adapter_n1._prefetch_column_extras = lambda *a, **k: None
    adapter_n1._prefetch_table_storage_options = lambda *a, **k: None
    adapter_n1._prefetch_row_estimates = lambda *a, **k: None
    adapter_n1._prefetch_primary_keys = lambda *a, **k: None

    # El bateo de vistas y rutinas vive DENTRO de sus hooks, no en un prefetch conmutable, así
    # que el brazo de control es una reimplementación explícita del N+1 que había antes. Si el
    # bateado y éste divergen, es que el agrupado en memoria perdió u ordenó mal algo.
    def _views_n1(conn, database, schema):
        from app.services.db_admin.dtos import ViewInfo
        out = []
        for name, vdef, check_option, security in conn.execute(
            text(
                "SELECT TABLE_NAME, VIEW_DEFINITION, CHECK_OPTION, SECURITY_TYPE "
                "FROM information_schema.VIEWS WHERE TABLE_SCHEMA = :db ORDER BY TABLE_NAME"
            ),
            {"db": database},
        ).fetchall():
            cols = [
                r[0]
                for r in conn.execute(
                    text(
                        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                        "WHERE TABLE_SCHEMA = :db AND TABLE_NAME = :t ORDER BY ORDINAL_POSITION"
                    ),
                    {"db": database, "t": name},
                ).fetchall()
            ]
            out.append(ViewInfo(
                name=name, is_materialized=False, definition=str(vdef or ""), columns=cols,
                check_option=None if not check_option or check_option == "NONE" else str(check_option),
                security_definer=str(security or "").upper() == "DEFINER",
            ))
        return out

    def _routines_n1(conn, database, schema):
        from app.services.db_admin.dtos import RoutineInfo, RoutineParam
        from app.services.db_admin.identifiers import quote_identifier, validate_identifier
        out = []
        for name, rtype, return_type, deterministic, security in conn.execute(
            text(
                "SELECT ROUTINE_NAME, ROUTINE_TYPE, DTD_IDENTIFIER, IS_DETERMINISTIC, "
                "SECURITY_TYPE FROM information_schema.ROUTINES "
                "WHERE ROUTINE_SCHEMA = :db ORDER BY ROUTINE_TYPE, ROUTINE_NAME"
            ),
            {"db": database},
        ).fetchall():
            kind = "PROCEDURE" if str(rtype).upper() == "PROCEDURE" else "FUNCTION"
            q = quote_identifier(
                validate_identifier(name, "mysql", "rutina", allow_existing=True), "mysql"
            )
            crow = conn.execute(text(f"SHOW CREATE {kind} {q}")).fetchone()
            body = adapter_n1._strip_definer_clause(
                adapter_n1._show_create_value(crow, (f"Create {kind.capitalize()}",), 2)
            )
            params = []
            for pname, pmode, dtd, ordinal in conn.execute(
                text(
                    "SELECT PARAMETER_NAME, PARAMETER_MODE, DTD_IDENTIFIER, ORDINAL_POSITION "
                    "FROM information_schema.PARAMETERS "
                    "WHERE SPECIFIC_SCHEMA = :db AND SPECIFIC_NAME = :n ORDER BY ORDINAL_POSITION"
                ),
                {"db": database, "n": name},
            ).fetchall():
                if ordinal == 0:
                    continue
                params.append(RoutineParam(name=pname, mode=pmode, type=str(dtd or "")))
            out.append(RoutineInfo(
                name=name, kind=kind, parameters=params,
                return_type=str(return_type) if return_type else None, language="SQL",
                deterministic=str(deterministic or "").upper() == "YES",
                security_definer=str(security or "").upper() == "DEFINER", body=body,
            ))
        return out

    adapter_n1._snapshot_views = _views_n1
    adapter_n1._snapshot_routines = _routines_n1

    h_n1 = contar(consultas_n1)
    event.listen(eng, "before_cursor_execute", h_n1)
    t0 = time.perf_counter()
    snap_n1 = adapter_n1.structural_snapshot(DB)
    ms_n1 = int((time.perf_counter() - t0) * 1000)
    event.remove(eng, "before_cursor_execute", h_n1)

    print("\n== Igualdad: el bateado tiene que dar EXACTAMENTE lo mismo ==")
    a, b = normalizar(snap_batch), normalizar(snap_n1)
    check("el snapshot completo es idéntico", a == b)
    if a != b:
        # Localizar la diferencia para que el fallo sea accionable.
        for clave in a:
            if a[clave] != b.get(clave):
                print(f"    difiere en '{clave}'")
                if clave == "tables":
                    for t_a, t_b in zip(a["tables"], b["tables"], strict=False):
                        if t_a != t_b:
                            print(f"      tabla {t_a.get('table')}:")
                            for campo in t_a:
                                if t_a[campo] != t_b.get(campo):
                                    print(f"        {campo}:")
                                    print(f"          batch = {t_a[campo]}")
                                    print(f"          n+1   = {t_b.get(campo)}")

    print("\n== Que los casos difíciles estén de verdad en el snapshot ==")
    por_nombre = {t.table: t for t in snap_batch.tables}
    check("excluye la contabilidad del gateway", "_gw_v_algo" not in por_nombre,
          f"tablas: {sorted(por_nombre)}")
    check("6 tablas de usuario", len(por_nombre) == 6, f"encontradas {sorted(por_nombre)}")

    tipos = por_nombre.get("tipos")
    if tipos:
        cols = {c.name: c for c in tipos.columns}
        estado = cols.get("estado")
        check("el ENUM conserva su lista de valores",
              bool(estado and "en revisión" in (estado.type or "")),
              f"type = {estado.type if estado else None}")
        idc = cols.get("id")
        check("UNSIGNED no se pierde",
              bool(idc and "unsigned" in (idc.type or "").lower()),
              f"type = {idc.type if idc else None}")
        check("el engine de la tabla se lee", tipos.storage_options.get("engine") == "InnoDB",
              str(tipos.storage_options))
        check("el default de la BASE viaja en cada tabla",
              tipos.storage_options.get("db_collation") == "utf8mb4_unicode_ci",
              str(tipos.storage_options))

    memoria = por_nombre.get("memoria")
    check("un engine distinto por tabla se respeta",
          bool(memoria and memoria.storage_options.get("engine") == "MEMORY"),
          str(memoria.storage_options) if memoria else "sin tabla")

    textos = por_nombre.get("textos")
    if textos:
        cols = {c.name: c for c in textos.columns}
        check("collation por COLUMNA, no por tabla",
              cols.get("a") is not None and cols["a"].collation == "latin1_swedish_ci",
              f"a.collation = {cols.get('a').collation if cols.get('a') else None}")
        check("otra collation en la misma tabla",
              cols.get("b") is not None and cols["b"].collation == "utf8mb4_bin",
              f"b.collation = {cols.get('b').collation if cols.get('b') else None}")
        check("ON UPDATE CURRENT_TIMESTAMP se detecta",
              cols.get("tocado") is not None and cols["tocado"].on_update is not None)

    generadas = por_nombre.get("generadas")
    if generadas:
        cols = {c.name: c for c in generadas.columns}
        doble = cols.get("doble")
        check("la columna generada trae su expresión canónica",
              bool(doble and doble.computed and doble.computed.sqltext),
              f"computed = {doble.computed if doble else None}")
        check("el COMMENT con paréntesis de una generada no se corrompe",
              bool(doble and doble.comment and "(x2)" in doble.comment),
              f"comment = {doble.comment if doble else None}")

    check("las vistas se leen", len(snap_batch.views) == 2, f"{len(snap_batch.views)}")
    check("las rutinas se leen", len(snap_batch.routines) == 2, f"{len(snap_batch.routines)}")
    check("los triggers se leen", len(snap_batch.triggers) == 1, f"{len(snap_batch.triggers)}")

    print("\n== Objetos con cuerpo: vistas y rutinas ==")
    v_por_nombre = {v.name: v for v in snap_batch.views}
    check("la vista trae sus columnas, en orden",
          v_por_nombre["v_altas"].columns == ["id", "estado"]
          if "v_altas" in v_por_nombre else False,
          str(v_por_nombre.get("v_altas")))
    check("una vista encadenada también",
          v_por_nombre["v_encadenada"].columns == ["id"]
          if "v_encadenada" in v_por_nombre else False)
    r_por_nombre = {r.name: r for r in snap_batch.routines}
    proc = r_por_nombre.get("p_contar")
    check("los parámetros de una rutina conservan orden y modo",
          proc is not None
          and [(p.name, p.mode) for p in proc.parameters] == [("limite", "IN"), ("total", "OUT")],
          str([(p.name, p.mode) for p in proc.parameters]) if proc else "sin p_contar")
    fn = r_por_nombre.get("f_doble")
    check("una FUNCTION no cuenta su tipo de retorno como parámetro",
          fn is not None and [p.name for p in fn.parameters] == ["x"],
          str([p.name for p in fn.parameters]) if fn else "sin f_doble")

    print("\n== list_table_stats: el bateado tiene que dar EXACTAMENTE lo mismo ==")
    stats_batch = adapter.list_table_stats(DB)
    stats_n1 = adapter_n1.list_table_stats(DB)
    check("las estadísticas por tabla son idénticas",
          normalizar(stats_batch) == normalizar(stats_n1))
    por_tabla = {t.table: t for t in stats_batch}
    check("una tabla SIN PK se reporta sin PK",
          por_tabla["sin_pk"].has_primary_key is False if "sin_pk" in por_tabla else False,
          f"tablas: {sorted(por_tabla)}")
    check("una tabla CON PK se reporta con PK",
          por_tabla["tipos"].has_primary_key is True if "tipos" in por_tabla else False)
    # `estimated_rows_known` es la distinción que se rompe si alguien confunde None con 0: una
    # tabla que el catálogo no supo estimar se informaría como vacía.
    check("ninguna estimación None se convirtió en 0 silenciosamente",
          all(s.estimated_rows_known or s.estimated_rows == 0 for s in stats_batch))

    q_stats_batch: list[str] = []
    q_stats_n1: list[str] = []
    h1 = contar(q_stats_batch)
    event.listen(eng, "before_cursor_execute", h1)
    adapter.list_table_stats(DB)
    event.remove(eng, "before_cursor_execute", h1)
    h2 = contar(q_stats_n1)
    event.listen(eng, "before_cursor_execute", h2)
    adapter_n1.list_table_stats(DB)
    event.remove(eng, "before_cursor_execute", h2)
    print(f"  consultas de list_table_stats: {len(q_stats_n1)} -> {len(q_stats_batch)}")
    check("list_table_stats dejó de escalar con la cantidad de tablas",
          len(q_stats_batch) < len(q_stats_n1),
          f"{len(q_stats_batch)} vs {len(q_stats_n1)}")

    print("\n== Costo: además de igual, más barato ==")
    n = len(snap_batch.tables)
    print(f"  bateado  : {len(consultas_batch):4d} consultas, {ms_batch:5d} ms")
    print(f"  por tabla: {len(consultas_n1):4d} consultas, {ms_n1:5d} ms")
    print(f"  ({n} tablas)")
    # Los MILISEGUNDOS de acá NO significan nada: contra un MySQL en localhost el RTT es
    # ~0 y el ruido de la primera conexión domina. Lo que se verifica es el CONTEO, que es
    # lo que se multiplica por el RTT en un servidor remoto — que es el caso real.

    # Fijar el número EXACTO, y no «menos consultas», es lo que detecta que vuelva a colarse un
    # N+1: con «menos» alcanzaría con ahorrar una sola. Se ahorra, por cada eje bateado:
    #   tablas:  3 consultas por tabla (COLUMNS + TABLES + SCHEMATA) -> 3 para toda la base
    #   vistas:  1 consulta de columnas por vista  -> 1 para todas
    #   rutinas: 1 consulta de parámetros por rutina -> 1 para todas
    n_vistas = len(snap_batch.views)
    n_rutinas = len(snap_batch.routines)
    esperado = 3 * (n - 1) + (n_vistas - 1) + (n_rutinas - 1)
    real = len(consultas_n1) - len(consultas_batch)
    check(f"los N+1 desaparecieron: {esperado} consultas menos ({3 * (n - 1)} de tablas, {n_vistas - 1} de vistas, {n_rutinas - 1} de rutinas)",
          real == esperado,
          f"esperaba {esperado}, hubo {real}")
    check("una sola consulta a SCHEMATA en todo el snapshot",
          sum(1 for q in consultas_batch if "SCHEMATA" in q) == 2,
          f"{sum(1 for q in consultas_batch if 'SCHEMATA' in q)} (prefetch + _database_defaults)")

    print("\n" + "=" * 70)
    if failures:
        print(f"FALLARON {len(failures)}:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("TODO OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
