"""
Tests unitarios PUROS de la política de la consola SQL (``query_policy``): sin cliente,
sin motor y sin BD.

Es el módulo que decide si una sentencia se ejecuta directo, exige confirmación o queda
prohibida, así que la cobertura se organiza por el tipo de error que la política existe
para evitar:

- **Falsos positivos** de un enfoque por palabras clave (``WHERE accion = 'DELETE'``).
- **Evasiones** de ese mismo enfoque (comentarios, mayúsculas mezcladas, CTE, comentario
  ejecutable de MySQL).
- **Fail-closed**: SQL ilegible u opaco debe salir PELIGROSO, nunca lectura.
- **Blocklist**: lo que no se ejecuta ni confirmando.
- **Estimación de impacto**: solo cuando el conteo es EXACTO.
"""

import pytest

from app.services.db_admin import query_policy as qp

MYSQL = "mysql"
PG = "postgresql"


def _danger(sql: str, engine: str = MYSQL) -> str:
    return qp.classify(sql, engine=engine).danger


# --------------------------------------------------------------------------- #
# Lectura — y los falsos positivos que un filtro por palabras clave marcaría    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM t WHERE a = 1",
        # La palabra peligrosa está DENTRO de un literal: no se ejecuta nada.
        "SELECT * FROM logs WHERE accion = 'DELETE'",
        "SELECT * FROM logs WHERE accion = 'GRANT'",
        # …y dentro de un comentario.
        "SELECT * FROM t -- DROP TABLE u",
        "SELECT * FROM t /* TRUNCATE TABLE u */",
        "SELECT * FROM t UNION SELECT * FROM u",
        "SHOW TABLES",
        "DESCRIBE t",
        "EXPLAIN SELECT * FROM t",
        # Leer un esquema del sistema es legítimo: es parte de probar permisos.
        "SELECT * FROM mysql.user",
    ],
)
def test_lecturas_no_requieren_confirmacion(sql):
    plan = qp.classify(sql, engine=MYSQL)
    assert plan.danger == qp.READ
    assert not plan.requires_confirmation


def test_show_grants_no_parsea_pero_se_reconoce_como_lectura():
    """
    sqlglot no parsea ``SHOW GRANTS FOR …``. Admitirlo por su palabra inicial es seguro
    PORQUE el nivel de lectura se ejecuta en una transacción READ ONLY: si el
    reconocimiento fallara, el motor aborta la sentencia.
    """
    plan = qp.classify("SHOW GRANTS FOR 'a'@'b'", engine=MYSQL)
    assert plan.danger == qp.READ
    assert any(r.code == "read_by_leading_keyword" for r in plan.reasons)


# --------------------------------------------------------------------------- #
# Evasiones que un filtro por palabras clave dejaría pasar                      #
# --------------------------------------------------------------------------- #
def test_delete_ofuscado_con_comentario_y_mayusculas_mezcladas():
    assert _danger("/*x*/ dElEtE FROM t") == qp.WRITE


def test_dml_escondido_en_un_cte_de_postgresql():
    """La raíz es un ``Select``; el ``DELETE`` vive dentro del CTE."""
    plan = qp.classify(
        "WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d", engine=PG
    )
    assert plan.danger == qp.WRITE
    assert any(r.code == "nested_dml" for r in plan.reasons)


def test_comentario_ejecutable_de_mysql_no_es_un_comentario():
    """
    ``/*!40101 … */`` lo EJECUTA MySQL. Tratarlo como comentario sería una evasión
    trivial de la blocklist; el número de versión tampoco puede desplazar el anclaje.
    """
    assert _danger("/*!40101 GRANT ALL ON *.* TO 'x'@'%' */") == qp.BLOCKED


def test_explain_analyze_ejecuta_y_no_es_lectura():
    assert _danger("EXPLAIN ANALYZE SELECT * FROM t", PG) == qp.DDL
    assert _danger("EXPLAIN ANALYZE DELETE FROM t") == qp.WRITE


