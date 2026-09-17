"""
Tests de la longitud de PREFIJO de índice (``key(191)``) y del ``ROW_FORMAT`` de tabla,
en todo el recorrido: reflexión → DTO → firma del diff → DDL renderizado.

El incidente que los origina: clonar una base de tenant fallaba con
``(1071, 'Specified key was too long; max key length is 3072 bytes')`` al recrear
``wa_wasabi_logs.idx_bucket_key``, definido en el origen como ``(bucket, key(191))``.
El origen era correcto —1784 bytes, con 1288 de margen— pero el clon reconstruía el
índice SIN el prefijo, con lo que pasaba a ``255×4 + 1024×4 = 5116`` bytes.

El 1071 es el caso AFORTUNADO: falla ruidosamente porque la columna es ``varchar(1024)``.
Con una columna más corta el índice se crea sin error sobre la columna completa, y si la
clave es ``UNIQUE`` el destino pasa a aceptar filas que el origen rechazaba — eso ya no es
una diferencia de performance sino de datos. Por eso se cubren las dos rutas de emisión
(``CREATE INDEX`` suelto y ``UNIQUE`` inline del ``CREATE TABLE``) y también la firma del
diff, que sin el prefijo dejaba la deriva invisible para schema-comparison.
"""

import pytest

from app.exceptions.AppHttpException import AppHttpException
from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.dtos import (
    ColumnInfo,
    IndexInfo,
    SchemaSnapshot,
    TableSchema,
    UniqueConstraintInfo,
)
from app.services.db_admin.mysql_adapter import MariaDBAdapter, MySQLAdapter
from app.services.db_admin.schema_diff import (
    _index_signature,
    _unique_signature,
    diff_snapshots,
)


def _adapter(dialect="mysql"):
    # Solo se ejercita el render: instanciar sin __init__ (no abre conexión).
    cls = MariaDBAdapter if dialect == "mariadb" else MySQLAdapter
    a = cls.__new__(cls)
    a.dialect = dialect
    return a


# --------------------------------------------------------------------------- #
# Reflexión: de dónde sale el prefijo                                          #
# --------------------------------------------------------------------------- #
def test_reflection_captures_mysql_prefix_length():
    """``dialect_options['mysql_length']`` → ``IndexInfo.prefix_lengths``."""
    raw = {
        "name": "idx_bucket_key",
        "column_names": ["bucket", "key"],
        "unique": False,
        "dialect_options": {"mysql_length": {"key": 191}},
    }
    assert ServerAdapter._index_from_raw(raw).prefix_lengths == {"key": 191}


def test_reflection_captures_mariadb_prefix_length():
    """
    MariaDB entrega la MISMA opción como ``mariadb_length``.

    SQLAlchemy arma la clave con ``self.name`` del dialecto
    (``dialect_options["%s_length" % self.name]``), así que un match contra la cadena
    literal ``mysql_length`` dejaría el fix sin efecto justo en el motor donde se reportó
    el 1071. Este test es el que fija el match por SUFIJO.
    """
    raw = {
        "name": "idx_bucket_key",
        "column_names": ["bucket", "key"],
        "unique": False,
        "dialect_options": {"mariadb_length": {"key": 191}},
    }
    assert ServerAdapter._index_from_raw(raw).prefix_lengths == {"key": 191}


def test_reflection_without_prefix_leaves_mapping_empty():
    raw = {"name": "idx_created_at", "column_names": ["created_at"], "unique": False}
    assert ServerAdapter._index_from_raw(raw).prefix_lengths == {}


def test_reflection_postgres_options_do_not_become_prefixes():
    """Las opciones de PG siguen mapeando a sus campos y no contaminan el prefijo."""
    raw = {
        "name": "ix",
        "column_names": ["a"],
        "unique": False,
        "dialect_options": {
            "postgresql_using": "gin",
            "postgresql_where": "x IS NULL",
            "postgresql_include": ["b"],
        },
    }
    ix = ServerAdapter._index_from_raw(raw)
    assert ix.prefix_lengths == {}
    assert (ix.method, ix.predicate, ix.include_columns) == ("gin", "x IS NULL", ["b"])


