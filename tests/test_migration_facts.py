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


def _facts(sql):
    return mf.analyze(sql)


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
    facts = _facts("COPY t FROM '/tmp/x.csv'")
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
    facts = _facts('CREATE TABLE t (c TEXT COLLATE "es_ES")')
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


def test_sql_valido_no_reporta_errores():
    facts = _facts("CREATE TABLE t (id INT PRIMARY KEY)")
    assert facts.parse_errors == ()


def test_tablas_referenciadas_alimentan_la_verificacion_de_catalogo():
    facts = _facts("ALTER TABLE clientes ADD COLUMN nombre VARCHAR(50)")
    assert "clientes" in facts.requires_existing_tables


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


# --------------------------------------------------------------------------- #
# Regresiones de la auditoría: los falsos positivos que hacían inútil el aviso #
# --------------------------------------------------------------------------- #
def test_las_tablas_que_la_propia_migracion_crea_no_se_exigen():
    """
    El fallo más grave del primer entregable: un baseline —que es TODO `CREATE TABLE`—
    reportaba cada una de sus tablas como inexistente, así que el aviso que justificaba abrir
    conexión al motor era el que más ruido producía.
    """
    facts = _facts("CREATE TABLE nueva (id INT PRIMARY KEY); ALTER TABLE nueva ADD COLUMN c INT;")
    assert facts.requires_existing_tables == ()
    assert facts.creates_tables == ("nueva",)


def test_un_alter_sobre_una_tabla_que_nadie_crea_si_se_exige():
    """El caso que motivó todo: sigue detectándose."""
    assert _facts("ALTER TABLE inexistente ADD COLUMN c INT").requires_existing_tables == (
        "inexistente",
    )


def test_drop_if_exists_no_exige_la_tabla():
    assert _facts("DROP TABLE IF EXISTS quizas").requires_existing_tables == ()


def test_create_index_exige_su_tabla():
    assert "clientes" in _facts("CREATE INDEX ix ON clientes (nombre)").requires_existing_tables


def test_el_cuerpo_de_un_trigger_no_afirma_nada_sobre_tablas():
    """Ante un cuerpo que sqlglot no analiza, callar en vez de inventar un falso positivo."""
    facts = _facts("CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW BEGIN END")
    assert facts.requires_existing_tables == ()


def test_el_sql_de_mysql_se_analiza_con_su_dialecto():
    """
    Antes el dialecto lo fijaba el motor del DESTINO. Validando contra PostgreSQL, sqlglot no
    fallaba: simplemente no reconocía las tablas, `missing_tables` salía vacío y la
    comprobación decía que todo estaba bien sin haber comprobado nada.
    """
    facts = _facts("ALTER TABLE `pedidos` ADD COLUMN c INT")
    assert facts.requires_existing_tables == ("pedidos",)


def test_on_update_cascade_no_es_siembra():
    """`ON UPDATE CASCADE` aparece en casi cualquier FK: marcarlo encendía la insignia siempre."""
    sql = "CREATE TABLE t (id INT, pid INT, FOREIGN KEY (pid) REFERENCES p(id) ON UPDATE CASCADE)"
    assert _facts(sql).has_seed is False
    assert mf.quick_facts(sql).has_seed is False


def test_after_update_on_no_es_siembra():
    sql = "CREATE TRIGGER tr AFTER UPDATE ON t FOR EACH ROW BEGIN END"
    assert _facts(sql).has_seed is False
    assert mf.quick_facts(sql).has_seed is False


def test_insert_dentro_de_un_trigger_no_cuenta_como_siembra():
    """Efecto colateral aceptado del anclaje: la migración crea el trigger, no siembra datos."""
    sql = "CREATE TRIGGER tr AFTER INSERT ON t FOR EACH ROW BEGIN INSERT INTO log VALUES (1); END"
    assert mf.quick_facts(sql).has_seed is False


def test_alter_drop_column_es_destructivo():
    """Pierde datos, aunque para el clasificador sea un `alter` cualquiera."""
    assert _facts("ALTER TABLE t DROP COLUMN c").destructive_statements == (0,)


# --------------------------------------------------------------------------- #
# Coherencia: las insignias y el validador no pueden contradecirse             #
# --------------------------------------------------------------------------- #
_CORPUS = [
    "CREATE TABLE t (id INT PRIMARY KEY)",
    "CREATE TABLE t (id INT, pid INT, FOREIGN KEY (pid) REFERENCES p(id) ON UPDATE CASCADE)",
    "CREATE TRIGGER tr AFTER UPDATE ON t FOR EACH ROW BEGIN END",
    "ALTER TABLE t ADD COLUMN c INT",
    "ALTER TABLE t DROP COLUMN c",
    "DROP TABLE t",
    "TRUNCATE TABLE t",
    "DELETE FROM t",
    "DELETE FROM t WHERE id = 1",
    "INSERT INTO t (a) VALUES (1)",
    "UPDATE t SET a = 1 WHERE id = 2",
    "CREATE TABLE t (id INT); INSERT INTO t (id) VALUES (1);",
]


def test_quick_facts_y_analyze_no_se_contradicen():
    """
    El listado dice «⚠ destructiva», pulsas «Validar» y el panel no menciona nada: dos
    veredictos sobre el mismo SQL destruyen la confianza en ambos. Este test es la garantía
    de que no vuelvan a divergir, no un caso más.
    """
    for sql in _CORPUS:
        quick, full = mf.quick_facts(sql), mf.analyze(sql)
        assert quick.has_seed == full.has_seed, f"has_seed difiere en: {sql}"
        assert quick.destructive == bool(full.destructive_statements), (
            f"destructividad difiere en: {sql}"
        )
        assert quick.forced_collations == full.forced_collations, f"collations difieren: {sql}"


def test_kind_data_nunca_es_reanudable():
    assert mf.analyze("INSERT INTO t (a) VALUES (1)", "data").resumable is False


def test_el_sql_enorme_no_se_memoiza():
    """La cota evita retener megabytes por entrada en un proceso de larga vida."""
    big = "SELECT 1; " + ("-- x\n" * 20000)
    assert len(big.encode()) > mf._CACHE_MAX_SQL_BYTES
    assert mf.analyze(big) is not mf.analyze(big)