def test_select_for_update_bloquea_filas():
    plan = qp.classify("SELECT * FROM t FOR UPDATE", engine=MYSQL)
    assert plan.danger == qp.WRITE
    assert any(r.code == "row_locking_read" for r in plan.reasons)


def test_select_into_materializa_y_no_es_lectura():
    assert _danger("SELECT * INTO nueva FROM t", PG) == qp.DDL


# --------------------------------------------------------------------------- #
# Fail-closed                                                                   #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql,code",
    [
        ("esto no es sql valido (((", "unparseable"),
        ("CALL sp_borrar_todo()", "opaque_statement"),
        ("REPLACE INTO t VALUES (1)", "opaque_statement"),
    ],
)
def test_sql_ilegible_u_opaco_sale_peligroso(sql, code):
    plan = qp.classify(sql, engine=MYSQL)
    assert plan.danger == qp.DDL
    assert any(r.code == code for r in plan.reasons)
    assert plan.requires_confirmation


# --------------------------------------------------------------------------- #
# Escritura y DDL: exigen confirmación                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql,expected",
    [
        ("UPDATE t SET a = 1 WHERE b = 2", qp.WRITE),
        ("UPDATE t SET a = 1", qp.WRITE),
        ("DELETE FROM t WHERE b = 2", qp.WRITE),
        ("INSERT INTO t VALUES (1)", qp.WRITE),
        ("DROP TABLE t", qp.DDL),
        ("ALTER TABLE t ADD COLUMN c INT", qp.DDL),
        ("TRUNCATE TABLE t", qp.DDL),
        ("RENAME TABLE a TO b", qp.DDL),
    ],
)
def test_escritura_y_ddl_exigen_confirmacion(sql, expected):
    plan = qp.classify(sql, engine=MYSQL)
    assert plan.danger == expected
    assert plan.requires_confirmation
    # La confirmación se pide igual TENGA O NO cláusula WHERE: es el requisito del
    # usuario y lo que evita el UPDATE masivo por descuido.
    assert not plan.is_blocked


# --------------------------------------------------------------------------- #
# Blocklist: prohibido incluso confirmando                                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql,engine,code",
    [
        # DCL: evita el módulo de permisos del gateway y su auditoría.
        ("GRANT SELECT ON db.* TO 'u'@'%'", MYSQL, "dcl_grant_revoke"),
        ("REVOKE ALL ON db.* FROM 'u'@'%'", MYSQL, "dcl_grant_revoke"),
        ("CREATE USER 'x'@'%' IDENTIFIED BY 'p'", MYSQL, "dcl_user_role"),
        ("ALTER USER 'x'@'%' IDENTIFIED BY 'p'", MYSQL, "dcl_user_role"),
        ("SET PASSWORD FOR 'x'@'%' = 'p'", MYSQL, "dcl_user_role"),
        # Acceso a archivos del host: el gateway conecta como pseudo-root, el motor SÍ
        # lo permitiría.
        ("COPY t FROM PROGRAM 'curl evil'", PG, "copy_statement"),
        ("SELECT * FROM t INTO OUTFILE '/tmp/x'", MYSQL, "server_file_access"),
        ("SELECT LOAD_FILE('/etc/passwd')", MYSQL, "server_file_access"),
        ("SELECT pg_read_file('/etc/passwd')", PG, "server_file_access"),
        ("LOAD DATA INFILE '/x' INTO TABLE t", MYSQL, "server_file_access"),
        ("CREATE EXTENSION plpython3u", PG, "extension_or_untrusted_language"),
        # Estado global del servidor: afecta a todas sus bases.
        ("SET GLOBAL max_connections = 100", MYSQL, "server_global_state"),
        ("ALTER SYSTEM SET work_mem = '1GB'", PG, "server_global_state"),
        ("FLUSH PRIVILEGES", MYSQL, "server_global_state"),
        ("KILL 12", MYSQL, "server_global_state"),
        # Ciclo de vida de BDs: tiene endpoint dedicado con doble confirmación.
        ("DROP DATABASE otra", MYSQL, "database_lifecycle"),
        ("DROP SCHEMA public CASCADE", PG, "database_lifecycle"),
        # Control de sesión: rompería el envoltorio de solo lectura del runner.
        ("BEGIN", MYSQL, "session_control"),
        ("USE otra_db", MYSQL, "session_control"),
        ("LOCK TABLES t WRITE", MYSQL, "session_control"),
        ("SET ROLE otro", PG, "role_switch"),
        # SQL dinámico: ejecutaría texto que la política nunca clasificó.
        ("PREPARE s FROM 'DROP TABLE t'", MYSQL, "dynamic_sql"),
        ("EXECUTE s", MYSQL, "dynamic_sql"),
    ],
)
def test_sentencias_prohibidas(sql, engine, code):
    plan = qp.classify(sql, engine=engine)
    assert plan.is_blocked
    assert any(r.code == code for r in plan.reasons)