def test_reflection_fulltext_prefix_option_is_not_a_length():
    """
    ``mysql_prefix`` (FULLTEXT/SPATIAL) termina en ``_prefix``, no en ``_length``.

    Comparten la palabra "prefijo" en castellano pero son cosas distintas: uno es el tipo
    de índice y el otro la cantidad de caracteres indexados. Confundirlos emitiría
    ``FULLTEXT`` como si fuera una longitud.
    """
    raw = {
        "name": "ft_body",
        "column_names": ["body"],
        "unique": False,
        "type": "FULLTEXT",
        "dialect_options": {"mysql_prefix": "FULLTEXT"},
    }
    assert ServerAdapter._index_from_raw(raw).prefix_lengths == {}


# --------------------------------------------------------------------------- #
# Render: CREATE INDEX suelto                                                  #
# --------------------------------------------------------------------------- #
def test_create_index_emits_prefix_length():
    """El caso exacto del incidente: 5116 bytes sin el prefijo, 1784 con él."""
    ix = IndexInfo(
        name="idx_bucket_key",
        columns=["bucket", "key"],
        unique=False,
        prefix_lengths={"key": 191},
    )
    assert _adapter()._render_create_index("wa_wasabi_logs", ix) == (
        "CREATE INDEX `idx_bucket_key` ON `wa_wasabi_logs` (`bucket`, `key`(191))"
    )


def test_create_index_without_prefix_is_unchanged():
    """Sin prefijo el DDL es idéntico al de antes del fix: cero regresión."""
    ix = IndexInfo(name="idx_created_at", columns=["created_at"], unique=False)
    assert _adapter()._render_create_index("t", ix) == (
        "CREATE INDEX `idx_created_at` ON `t` (`created_at`)"
    )


def test_create_unique_index_emits_prefix_length():
    """
    La ruta UNIQUE es la del fallo SILENCIOSO.

    Un ``UNIQUE`` que pierde el prefijo pasa a cubrir la columna completa, lo que
    DEBILITA la restricción: el clon acepta filas que el origen rechazaba.
    """
    ix = IndexInfo(
        name="uq_value", columns=["value"], unique=True, prefix_lengths={"value": 191}
    )
    assert _adapter()._render_create_index("wa_message_buttons", ix) == (
        "CREATE UNIQUE INDEX `uq_value` ON `wa_message_buttons` (`value`(191))"
    )


def test_create_index_mixes_prefixed_and_plain_columns():
    ix = IndexInfo(
        name="idx_mix",
        columns=["a", "b", "c"],
        unique=False,
        prefix_lengths={"a": 10, "c": 20},
    )
    assert "(`a`(10), `b`, `c`(20))" in _adapter()._render_create_index("t", ix)


@pytest.mark.parametrize("bad", [0, -5])
def test_non_positive_prefix_is_dropped_instead_of_emitted(bad):
    """
    Una longitud <= 0 no es SQL válido y no puede venir de la reflexión.

    Se descarta en vez de abortar el clon entero: emitir ``col(0)`` rompería la sentencia,
    y hacer fallar todo el trabajo por un dato que el motor no debería haber devuelto es
    peor que ignorarlo.
    """
    ix = IndexInfo(name="i", columns=["a"], unique=False, prefix_lengths={"a": bad})
    assert _adapter()._render_create_index("t", ix) == (
        "CREATE INDEX `i` ON `t` (`a`)"
    )


# --------------------------------------------------------------------------- #
# Render: UNIQUE inline del CREATE TABLE                                       #
# --------------------------------------------------------------------------- #
def _table(**kw):
    base = dict(
        database="d",
        table="t",
        columns=[
            ColumnInfo(name="id", type="int", nullable=False, primary_key=True),
            ColumnInfo(name="value", type="varchar(1024)", nullable=False),
        ],
        primary_key=["id"],
        foreign_keys=[],
        indexes=[],
    )
    base.update(kw)
    return TableSchema(**base)


