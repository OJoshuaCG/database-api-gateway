"""
``export_spec``: vocabulario, selección, subconjunto de datos, matriz de compatibilidad,
filtro de filas y saneado del nombre de archivo.

Cubre los casos 1, 2, 3 y 6 del §13 del diseño. El caso 3 ("saneamiento") se prueba acá
en lo que F1 posee — la plantilla del nombre de archivo y el gobierno de las opciones de
saneado por la matriz —; el saneado del DDL emitido (comentarios, ``DEFINER``,
``AUTO_INCREMENT``, opciones de motor) pertenece al writer y se prueba con él.
"""

import pytest

from app.exceptions import AppHttpException
from app.services.db_admin.export_spec import (
    CODE_INCOMPATIBLE_OPTION,
    CODE_INVALID_ROW_FILTER,
    DEFAULT_FILENAME_TEMPLATE,
    ERROR_CODES,
    FILENAME_TOKENS,
    CatalogObject,
    Compression,
    DataOptions,
    DataSelectionMode,
    DefinerMode,
    Delivery,
    EntityDdl,
    ExportSpec,
    Format,
    InsertVariant,
    Organization,
    OutputOptions,
    SanitizeOptions,
    ScopeDdl,
    Selection,
    SelectionMode,
    StructureOptions,
    blocking,
    build_row_select_sql,
    check_data_subset,
    compatibility_matrix,
    raise_for_incompatibilities,
    resolve_definer,
    resolve_selection,
    sanitize_filename,
    sanitize_filename_template,
    validate_compatibility,
    validate_row_filter,
)

CATALOG = (
    CatalogObject("table", "orders"),
    CatalogObject("table", "order_items"),
    CatalogObject("table", "sessions"),
    CatalogObject("table", "audit_log"),
    CatalogObject("table", "tmp_import"),
    CatalogObject("table", "_gw_v_tienda"),
    CatalogObject("table", "_gw_stg_ab12"),
    CatalogObject("view", "v_orders"),
    CatalogObject("routine", "sp_recalcular"),
)


def _names(resolved):
    return list(resolved.names)


# --------------------------------------------------------------------------- #
# Caso 1 — regla de dependencia: el estado inválido NO ES REPRESENTABLE         #
# --------------------------------------------------------------------------- #
def test_no_existe_un_valor_que_elimine_sin_crear():
    """
    Test de TIPO, no de validación (§13.1): la regla "si se pide eliminación, la creación
    se incluye siempre" es el enumerado. Todo valor que destruye, crea.
    """
    for enum_cls in (ScopeDdl, EntityDdl):
        values = {m.value for m in enum_cls}
        assert values == {"NONE", "CREATE", "DROP_CREATE", "CREATE_IF_NOT_EXISTS"}
        for value in values:
            if "DROP" in value:
                assert "CREATE" in value, value


def test_las_dos_idempotencias_son_valores_distintos():
    """``DROP_CREATE`` y ``CREATE_IF_NOT_EXISTS`` no son opuestos por accidente (§4.1)."""
    assert ScopeDdl.DROP_CREATE != ScopeDdl.CREATE_IF_NOT_EXISTS


def test_los_enums_serializan_a_su_valor():
    """StrEnum y no ``(str, Enum)``: ``str(ScopeDdl.NONE)`` debe ser ``'NONE'``."""
    assert str(ScopeDdl.NONE) == "NONE"
    assert str(Format.sql) == "sql"
    assert f"{Delivery.inline}" == "inline"


def test_from_dict_reconstruye_el_spec_y_falla_cerrado():
    spec = ExportSpec.from_dict(
        {
            "format": "sql",
            "structure": {"scope_ddl": "DROP_CREATE", "confirm_scope_drop": "tienda"},
            "data": {"mode": "all", "per_object": {"orders": {"limit": 10}}},
        }
    )
    assert spec.structure.scope_ddl is ScopeDdl.DROP_CREATE
    assert spec.data.per_object["orders"].limit == 10
    # Los defaults de las rutas no enviadas siguen valiendo.
    assert spec.output.delivery is Delivery.file

    with pytest.raises(AppHttpException) as err:
        ExportSpec.from_dict({"format": "parquet"})
    assert err.value.public_context["code"] == CODE_INCOMPATIBLE_OPTION
    assert err.value.public_context["field"] == "format"