def test_flush_privileges_no_se_detecta_por_el_ast():
    """
    Regresión del motivo por el que la blocklist de TEXTO no es redundante: sqlglot
    parsea ``FLUSH PRIVILEGES`` como una expresión ``Alias``, indistinguible de algo
    inofensivo. Si la clasificación dependiera solo del AST, pasaría como lectura.
    """
    import sqlglot
    from sqlglot import exp

    assert isinstance(sqlglot.parse_one("FLUSH PRIVILEGES", read="mysql"), exp.Alias)
    assert _danger("FLUSH PRIVILEGES") == qp.BLOCKED


def test_escribir_un_esquema_del_sistema_se_bloquea_pero_leerlo_no():
    assert (
        _danger("UPDATE mysql.user SET authentication_string = '' WHERE user = 'root'")
        == qp.BLOCKED
    )
    assert _danger("SELECT * FROM mysql.user") == qp.READ


def test_tablas_internas_del_gateway():
    """
    La consola no puede ser una vía nueva para tocar ``_gw_v_*``: es la contabilidad de
    versiones de Alembic dentro de cada BD gestionada, y perderla deja la base sin
    puntero de versión (el incidente que ya cubren los guards de migraciones).
    """
    plan = qp.classify("DROP TABLE _gw_v_miblueprint", engine=MYSQL)
    assert plan.is_blocked
    assert any(r.code == "gateway_internal_table" for r in plan.reasons)


# --------------------------------------------------------------------------- #
# Lotes                                                                         #
# --------------------------------------------------------------------------- #
def test_el_peligro_del_lote_es_el_maximo_de_sus_sentencias():
    plan = qp.classify(
        "SELECT 1;\nUPDATE t SET a = 1 WHERE id = 2;\nSELECT 2;", engine=MYSQL
    )
    assert len(plan.statements) == 3
    assert plan.danger == qp.WRITE
    assert plan.requires_confirmation


def test_una_sola_sentencia_prohibida_bloquea_el_lote_entero():
    plan = qp.classify("SELECT 1; GRANT ALL ON *.* TO 'x'@'%'", engine=MYSQL)
    assert plan.is_blocked
    assert len(plan.blocked_statements) == 1


def test_cuerpo_procedural_no_se_parte_en_su_primer_punto_y_coma():
    """
    El splitter reconoce los cuerpos ``BEGIN…END`` de las rutinas por sí solo, SIN
    necesidad de ``DELIMITER``: la política recibe las sentencias completas y no el
    cuerpo cortado en su primer ``;`` interno.

    DOS rutinas (no una): con una sola no hay par que emparejar, así que un bug de
    scanner "pasaría igual" — es exactamente el motivo, documentado en el historial del
    proyecto, por el que el bug de ``DELIMITER $$`` vs dollar-quoting tardó en notarse.
    """
    sql = (
        "CREATE PROCEDURE sp1() BEGIN DECLARE x INT; SET x = 1; END;\n"
        "CREATE PROCEDURE sp2() BEGIN DECLARE y INT; SET y = 2; END;"
    )
    plan = qp.classify(sql, engine=MYSQL)
    assert len(plan.statements) == 2
    assert plan.danger == qp.DDL
    assert "sp1" in plan.statements[0].sql and "END" in plan.statements[0].sql
    assert "sp2" in plan.statements[1].sql and "END" in plan.statements[1].sql


