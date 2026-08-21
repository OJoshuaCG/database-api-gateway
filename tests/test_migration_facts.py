"""
Hechos derivados del SQL de una migración (`app/services/db_admin/migration_facts.py`).

El foco está en los casos que el clasificador de la consola SQL NO resuelve por sí solo, que
son justamente los que harían mentir a las insignias de la UI:

- `LOAD DATA` y `COPY` caen en la blocklist (`danger=blocked`), no en `WRITE`.
- `REPLACE INTO` parsea como `exp.Command` genérico → `kind='unknown'`, `danger=ddl`.
- Un `COLLATE` dentro de un literal no es un COLLATE forzado.
- `DELETE` sin `WHERE` es destructivo; con `WHERE`, no.
"""

from app.services.db_admin import migration_facts as mf


def _facts(sql, engine="mysql"):
    return mf.analyze(sql, engine)


# --------------------------------------------------------------------------- #
# Siembra de datos                                                             #
# --------------------------------------------------------------------------- #
def test_insert_es_siembra():
    assert _facts("INSERT INTO t (a) VALUES (1)").has_seed is True


def test_ddl_puro_no_es_siembra():
    assert _facts("CREATE TABLE t (id INT PRIMARY KEY)").has_seed is False


def test_replace_into_es_siembra_aunque_el_clasificador_no_lo_marque():
    facts = _facts("REPLACE INTO t (a) VALUES (1)")
    assert facts.has_seed is True
    # Se documenta la premisa: si algún día sqlglot lo parseara como Insert, este assert
    # cambiaría y habría que revisar el fallback por regex.
    assert facts.statements[0].danger != "write"


def test_load_data_es_siembra_pese_a_estar_bloqueada():
    facts = _facts("LOAD DATA INFILE '/tmp/x.csv' INTO TABLE t")
    assert facts.has_seed is True
    assert facts.statements[0].danger == "blocked"


def test_copy_es_siembra_pese_a_estar_bloqueada():
    facts = _facts("COPY t FROM '/tmp/x.csv'", engine="postgresql")
    assert facts.has_seed is True


def test_baseline_con_ddl_y_datos_detecta_la_siembra():
    """El `danger` del lote es el PEOR (ddl > write): mirar solo el agregado la perdería."""
    facts = _facts("CREATE TABLE t (id INT PRIMARY KEY); INSERT INTO t (id) VALUES (1);")
    assert facts.has_seed is True


# --------------------------------------------------------------------------- #
# COLLATE / CHARACTER SET forzados                                             #
# --------------------------------------------------------------------------- #
def test_collate_explicito_se_detecta():
    facts = _facts("ALTER TABLE t MODIFY c VARCHAR(10) COLLATE utf8mb4_bin")
    assert facts.forced_collations == ("utf8mb4_bin",)


def test_character_set_explicito_se_detecta():
    facts = _facts("CREATE TABLE t (c VARCHAR(10)) DEFAULT CHARACTER SET = utf8mb4")
    assert "utf8mb4" in facts.forced_charsets


def test_collate_dentro_de_un_literal_no_cuenta():
    """Sin enmascarar los literales, este COMMENT daría un falso positivo."""
    facts = _facts("CREATE TABLE t (c VARCHAR(10) COMMENT 'ojo: usa COLLATE utf8mb4_bin')")
    assert facts.forced_collations == ()


def test_collate_entrecomillado_recupera_el_nombre_real():
    """
    La máscara vacía el CONTENIDO de las comillas pero conserva la longitud, así que el
    nombre se recorta del texto original en las mismas posiciones. Sin eso, un
    `COLLATE "es_ES"` de PostgreSQL se leería como una cadena de espacios.
    """
    facts = _facts('CREATE TABLE t (c TEXT COLLATE "es_ES")', engine="postgresql")
    assert facts.forced_collations == ("es_ES",)


def test_collate_repetido_no_se_duplica():
    facts = _facts(
        "ALTER TABLE t MODIFY a VARCHAR(5) COLLATE utf8mb4_bin, "
        "MODIFY b VARCHAR(5) COLLATE utf8mb4_bin"
    )
    assert facts.forced_collations == ("utf8mb4_bin",)


# --------------------------------------------------------------------------- #
# Destructividad                                                               #
# --------------------------------------------------------------------------- #
def test_drop_y_truncate_son_destructivos():
    assert _facts("DROP TABLE t").destructive_statements == (0,)
    assert _facts("TRUNCATE TABLE t").destructive_statements == (0,)


def test_delete_sin_where_es_destructivo_y_con_where_no():
    facts = _facts("DELETE FROM t; DELETE FROM u WHERE id = 1;")
    assert facts.destructive_statements == (0,)


def test_create_no_es_destructivo():
    assert _facts("CREATE TABLE t (id INT PRIMARY KEY)").destructive_statements == ()


# --------------------------------------------------------------------------- #
# Parseo, traducción y tablas referenciadas                                    #
# --------------------------------------------------------------------------- #
def test_sql_roto_reporta_el_error_con_su_posicion():
    facts = _facts("CREATE TABLE (((")
    assert facts.parse_errors
    seq, message = facts.parse_errors[0]
    assert seq == 0
    assert message  # el mensaje de sqlglot es el producto: dice dónde falla
    assert facts.is_valid is False


def test_sql_valido_no_reporta_errores():
    facts = _facts("CREATE TABLE t (id INT PRIMARY KEY)")
    assert facts.parse_errors == ()
    assert facts.is_valid is True


def test_tablas_referenciadas_alimentan_la_verificacion_de_catalogo():
    facts = _facts("ALTER TABLE clientes ADD COLUMN nombre VARCHAR(50)")
    assert "clientes" in facts.referenced_tables


def test_blockers_se_calculan_siempre_hacia_postgresql():
    """
    El `up_sql` se escribe en estilo MySQL: la única dirección donde la traducción puede
    fallar es hacia PostgreSQL. Pasar el motor del destino devolvería lista vacía al validar
    desde el formulario, que es justo cuando más sirve saberlo.
    """
    facts = _facts("ALTER TABLE t MODIFY COLUMN c VARCHAR(20)")
    assert isinstance(facts.postgresql_blockers, tuple)


def test_tabla_interna_del_gateway_se_detecta():
    facts = _facts("SELECT * FROM _gw_v_whatsapp")
    assert facts.gateway_internal_tables


# --------------------------------------------------------------------------- #
# Memoización                                                                  #
# --------------------------------------------------------------------------- #
def test_analyze_es_memoizado_por_sql():
    sql = "CREATE TABLE memo (id INT PRIMARY KEY)"
    assert _facts(sql) is _facts(sql)