def test_los_codigos_de_error_son_el_conjunto_del_diseno():
    assert ERROR_CODES == {
        "export.incompatible_option",
        "export.data_without_structure",
        "export.missing_dependencies",
        "export.invalid_row_filter",
        "export.inline_too_large",
        "export.fingerprint_changed",
        "export.artifact_expired",
        "export.artifact_consumed",
        "export.quota_exceeded",
        # Destino no exportable por lo que ES (hoy: la base de metadatos del gateway).
        "export.scope_not_allowed",
    }


# --------------------------------------------------------------------------- #
# Caso 2 — selección: modos, patrones, subconjunto                             #
# --------------------------------------------------------------------------- #
def test_all_excluye_las_tablas_internas_del_gateway():
    """Es el fix del incidente de ``_gw_v_*``: nunca entran en un artefacto."""
    resolved = resolve_selection(CATALOG, Selection(mode=SelectionMode.all))
    assert "_gw_v_tienda" not in _names(resolved)
    assert "_gw_stg_ab12" not in _names(resolved)
    assert set(resolved.excluded_internal) == {"_gw_v_tienda", "_gw_stg_ab12"}


def test_filtro_por_tipo():
    resolved = resolve_selection(CATALOG, Selection(types=("view", "routine")))
    assert _names(resolved) == ["v_orders", "sp_recalcular"]


def test_mode_include_es_interseccion():
    resolved = resolve_selection(
        CATALOG, Selection(mode=SelectionMode.include, names=("orders", "v_orders"))
    )
    assert _names(resolved) == ["orders", "v_orders"]


def test_mode_all_except_resta():
    resolved = resolve_selection(
        CATALOG,
        Selection(mode=SelectionMode.all_except, names=("sessions", "audit_log")),
    )
    assert "sessions" not in _names(resolved)
    assert "orders" in _names(resolved)


def test_nombres_inexistentes_se_reportan_no_se_ignoran():
    resolved = resolve_selection(
        CATALOG, Selection(mode=SelectionMode.include, names=("orders", "no_existe"))
    )
    assert resolved.unknown_names == ("no_existe",)
    assert _names(resolved) == ["orders"]


def test_pedir_una_tabla_interna_por_nombre_no_la_incluye():
    resolved = resolve_selection(
        CATALOG, Selection(mode=SelectionMode.include, names=("_gw_v_tienda",))
    )
    assert _names(resolved) == []
    # No es "desconocida": existe, pero es contabilidad del gateway.
    assert resolved.unknown_names == ()


def test_include_patterns_filtran_por_glob():
    resolved = resolve_selection(
        CATALOG, Selection(types=("table",), include_patterns=("order*",))
    )
    assert _names(resolved) == ["orders", "order_items"]


def test_exclude_patterns_ganan_sobre_include():
    """§5.1: la exclusión gana. ``order*`` incluye, ``*_items`` saca."""
    resolved = resolve_selection(
        CATALOG,
        Selection(
            types=("table",),
            include_patterns=("order*",),
            exclude_patterns=("*_items",),
        ),
    )
    assert _names(resolved) == ["orders"]


def test_exclude_pattern_sobre_una_seleccion_explicita():
    resolved = resolve_selection(
        CATALOG,
        Selection(
            mode=SelectionMode.include,
            names=("orders", "tmp_import"),
            exclude_patterns=("tmp_*",),
        ),
    )
    assert _names(resolved) == ["orders"]


def test_los_patrones_distinguen_mayusculas():
    """``fnmatchcase``: un mismo spec no puede resolver distinto según el SO."""
    resolved = resolve_selection(CATALOG, Selection(include_patterns=("ORDER*",)))
    assert _names(resolved) == []


def test_se_preserva_el_orden_del_catalogo():
    """El determinismo byte a byte de §8.3 depende de no reordenar acá."""
    resolved = resolve_selection(
        CATALOG,
        Selection(mode=SelectionMode.include, names=("v_orders", "orders", "sessions")),
    )
    assert _names(resolved) == ["orders", "sessions", "v_orders"]


def test_data_selection_solo_mira_tablas():
    data = DataOptions(mode=DataSelectionMode.all)
    resolved = resolve_selection(CATALOG, data.selection)
    assert "v_orders" not in _names(resolved)
    assert "orders" in _names(resolved)