@pytest.mark.parametrize(
    "sql,engine",
    [
        ("DELIMITER //\nUPDATE t SET x = 1; GRANT ALL ON SCHEMA public TO evil //", PG),
        ("DELIMITER //\nUPDATE t SET x = 1; DROP DATABASE victima //", MYSQL),
        ("DELIMITER //\nUPDATE t SET x = 1; PREPARE s FROM 'DROP TABLE t' //", MYSQL),
        ("DELIMITER $$\nCREATE PROCEDURE sp() BEGIN SET x = 1; END$$", MYSQL),
    ],
)
def test_delimiter_se_rechaza_porque_agrupaba_sentencias_y_evadia_la_blocklist(sql, engine):
    """
    REGRESIÓN de un bypass real: ``DELIMITER`` es una directiva del CLIENTE mysql, no del
    motor. Su único efecto acá era agrupar varias sentencias del servidor en UNA unidad
    del splitter, y con eso el keyword peligroso dejaba de estar al principio del texto
    normalizado — evadiendo TODA la blocklist anclada con ``^`` (DCL, estado global,
    ciclo de vida de BDs, SQL dinámico…), que pasaba a clasificarse ``write``/``ddl``,
    es decir, ejecutable con confirmación.

    No se pierde funcionalidad: el test de arriba muestra que los cuerpos ``BEGIN…END``
    se reconocen sin la directiva.
    """
    plan = qp.classify(sql, engine=engine)
    assert plan.is_blocked
    assert any(r.code == "delimiter_directive" for r in plan.reasons)


# --------------------------------------------------------------------------- #
# Estimación de impacto                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql,engine",
    [
        ("UPDATE t SET a = 1 WHERE b = 2 AND c IN (1, 2)", MYSQL),
        ("DELETE FROM esquema.t WHERE b = 2", MYSQL),
        # Sin WHERE el conteo es el total de la tabla: justamente la cifra que hace
        # evidente el error antes de confirmar.
        ("UPDATE t SET a = 1", MYSQL),
        # Una subconsulta se conserva íntegra, así que el conteo sigue siendo exacto.
        ("DELETE FROM t WHERE id IN (SELECT id FROM u)", MYSQL),
    ],
)
def test_impacto_estimable(sql, engine):
    stmt = qp.classify(sql, engine=engine).statements[0]
    assert stmt.impact_query is not None
    assert stmt.impact_query.upper().startswith("SELECT COUNT(*)")


@pytest.mark.parametrize(
    "sql,engine",
    [
        # El USING vive en otra rama del árbol: el COUNT ingenuo dejaría ``u`` fuera de
        # alcance y devolvería un número falso.
        ("DELETE FROM t USING u WHERE t.id = u.id", PG),
        ("UPDATE t SET a = 1 FROM u WHERE t.id = u.id", PG),
        # El COUNT de un join cuenta filas del producto, no las filas actualizadas.
        ("UPDATE a JOIN b ON a.id = b.id SET a.x = 1 WHERE b.y = 2", MYSQL),
    ],
)
def test_impacto_no_estimable_devuelve_none_en_vez_de_un_numero_enganoso(sql, engine):
    stmt = qp.classify(sql, engine=engine).statements[0]
    assert stmt.impact_query is None
    # No saber cuántas filas afecta NO relaja la política.
    assert qp.classify(sql, engine=engine).requires_confirmation