def test_inline_unique_emits_prefix_length():
    """
    La ``UNIQUE`` que va INLINE en el CREATE TABLE es la otra ruta que perdía el prefijo.

    Es una ruta distinta de ``_render_create_index`` porque el diff descarta el índice
    que respalda una unique constraint para no emitir la misma clave dos veces
    (``1061 Duplicate key name``): si solo se arreglara el CREATE INDEX, este caso
    seguiría roto.
    """
    tbl = _table(
        unique_constraints=[
            UniqueConstraintInfo(
                name="uq_value", columns=["value"], prefix_lengths={"value": 191}
            )
        ]
    )
    assert "CONSTRAINT `uq_value` UNIQUE (`value`(191))" in _adapter()._render_create_table(tbl)


def test_inline_unique_without_prefix_is_unchanged():
    tbl = _table(
        unique_constraints=[UniqueConstraintInfo(name="uq_value", columns=["value"])]
    )
    assert "CONSTRAINT `uq_value` UNIQUE (`value`)" in _adapter()._render_create_table(tbl)


# --------------------------------------------------------------------------- #
# ROW_FORMAT y COMMENT de tabla                                                #
# --------------------------------------------------------------------------- #
def test_create_table_pins_row_format():
    """
    Sin el pin, el límite de la clave lo decide el ``innodb_default_row_format`` del HOST.

    Ahí el mismo clon entra en un servidor y falla con 1071 en otro: 3072 bytes con
    ``DYNAMIC``/``COMPRESSED``, apenas 767 con ``COMPACT``/``REDUNDANT``.
    """
    tbl = _table(storage_options={"engine": "InnoDB", "row_format": "DYNAMIC"})
    assert _adapter()._render_create_table(tbl).endswith(
        "ENGINE=InnoDB ROW_FORMAT=DYNAMIC"
    )


def test_create_table_without_row_format_is_unchanged():
    tbl = _table(storage_options={"engine": "InnoDB"})
    assert _adapter()._render_create_table(tbl).endswith("ENGINE=InnoDB")


def test_create_table_emits_table_comment():
    """El COMMENT se reflejaba en ``TableSchema.comment`` y no se emitía nunca."""
    tbl = _table(comment="Bitacora de operaciones")
    assert _adapter()._render_create_table(tbl).endswith(
        "COMMENT='Bitacora de operaciones'"
    )


def test_table_comment_with_quotes_is_escaped():
    """El comentario es texto libre del usuario y va interpolado en DDL."""
    tbl = _table(comment="'; DROP TABLE users; --")
    ddl = _adapter()._render_create_table(tbl)
    assert ddl.endswith("COMMENT='''; DROP TABLE users; --'")


def test_table_comment_with_null_byte_is_rejected():
    tbl = _table(comment="\x00")
    with pytest.raises(AppHttpException):
        _adapter()._render_create_table(tbl)


def test_storage_from_row_reads_row_format():
    opts = MySQLAdapter._storage_from_row("InnoDB", "utf8mb4_unicode_ci", "Dynamic")
    assert opts["row_format"] == "Dynamic"
    assert opts["engine"] == "InnoDB"
    assert opts["charset"] == "utf8mb4"


def test_storage_from_row_omits_absent_row_format():
    """Una vista devuelve NULL en ROW_FORMAT: la clave no debe aparecer vacía."""
    assert "row_format" not in MySQLAdapter._storage_from_row("InnoDB", None, None)


# --------------------------------------------------------------------------- #
# Firma del diff: que la deriva DEJE de ser invisible                          #
# --------------------------------------------------------------------------- #
def test_diff_signature_distinguishes_prefixed_from_plain_index():
    """
    Sin el prefijo en la firma, el diff veía ``idx(key(191))`` IDÉNTICO a ``idx(key)``.

    O sea: la herramienta que existe para detectar la deriva del clon se quedaba callada
    justo ante la deriva que este fix corrige.
    """
    con = IndexInfo(name="i", columns=["key"], unique=False, prefix_lengths={"key": 191})
    sin = IndexInfo(name="i", columns=["key"], unique=False)
    assert _index_signature(con) != _index_signature(sin)