def test_data_mode_none_no_selecciona_nada():
    resolved = resolve_selection(CATALOG, DataOptions().selection)
    assert _names(resolved) == []


# --- datos ⊆ estructura (§5.3) --------------------------------------------- #
def test_datos_fuera_de_la_estructura_se_reportan():
    structure = resolve_selection(
        CATALOG, Selection(mode=SelectionMode.include, names=("orders",))
    )
    data = resolve_selection(
        CATALOG,
        DataOptions(
            mode=DataSelectionMode.include, names=("orders", "sessions")
        ).selection,
    )
    spec = ExportSpec(structure=StructureOptions(entity_ddl=EntityDdl.CREATE))
    assert check_data_subset(structure, data, spec) == ["sessions"]


def test_subconjunto_valido_no_reporta_nada():
    structure = resolve_selection(CATALOG, Selection(types=("table",)))
    data = resolve_selection(CATALOG, DataOptions(mode=DataSelectionMode.all).selection)
    spec = ExportSpec(structure=StructureOptions(entity_ddl=EntityDdl.CREATE))
    assert check_data_subset(structure, data, spec) == []


def test_excepcion_solo_datos_desactiva_la_restriccion():
    """
    §5.3: con ``scope_ddl`` y ``entity_ddl`` en ``NONE`` la exportación es "solo datos" y
    la restricción no aplica — es la única forma en que csv/json pueden existir.
    """
    structure = resolve_selection(
        CATALOG, Selection(mode=SelectionMode.include, names=())
    )
    data = resolve_selection(
        CATALOG, DataOptions(mode=DataSelectionMode.include, names=("orders",)).selection
    )
    solo_datos = ExportSpec(
        structure=StructureOptions(scope_ddl=ScopeDdl.NONE, entity_ddl=EntityDdl.NONE)
    )
    assert check_data_subset(structure, data, solo_datos) == []


def test_check_data_subset_acepta_solo_las_opciones_de_estructura():
    structure = resolve_selection(CATALOG, Selection(mode=SelectionMode.include))
    data = resolve_selection(
        CATALOG, DataOptions(mode=DataSelectionMode.include, names=("orders",)).selection
    )
    opciones = StructureOptions(entity_ddl=EntityDdl.CREATE)
    assert check_data_subset(structure, data, opciones) == ["orders"]


# --------------------------------------------------------------------------- #
# Caso 6 — matriz de compatibilidad: publicada Y exigida                       #
# --------------------------------------------------------------------------- #
def test_la_matriz_publicada_tiene_la_forma_del_contrato():
    matrix = compatibility_matrix()
    assert matrix, "la matriz no puede estar vacía"
    for rule in matrix:
        assert set(rule) == {"when", "forbids", "requires", "reason", "blocking", "code"}
        assert rule["code"] in ERROR_CODES
        assert rule["reason"].strip()


def test_un_spec_por_defecto_es_compatible():
    assert validate_compatibility(ExportSpec()) == []


def test_csv_no_admite_estructura():
    spec = ExportSpec(
        format=Format.csv,
        structure=StructureOptions(entity_ddl=EntityDdl.CREATE),
        output=OutputOptions(organization=Organization.per_object),
        data=DataOptions(insert_variant=InsertVariant.none),
        sanitize=SanitizeOptions(session_preamble=False),
    )
    fields = {i.field for i in validate_compatibility(spec)}
    assert "structure.entity_ddl" in fields


def test_csv_error_accionable_con_public_context():
    """§11.2: código estable + opción culpable + valores admitidos, siempre visibles."""
    spec = ExportSpec(
        format=Format.csv,
        structure=StructureOptions(entity_ddl=EntityDdl.CREATE),
        output=OutputOptions(organization=Organization.per_object),
        data=DataOptions(insert_variant=InsertVariant.none),
        sanitize=SanitizeOptions(session_preamble=False),
    )
    with pytest.raises(AppHttpException) as err:
        raise_for_incompatibilities(validate_compatibility(spec))
    ctx = err.value.public_context
    assert ctx["code"] == CODE_INCOMPATIBLE_OPTION
    assert ctx["field"] == "structure.entity_ddl"
    assert ctx["format"] == "csv"
    assert ctx["allowed"] == ["NONE"]
    assert err.value.status_code == 422