# --------------------------------------------------------------------------- #
# Utilidades                                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql,secreto",
    [
        ("ALTER USER 'x'@'%' IDENTIFIED BY 'sup3rs3cr3t'", "sup3rs3cr3t"),
        ("CREATE ROLE r WITH ENCRYPTED PASSWORD 'abc123'", "abc123"),
        ("CREATE USER u IDENTIFIED WITH mysql_native_password BY 'p4ss'", "p4ss"),
    ],
)
def test_redaccion_de_contrasenas_antes_de_persistir(sql, secreto):
    redactado = qp.redact_secrets(sql)
    assert secreto not in redactado
    assert "'***'" in redactado


def test_el_hash_cambia_con_el_sql_pero_no_con_el_espaciado_de_los_bordes():
    assert qp.sql_hash("  SELECT 1  ") == qp.sql_hash("SELECT 1")
    assert qp.sql_hash("SELECT 1") != qp.sql_hash("DROP TABLE t")


# --------------------------------------------------------------------------- #
# GAP: contenido de un comentario ejecutable de MySQL (``/*!... */``) es CÓDIGO   #
# para el escaneo de texto pero INVISIBLE para el AST de sqlglot                 #
# --------------------------------------------------------------------------- #
def test_for_update_escondido_en_comentario_ejecutable_se_detecta_por_texto():
    """
    REGRESIÓN de un hueco de clasificación: sqlglot NO tokeniza el contenido de
    ``/*!… */`` —lo adjunta como comentario al nodo más cercano, sin parsearlo— así que
    un ``FOR UPDATE`` escondido ahí era invisible para el AST, que es donde la política
    lo detectaba (``exp.Lock``). La sentencia salía ``read`` y se ejecutaba SIN pedir
    confirmación, aunque MySQL sí ejecuta el contenido del comentario.

    El motor lo habría atajado igual (``START TRANSACTION READ ONLY`` rechaza un
    ``FOR UPDATE`` con errno 1792), pero eso es la red de seguridad, no la política:
    ahora hay respaldo de TEXTO, igual que ya existía para ``INTO OUTFILE``.
    """
    plan = qp.classify("SELECT * FROM t /*!40101 FOR UPDATE*/", engine=MYSQL)
    assert plan.danger == qp.WRITE
    assert any(r.code == "row_locking_read" for r in plan.reasons)


def test_select_into_escondido_en_comentario_ejecutable_se_detecta_por_texto():
    """Mismo mecanismo que el test hermano, con ``SELECT … INTO @var``."""
    plan = qp.classify("SELECT 1 /*!40100 INTO @x*/", engine=MYSQL)
    assert plan.danger == qp.DDL
    assert any(r.code == "select_into" for r in plan.reasons)


def test_el_respaldo_de_texto_no_confunde_un_insert_con_un_select_into():
    """``INSERT INTO`` debe seguir siendo ``write``: el respaldo mira ``INTO @``."""
    assert _danger("INSERT INTO t VALUES (1)") == qp.WRITE


def test_comentario_ejecutable_con_verbo_blocklisteado_si_se_detecta():
    """
    Contraste con los dos tests anteriores: cuando el contenido oculto SÍ coincide
    con un patrón de TEXTO de la blocklist, el escaneo lo atrapa igual —
    ``_scan_normalize`` preserva el contenido de ``/*!... */`` como código, así que
    ``INTO OUTFILE`` escondido ahí se bloquea. La diferencia es exclusivamente con
    los verbos que la política reconoce SOLO por AST (``FOR UPDATE``/``FOR SHARE``,
    ``SET`` fuera de la raíz, ``SELECT ... INTO``) y no tienen respaldo de texto.
    """
    assert _danger("SELECT 1 /*!40100 INTO OUTFILE '/tmp/x'*/") == qp.BLOCKED