def test_diff_signature_distinguishes_different_prefix_lengths():
    a = IndexInfo(name="i", columns=["key"], unique=False, prefix_lengths={"key": 191})
    b = IndexInfo(name="i", columns=["key"], unique=False, prefix_lengths={"key": 255})
    assert _index_signature(a) != _index_signature(b)


def test_diff_signature_is_stable_across_dict_ordering():
    """
    Un dict no tiene orden estable entre reflexiones.

    Si el orden de inserción entrara en la firma, dos snapshots idénticos se verían
    distintos y el diff emitiría un DROP+CREATE de índice sin ningún cambio real detrás
    — sobre una tabla de producción de un tercero.
    """
    a = IndexInfo(name="i", columns=["a", "b"], unique=False, prefix_lengths={"a": 10, "b": 20})
    b = IndexInfo(name="i", columns=["a", "b"], unique=False, prefix_lengths={"b": 20, "a": 10})
    assert _index_signature(a) == _index_signature(b)


def test_unique_signature_distinguishes_prefixed_from_plain():
    con = UniqueConstraintInfo(name="u", columns=["v"], prefix_lengths={"v": 191})
    sin = UniqueConstraintInfo(name="u", columns=["v"])
    assert _unique_signature(con) != _unique_signature(sin)


def test_signatures_unchanged_when_no_prefixes_on_either_side():
    """Sin prefijos a ambos lados, agregar el campo no altera ninguna comparación."""
    a = IndexInfo(name="i", columns=["x"], unique=False)
    b = IndexInfo(name="i", columns=["x"], unique=False)
    assert _index_signature(a) == _index_signature(b)
    u1 = UniqueConstraintInfo(name="u", columns=["x"])
    u2 = UniqueConstraintInfo(name="u", columns=["x"])
    assert _unique_signature(u1) == _unique_signature(u2)


# --------------------------------------------------------------------------- #
# End-to-end: el pipeline que usa el clon                                      #
# --------------------------------------------------------------------------- #
def test_clone_pipeline_preserves_prefix_row_format_and_comment():
    """
    El recorrido REAL de la fase de estructura del clon:
    ``diff(origen, snapshot vacío)`` → ``render_diff`` (ver ``_build_execution_plan``).

    Reproduce ``wa_wasabi_logs`` del incidente sobre MariaDB, que es donde se reportó.
    """
    tbl = TableSchema(
        database="cirox_012_tenant",
        table="wa_wasabi_logs",
        columns=[
            ColumnInfo(name="id", type="bigint(20) unsigned", nullable=False,
                       primary_key=True, autoincrement=True),
            ColumnInfo(name="bucket", type="varchar(255)", nullable=False),
            ColumnInfo(name="key", type="varchar(1024)", nullable=False),
        ],
        primary_key=["id"],
        foreign_keys=[],
        indexes=[
            IndexInfo(name="idx_bucket_key", columns=["bucket", "key"], unique=False,
                      prefix_lengths={"key": 191}),
        ],
        comment="Bitacora de operaciones Wasabi",
        storage_options={"engine": "InnoDB", "charset": "utf8mb4",
                         "collation": "utf8mb4_unicode_ci", "row_format": "DYNAMIC"},
    )
    src = SchemaSnapshot(database="cirox_012_tenant", source_engine="mariadb", tables=[tbl])
    empty = SchemaSnapshot(database="cirox_099_tenant", source_engine="mariadb", tables=[])

    rendered = _adapter("mariadb").render_diff(diff_snapshots(src, empty))
    by_type = {st.object_type: st.sql for st in rendered}

    # El índice va en una sentencia SEPARADA del CREATE TABLE: es el punto exacto
    # donde el prefijo se perdía, y el que el reporte del incidente marcaba como
    # ``index · wa_wasabi_logs.idx_bucket_key  failed``.
    assert "(`bucket`, `key`(191))" in by_type["index"]
    assert "ROW_FORMAT=DYNAMIC" in by_type["table"]
    assert "COMMENT='Bitacora de operaciones Wasabi'" in by_type["table"]