def test_csv_exige_un_archivo_por_tabla():
    spec = ExportSpec(
        format=Format.csv,
        structure=StructureOptions(entity_ddl=EntityDdl.NONE),
        data=DataOptions(insert_variant=InsertVariant.none),
        sanitize=SanitizeOptions(session_preamble=False),
        output=OutputOptions(organization=Organization.single),
    )
    fields = {i.field for i in validate_compatibility(spec)}
    assert fields == {"output.organization"}


def test_csv_bien_configurado_pasa():
    spec = ExportSpec(
        format=Format.csv,
        structure=StructureOptions(scope_ddl=ScopeDdl.NONE, entity_ddl=EntityDdl.NONE),
        data=DataOptions(
            mode=DataSelectionMode.all, insert_variant=InsertVariant.none
        ),
        sanitize=SanitizeOptions(session_preamble=False),
        output=OutputOptions(organization=Organization.per_object),
    )
    assert validate_compatibility(spec) == []


@pytest.mark.parametrize("fmt", [Format.json, Format.ndjson])
def test_json_y_ndjson_tampoco_llevan_estructura_ejecutable(fmt):
    spec = ExportSpec(format=fmt, structure=StructureOptions(entity_ddl=EntityDdl.CREATE))
    fields = {i.field for i in validate_compatibility(spec)}
    assert "structure.entity_ddl" in fields


def test_inline_no_admite_multiarchivo_ni_compresion():
    spec = ExportSpec(
        output=OutputOptions(
            delivery=Delivery.inline,
            organization=Organization.per_object,
            split_max_bytes=1024,
            compression=Compression.gzip,
        )
    )
    fields = {i.field for i in validate_compatibility(spec)}
    assert fields == {
        "output.organization",
        "output.split_max_bytes",
        "output.compression",
    }


def test_gzip_no_es_un_contenedor():
    spec = ExportSpec(
        output=OutputOptions(
            compression=Compression.gzip, organization=Organization.per_object
        )
    )
    fields = {i.field for i in validate_compatibility(spec)}
    assert "output.organization" in fields


def test_drop_create_exige_confirmacion_explicita():
    spec = ExportSpec(structure=StructureOptions(scope_ddl=ScopeDdl.DROP_CREATE))
    items = validate_compatibility(spec)
    assert any(i.field == "structure.confirm_scope_drop" for i in items)

    confirmado = ExportSpec(
        structure=StructureOptions(
            scope_ddl=ScopeDdl.DROP_CREATE, confirm_scope_drop="tienda"
        )
    )
    assert validate_compatibility(confirmado) == []


def test_definer_replace_exige_el_valor():
    spec = ExportSpec(sanitize=SanitizeOptions(definer=DefinerMode.replace))
    assert [i.field for i in validate_compatibility(spec)] == ["sanitize.definer_value"]

    completo = ExportSpec(
        sanitize=SanitizeOptions(
            definer=DefinerMode.replace, definer_value="'app'@'localhost'"
        )
    )
    assert validate_compatibility(completo) == []


# --- reglas que dependen del MOTOR (§7.1) ---------------------------------- #
@pytest.mark.parametrize("modo", [DefinerMode.omit, DefinerMode.replace])
def test_postgresql_no_tiene_definer(modo):
    spec = ExportSpec(
        sanitize=SanitizeOptions(definer=modo, definer_value="postgres")
    )
    items = validate_compatibility(spec, engine="postgresql")
    assert any(i.field == "sanitize.definer" for i in items)


def test_definer_keep_es_no_op_en_postgresql():
    spec = ExportSpec(sanitize=SanitizeOptions(definer=DefinerMode.keep))
    assert validate_compatibility(spec, engine="postgresql") == []


def test_en_mysql_el_definer_se_puede_omitir():
    spec = ExportSpec(sanitize=SanitizeOptions(definer=DefinerMode.omit))
    assert validate_compatibility(spec, engine="mysql") == []


def test_sin_motor_las_reglas_de_motor_no_se_evaluan():
    """No se valida "a favor" con un motor supuesto: simplemente no se evalúan."""
    spec = ExportSpec(sanitize=SanitizeOptions(definer=DefinerMode.omit))
    assert validate_compatibility(spec) == []


def test_postgresql_drop_database_no_va_en_transaccion():
    spec = ExportSpec(
        structure=StructureOptions(
            scope_ddl=ScopeDdl.DROP_CREATE, confirm_scope_drop="tienda"
        ),
        sanitize=SanitizeOptions(transaction_wrap=True),
    )
    items = validate_compatibility(spec, engine="postgresql")
    assert any(i.field == "sanitize.transaction_wrap" and i.blocking for i in items)