# --------------------------------------------------------------------------- #
# GAP: falso positivo dentro de un cuerpo con dollar-quoting (PostgreSQL) — un     #
# literal de NEGOCIO que contiene texto de la blocklist SIN ser la operación real #
# --------------------------------------------------------------------------- #
def test_falso_positivo_bloquea_string_de_negocio_dentro_de_cuerpo_plpgsql():
    """
    HALLAZGO: dentro de un bloque ``$$...$$`` la política preserva el texto ENTERO
    como código (correcto: ahí puede haber un ``COPY ... FROM PROGRAM`` real oculto
    en un ``CREATE FUNCTION``), pero a diferencia de lo que hace FUERA del bloque, no
    vuelve a vaciar los literales de cadena ANIDADOS dentro del dollar-quoting
    (``WHERE accion = 'GRANT'`` no dispara nada fuera de un bloque; el mismo texto
    adentro de un ``$$...$$`` sí puede disparar). Un string de NEGOCIO dentro de un
    ``RAISE NOTICE`` que mencione, por ejemplo, "FROM PROGRAM" en su mensaje, bloquea
    una función legítima con ``server_file_access``.

    Es un sobre-bloqueo DELIBERADO, no un pendiente: vaciar los literales anidados
    escondería un ``EXECUTE 'COPY … FROM PROGRAM …'`` dentro de un cuerpo plpgsql, que es
    justamente la vía que ``server_file_access`` existe para cortar. Se prefiere rechazar
    una función legítima antes que dejar pasar una peligrosa; el camino para crear
    rutinas con texto así es el módulo de migraciones, que sí las versiona y audita.
    """
    sql = (
        "CREATE FUNCTION f() RETURNS void AS $$\n"
        "BEGIN\n"
        "  RAISE NOTICE 'export data FROM PROGRAM invoice';\n"
        "END;\n"
        "$$ LANGUAGE plpgsql;"
    )
    plan = qp.classify(sql, engine=PG)
    assert plan.danger == qp.BLOCKED  # sobre-bloqueo deliberado (ver docstring)
    assert any(r.code == "server_file_access" for r in plan.reasons)


# --------------------------------------------------------------------------- #
# Defensa en profundidad: el escaneo de la blocklist sobre el LOTE crudo atrapa    #
# lo que el splitter descarta como "sentencia" (comentario ejecutable aislado)    #
# --------------------------------------------------------------------------- #
def test_comentario_ejecutable_aislado_no_produce_sentencias_pero_bloquea_el_lote():
    """
    Si el LOTE es ÚNICAMENTE un comentario ejecutable (sin ninguna otra sentencia),
    ``split_sql_statements`` lo descarta por completo: para PARTIR lo trata como
    "puro comentario" (``_LEADING_NOISE_RE`` no distingue ``/*!...*/`` de un
    comentario normal), aunque la política sí lo trate como CÓDIGO para escanear.
    ``plan.statements`` queda VACÍO — ``classify_statement`` nunca llega a
    ejecutarse sobre este texto. Lo que bloquea el lote es el escaneo de texto
    sobre el SQL CRUDO en ``classify()`` (comentado ahí mismo como "defensa en
    profundidad", no como decisión de seguridad primaria).

    Importa porque el runner ejecuta EXACTAMENTE ``plan.statements``: si este
    escaneo crudo se rompiera o se quitara, ``plan.statements == ()`` haría que el
    lote se reportara ``read`` con CERO sentencias — ni bloqueado ni ejecutado nada,
    pero sin avisar del intento (ver también los dos tests de arriba: el mismo
    mecanismo de "el splitter descarta, pero classify() todavía escanea el texto
    crudo" NO cubre verbos que la política solo reconoce por AST).
    """
    plan = qp.classify("/*!40101 GRANT ALL ON *.* TO 'x'@'%' */", engine=MYSQL)
    assert plan.statements == ()
    assert plan.is_blocked
    assert any(r.code == "dcl_grant_revoke" for r in plan.reasons)


# --------------------------------------------------------------------------- #
# Parametrización por motor: MariaDB no se probaba en NINGÚN caso existente       #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "sql,expected",
    [
        ("SELECT * FROM t", qp.READ),
        ("UPDATE t SET a = 1", qp.WRITE),
        ("DROP TABLE t", qp.DDL),
        ("GRANT ALL ON *.* TO 'x'@'%'", qp.BLOCKED),
    ],
)
def test_mariadb_se_clasifica_igual_que_mysql(sql, expected):
    """
    ``mariadb`` se mapea al dialecto ``mysql`` de sqlglot (``_SQLGLOT_DIALECT``);
    ningún test existente ejercitaba ``engine='mariadb'`` directamente. Lock-in de
    esa equivalencia (si algún día MariaDB tuviera su propio dialecto en sqlglot,
    este test avisaría si el mapeo deja de ser transparente).
    """
    assert qp.classify(sql, engine="mariadb").danger == expected


def test_deteccion_de_la_propia_base_del_gateway():
    """Se compara por identidad FÍSICA: mismo nombre en otro host no es el gateway."""
    args = {"gateway_host": "db.interno", "gateway_port": 3306, "gateway_database": "gw"}
    assert qp.is_gateway_metadata_target(
        host="DB.Interno", port="3306", database="gw", **args
    )
    assert not qp.is_gateway_metadata_target(
        host="otro.host", port=3306, database="gw", **args
    )
    assert not qp.is_gateway_metadata_target(
        host="db.interno", port=3307, database="gw", **args
    )
    assert not qp.is_gateway_metadata_target(
        host="db.interno", port=3306, database="otra", **args
    )


# --------------------------------------------------------------------------- #
# Comentario ejecutable de MariaDB (``/*M! … */``)                             #
# --------------------------------------------------------------------------- #
# Agujero real: ``_scan_normalize`` reconocía ``/*!`` (MySQL) pero NO ``/*M!``, el prefijo
# EXCLUSIVO de MariaDB. Su contenido se descartaba como comentario común, la blocklist
# nunca lo veía y el motor lo ejecutaba igual. Con la credencial pseudo-root eso es
# escritura de archivo arbitraria en el host de la base del cliente.


@pytest.mark.parametrize(
    "sql,code",
    [
        (
            "SELECT a FROM t WHERE 1=1 /*M!100000 INTO OUTFILE '/tmp/x' */",
            "server_file_access",
        ),
        ("SELECT 1 /*M!100000 ; DROP DATABASE prod */", "database_lifecycle"),
        # Sin número de versión: ``/*M!`` a secas también es ejecutable.
        ("/*M! GRANT ALL ON *.* TO 'x'@'%' */", "dcl_grant_revoke"),
    ],
)
def test_el_comentario_ejecutable_de_mariadb_no_evade_la_blocklist(sql, code):
    plan = qp.classify(sql, engine="mariadb")
    assert plan.danger == qp.BLOCKED
    assert code in [r.code for r in plan.reasons]


def test_el_comentario_ejecutable_de_mariadb_tambien_se_lee_en_mysql():
    """
    Fail-closed: un MariaDB dado de alta como ``mysql`` es un error de inventario
    frecuente, y conservar texto de más solo puede sobre-bloquear, nunca dejar pasar.
    """
    plan = qp.classify("SELECT 1 /*M!100000 ; DROP DATABASE prod */", engine=MYSQL)
    assert plan.danger == qp.BLOCKED


def test_el_contenido_de_un_comentario_de_mariadb_se_conserva_como_codigo():
    scanned = qp._scan_normalize(
        "SELECT a FROM t WHERE 1=1 /*M!100000 INTO OUTFILE '/tmp/x' */", engine="mariadb"
    )
    assert "INTO OUTFILE" in scanned
    # El número de versión se descarta: si quedara, desplazaría los patrones anclados
    # en ``^`` y la evasión seguiría en pie por otra puerta.
    assert "100000" not in scanned


def test_un_comentario_de_bloque_normal_sigue_descartandose():
    """El fix no puede convertir TODO comentario en código: solo los ejecutables."""
    assert qp._scan_normalize("SELECT 1 /* DROP DATABASE prod */", engine="mysql") == (
        "SELECT 1"
    )