def test_postgresql_drop_database_deja_un_aviso_no_bloqueante():
    spec = ExportSpec(
        structure=StructureOptions(
            scope_ddl=ScopeDdl.DROP_CREATE, confirm_scope_drop="tienda"
        ),
        # En PostgreSQL el default aplicable de ``definer`` es ``keep`` (ver el test que
        # sigue); si no se fija, este spec traería además esa incompatibilidad.
        sanitize=SanitizeOptions(definer=DefinerMode.keep),
    )
    items = validate_compatibility(spec, engine="postgresql")
    assert items and blocking(items) == []
    raise_for_incompatibilities(items)  # un aviso no aborta


def test_el_spec_por_defecto_es_valido_en_los_tres_motores():
    """
    El default de ``sanitize.definer`` es ``auto`` justamente para esto: un ``POST`` con
    cuerpo ``{}`` —la llamada canónica "exportá esta base"— tiene que pasar en CUALQUIER
    motor. Con el default anterior (``omit``) daba 422 contra PostgreSQL, donde la matriz
    prohíbe ``omit``/``replace``.
    """
    por_defecto = ExportSpec()
    for engine in ("mysql", "mariadb", "postgresql"):
        assert validate_compatibility(por_defecto, engine=engine) == []


def test_omit_explicito_sigue_siendo_422_en_postgresql():
    """``auto`` arregla el DEFAULT, no la regla: pedir ``omit`` a mano sigue rechazándose."""
    explicito = ExportSpec(sanitize=SanitizeOptions(definer=DefinerMode.omit))
    assert blocking(validate_compatibility(explicito, engine="postgresql"))


@pytest.mark.parametrize(
    ("engine", "esperado"),
    [
        ("mysql", DefinerMode.omit),
        ("mariadb", DefinerMode.omit),
        ("postgresql", DefinerMode.keep),
        (None, DefinerMode.omit),  # sin motor conocido: el caso de la familia MySQL
    ],
)
def test_resolve_definer_traduce_auto_por_motor(engine, esperado):
    assert resolve_definer(DefinerMode.auto, engine) is esperado


@pytest.mark.parametrize(
    "modo", [DefinerMode.keep, DefinerMode.omit, DefinerMode.replace]
)
def test_resolve_definer_no_toca_un_valor_explicito(modo):
    # Un ``omit`` explícito tiene que llegar a la matriz para que lo rechace en PostgreSQL;
    # resolverlo en silencio a otra cosa sería aplicar algo que el usuario no pidió.
    assert resolve_definer(modo, "postgresql") is modo


def test_se_reportan_todas_las_incompatibilidades_no_solo_la_primera():
    spec = ExportSpec(
        format=Format.csv,
        structure=StructureOptions(
            scope_ddl=ScopeDdl.CREATE, entity_ddl=EntityDdl.CREATE
        ),
    )
    items = validate_compatibility(spec)
    assert len({i.field for i in items}) >= 3


# --------------------------------------------------------------------------- #
# Filtro de filas (§9.2)                                                       #
# --------------------------------------------------------------------------- #
COLS = ("id", "total", "created_at", "status")


@pytest.mark.parametrize(
    "where",
    [
        "created_at >= '2026-01-01'",
        "total > 100 AND status = 'paid'",
        "status IN ('paid', 'shipped')",
        "id BETWEEN 1 AND 1000",
        "created_at IS NOT NULL",
        "orders.total > 0",
    ],
)
def test_filtros_de_lectura_validos(where):
    validate_row_filter(where, "orders", COLS, "mysql")


def _rechaza(where, engine="mysql", table="orders"):
    with pytest.raises(AppHttpException) as err:
        validate_row_filter(where, table, COLS, engine)
    ctx = err.value.public_context
    assert err.value.status_code == 422
    assert ctx["code"] == CODE_INVALID_ROW_FILTER
    assert ctx["field"] == f"data.per_object.{table}.where"
    # El texto del filtro NUNCA se refleja en la respuesta.
    assert where not in str(ctx)
    return ctx["reason"]


def test_filtro_vacio_se_rechaza():
    assert _rechaza("   ") == "empty_filter"


def test_filtro_demasiado_largo_se_rechaza():
    assert _rechaza("id > 0 OR " * 500 + "id > 0") == "too_long"


def test_filtro_con_segunda_sentencia_se_rechaza():
    assert _rechaza("1=1; DROP TABLE orders") in {
        "multiple_statements",
        "not_read_only",
        "unparseable",
    }


def test_filtro_con_subconsulta_a_otra_tabla_se_rechaza():
    assert _rechaza("id IN (SELECT user_id FROM sessions)") in {
        "subquery_not_allowed",
        "foreign_table_reference",
    }


def test_filtro_con_subconsulta_sin_tabla_se_rechaza():
    assert _rechaza("id IN (SELECT 1)") == "subquery_not_allowed"


def test_filtro_contra_information_schema_se_rechaza():
    assert _rechaza("id IN (SELECT 1 FROM information_schema.tables)") in {
        "subquery_not_allowed",
        "foreign_table_reference",
    }


def test_filtro_que_referencia_otra_base_se_rechaza():
    assert _rechaza("otra_db.orders.id > 0") in {
        "foreign_column_qualifier",
        "foreign_table_reference",
        "not_read_only",
        "unparseable",
    }


def test_filtro_que_cierra_el_literal_y_ejecuta_dcl_se_rechaza():
    assert _rechaza("1=1 UNION SELECT 1; GRANT ALL ON *.* TO 'x'@'%'") in {
        "multiple_statements",
        "not_read_only",
        "unparseable",
    }


def test_filtro_con_lectura_de_archivos_se_rechaza_en_postgresql():
    assert _rechaza("id > 0 AND pg_read_file('/etc/passwd') IS NOT NULL", "postgresql") in {
        "not_read_only",
        "unparseable",
    }


def test_filtro_ilegible_se_rechaza():
    assert _rechaza("id >>>= ((") in {"unparseable", "not_read_only"}


def test_el_filtro_se_valida_sobre_la_consulta_real_no_sobre_el_fragmento():
    """
    Un fragmento que cierra el paréntesis del ``WHERE`` y abre otra cláusula solo se ve
    cuando se arma la consulta completa. Validar el fragmento suelto no alcanza.
    """
    assert _rechaza("1=1 INTO OUTFILE '/tmp/x'") in {
        "not_read_only",
        "unparseable",
        "multiple_statements",
    }


# --------------------------------------------------------------------------- #
# Caso 3 — saneamiento del nombre de archivo (§9.2)                            #
# --------------------------------------------------------------------------- #
def test_sanitize_filename_neutraliza_el_recorrido_de_rutas():
    assert "/" not in sanitize_filename("../../etc/passwd")
    assert "\\" not in sanitize_filename("..\\windows\\system32")
    assert sanitize_filename("C:/tmp/x") == "C__tmp_x"


def test_sanitize_filename_tiene_respaldo_y_tope():
    assert sanitize_filename("") == "export"
    assert sanitize_filename("...") == "export"
    assert sanitize_filename("...", fallback="db") == "db"
    assert len(sanitize_filename("a" * 500)) == 120


def test_plantilla_sustituye_los_tokens_de_la_whitelist():
    name = sanitize_filename_template(
        DEFAULT_FILENAME_TEMPLATE,
        {"database": "tienda", "date": "2026-08-16", "job_id": "42"},
    )
    assert name == "tienda-2026-08-16-42"


def test_plantilla_admite_todos_los_tokens_publicados():
    tokens = dict.fromkeys(FILENAME_TOKENS, "x")
    template = "-".join(f"{{{t}}}" for t in FILENAME_TOKENS)
    assert sanitize_filename_template(template, tokens) == "-".join("x" * len(tokens))


def test_plantilla_rechaza_un_token_desconocido():
    with pytest.raises(AppHttpException) as err:
        sanitize_filename_template("{database}-{secret}", {"database": "t"})
    ctx = err.value.public_context
    assert ctx["code"] == CODE_INCOMPATIBLE_OPTION
    assert ctx["field"] == "output.filename_template"
    assert ctx["unknown_tokens"] == ["secret"]


def test_plantilla_rechaza_llaves_sueltas():
    with pytest.raises(AppHttpException):
        sanitize_filename_template("{database", {"database": "t"})


def test_el_valor_de_un_token_no_puede_escaparse_del_directorio():
    name = sanitize_filename_template(
        "{database}", {"database": "../../../etc/cron.d/evil"}
    )
    assert "/" not in name


def test_plantilla_vacia_cae_en_la_por_defecto():
    name = sanitize_filename_template(
        "  ", {"database": "tienda", "date": "d", "job_id": "1"}
    )
    assert name == "tienda-d-1"


# --------------------------------------------------------------------------- #
# B1(b)/B2 — comentarios en el filtro y "lo validado es lo ejecutado"          #
# --------------------------------------------------------------------------- #
# La garantía central de ``validate_row_filter`` (el AST tiene que nombrar EXACTAMENTE la
# tabla, sin subconsultas ni UNION) se apoya en sqlglot, que **no tokeniza el contenido de
# un ``/*! … */``** — el motor sí lo ejecuta. Y un filtro terminado en comentario de línea
# comentaba el ``ORDER BY`` y el ``LIMIT`` que venían detrás. Los comentarios se rechazan
# de plano: un filtro de exportación no tiene ningún uso legítimo para uno.


@pytest.mark.parametrize(
    "where",
    [
        "1=1 /*!50000 UNION SELECT user,authentication_string FROM mysql.user */",
        "1=1 /*M!100000 INTO OUTFILE '/var/lib/mysql-files/x' */",
        "1=1 -- ",
        "1=1 #",
        "1=1 /* nota */",
        "id > 0 -- comentario al final",
    ],
)
def test_el_filtro_rechaza_cualquier_comentario(where):
    assert _rechaza(where) == "comment_not_allowed"


def test_en_postgresql_la_almohadilla_no_es_comentario():
    """
    ``#`` es el XOR de enteros en PostgreSQL: prohibirlo ahí sería prohibir un operador
    legítimo (mismo matiz que ``query_policy._scan_normalize``).
    """
    validate_row_filter("id # 0 > 0", "orders", COLS, "postgresql")
    # ...pero en la familia MySQL sí abre un comentario y se rechaza.
    assert _rechaza("id # 0 > 0") == "comment_not_allowed"


def test_lo_validado_incluye_el_order_by_y_el_limit():
    """
    El validador arma la cadena FINAL, no un prefijo. Sin esto, ``1=1 -- `` pasaba la
    validación y en ejecución comentaba la cola de la sentencia: la tabla salía entera,
    sin orden, ignorando el ``limit`` que el ``confirm_token`` hasheó.
    """
    sql = build_row_select_sql(
        "mysql", "orders", COLS, where="id > 5", order_by=["id"], limit=10
    )
    assert sql == (
        "SELECT `id`, `total`, `created_at`, `status` FROM `orders` "
        "WHERE (id > 5) ORDER BY `id` LIMIT 10"
    )


def test_el_filtro_va_entre_parentesis():
    """Segunda defensa: un ``OR`` suelto no puede comerse la precedencia de la cola."""
    sql = build_row_select_sql("mysql", "orders", ("id",), where="a=1 OR b=2", limit=3)
    assert "WHERE (a=1 OR b=2)" in sql


# --------------------------------------------------------------------------- #
# R4 — whitelist de codificación de archivo                                    #
# --------------------------------------------------------------------------- #
# El artefacto se codifica POR TROZO: un códec con estado escribe su BOM en cada ``write``
# y el archivo sale corrupto, con un sha256 que igual lo declara íntegro.


@pytest.mark.parametrize("enc", ["utf-8", "utf-8-sig", "latin-1", "cp1252", "UTF8", "latin1"])
def test_codificaciones_admitidas(enc):
    spec = ExportSpec(output=OutputOptions(file_encoding=enc))
    assert not [
        i for i in validate_compatibility(spec) if i.field == "output.file_encoding"
    ]


@pytest.mark.parametrize("enc", ["utf-16", "utf-32", "utf-16-le", "cualquier-cosa", ""])
def test_codificaciones_con_estado_o_desconocidas_se_rechazan(enc):
    spec = ExportSpec(output=OutputOptions(file_encoding=enc))
    bad = [i for i in validate_compatibility(spec) if i.field == "output.file_encoding"]
    assert len(bad) == 1
    assert bad[0].code == CODE_INCOMPATIBLE_OPTION
    assert bad[0].blocking is True
    assert "utf-8" in bad[0].detail["allowed"]
