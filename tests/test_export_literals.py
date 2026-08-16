"""
Render de literales de la exportación (``sql_literals``).

Cubre el caso 5 del §13 del diseño en lo que le toca a este módulo (valores límite:
nulos vs cadena vacía, binarios, comillas, saltos de línea, multibyte, fechas extremas,
``Decimal`` de precisión arbitraria, ``timedelta``) y la compatibilidad hacia atrás de la
extracción desde ``snapshot_data``.
"""

from datetime import date, datetime, time, timedelta
from decimal import Decimal
from uuid import UUID

import pytest

from app.services.db_admin import snapshot_data, value_json
from app.services.db_admin.export_spec import BinaryEncoding
from app.services.db_admin.sql_literals import (
    BINARY_ENCODINGS,
    UnsupportedValueError,
    render_value,
    render_value_text,
)


# --------------------------------------------------------------------------- #
# La extracción no rompe a nadie                                               #
# --------------------------------------------------------------------------- #
def test_snapshot_data_reexporta_los_nombres_historicos():
    """``sd.render_value`` sigue existiendo: hay tests y adapters que lo usan así."""
    assert snapshot_data.render_value is render_value
    assert snapshot_data.UnsupportedValueError is UnsupportedValueError


def test_binary_encoding_enum_coincide_con_el_render():
    """
    El enumerado del spec y las codificaciones que el render sabe emitir tienen que ser
    el mismo conjunto: si divergen, capabilities publica una opción que revienta al
    escribir la primera fila binaria.
    """
    assert tuple(m.value for m in BinaryEncoding) == BINARY_ENCODINGS


def test_timedelta_usa_el_criterio_compartido():
    assert value_json.format_timedelta(timedelta(hours=-1)) == "-01:00:00"


# --------------------------------------------------------------------------- #
# render_value — literales SQL                                                 #
# --------------------------------------------------------------------------- #
def test_render_value_tipos_basicos_mysql():
    assert render_value(None, "mysql") == "NULL"
    assert render_value(7, "mysql") == "7"
    assert render_value(True, "mysql") == "1"
    assert render_value(False, "mysql") == "0"
    assert render_value("", "mysql") == "''"
    assert render_value("O'Brien", "mysql") == "'O''Brien'"
    assert render_value(b"\x00\xff", "mysql") == "x'00ff'"


def test_render_value_postgres_bool_y_bytea():
    assert render_value(True, "postgresql") == "TRUE"
    assert render_value(b"\xde\xad", "postgresql") == "decode('dead', 'hex')"


def test_render_value_nulo_no_es_cadena_vacia():
    """``NULL`` y ``''`` son valores distintos y el literal tiene que distinguirlos."""
    assert render_value(None, "mysql") != render_value("", "mysql")


def test_render_value_salto_de_linea_y_multibyte():
    assert render_value("a\nb", "mysql") == "'a\nb'"
    assert render_value("emoji 🐘 acentos áé", "postgresql") == "'emoji 🐘 acentos áé'"


def test_render_value_backslash_por_motor():
    # MySQL dobla la barra (sql_mode por defecto la trata como escape).
    assert render_value("a\\b", "mysql") == "'a\\\\b'"
    # PostgreSQL necesita la forma E'' para que la barra sea literal.
    assert render_value("a\\b", "postgresql") == "E'a\\\\b'"


def test_render_value_decimal_conserva_precision():
    value = Decimal("0.1234567890123456789012345678")
    assert render_value(value, "mysql") == "0.1234567890123456789012345678"


def test_render_value_fechas_extremas():
    assert render_value(datetime(1, 1, 1, 0, 0, 0), "mysql") == "'0001-01-01 00:00:00'"
    assert render_value(date(9999, 12, 31), "mysql") == "'9999-12-31'"
    assert render_value(time(23, 59, 59), "mysql") == "'23:59:59'"


def test_render_value_timedelta_es_el_time_de_mysql():
    """``TIME`` llega como ``timedelta``: antes de la extracción esto explotaba."""
    assert render_value(timedelta(hours=838), "mysql") == "'838:00:00'"
    assert render_value(timedelta(hours=-1), "mysql") == "'-01:00:00'"
    assert render_value(timedelta(seconds=3661), "mysql") == "'01:01:01'"


def test_render_value_uuid():
    uid = UUID("12345678-1234-5678-1234-567812345678")
    assert render_value(uid, "postgresql") == "'12345678-1234-5678-1234-567812345678'"


def test_render_value_json_container():
    assert render_value({"a": 1}, "mysql") == """'{"a": 1}'"""


def test_render_value_fail_closed():
    with pytest.raises(UnsupportedValueError):
        render_value(object(), "mysql")
    with pytest.raises(UnsupportedValueError):
        render_value(float("nan"), "mysql")
    with pytest.raises(UnsupportedValueError):
        render_value(Decimal("Infinity"), "mysql")
    with pytest.raises(UnsupportedValueError):
        render_value("a\x00b", "mysql")


# --------------------------------------------------------------------------- #
# render_value_text — csv / json / ndjson                                      #
# --------------------------------------------------------------------------- #
def test_render_value_text_no_agrega_comillas():
    """El escapado del FORMATO lo pone el writer; acá sale el texto crudo."""
    assert render_value_text("O'Brien", binary_encoding="hex") == "O'Brien"
    assert render_value_text("", binary_encoding="hex") == ""


def test_render_value_text_binarios_por_codificacion():
    assert render_value_text(b"\xde\xad", binary_encoding="hex") == "dead"
    assert render_value_text(b"\xde\xad", binary_encoding="base64") == "3q0="


def test_render_value_text_codificacion_invalida_falla_cerrado():
    with pytest.raises(UnsupportedValueError):
        render_value_text(b"\x00", binary_encoding="rot13")


def test_render_value_text_decimal_nunca_float():
    value = Decimal("12345678901234567890.0987654321")
    assert render_value_text(value, binary_encoding="hex") == str(value)


def test_render_value_text_fechas_y_tipos_de_driver():
    assert render_value_text(date(2026, 8, 16), binary_encoding="hex") == "2026-08-16"
    assert (
        render_value_text(datetime(2026, 8, 16, 10, 30), binary_encoding="hex")
        == "2026-08-16 10:30:00"
    )
    assert render_value_text(timedelta(hours=-1), binary_encoding="hex") == "-01:00:00"
    assert render_value_text(True, binary_encoding="hex") == "true"


def test_render_value_text_fail_closed():
    with pytest.raises(UnsupportedValueError):
        render_value_text(object(), binary_encoding="hex")
    with pytest.raises(UnsupportedValueError):
        render_value_text("a\x00b", binary_encoding="hex")
    with pytest.raises(UnsupportedValueError):
        render_value_text(float("inf"), binary_encoding="hex")


def test_render_value_text_none_es_cadena_vacia_documentado():
    """
    Límite CONOCIDO: ``None`` sale como ``''`` y el llamador debe distinguirlo antes.
    El test existe para que el día que alguien cambie el criterio sea una decisión y no
    un descuido.
    """
    assert render_value_text(None, binary_encoding="hex") == ""


# --------------------------------------------------------------------------- #
# El contrato de la SEMILLA no se movió                                        #
# --------------------------------------------------------------------------- #
def test_build_seed_conserva_sus_techos_duros():
    assert snapshot_data.HARD_MAX_ROWS == 5000
    assert snapshot_data.HARD_MAX_BYTES == 5 * 1024 * 1024
    assert snapshot_data.effective_limits(999999, 999999999) == (
        snapshot_data.HARD_MAX_ROWS,
        snapshot_data.HARD_MAX_BYTES,
    )


def test_build_seed_sigue_omitiendo_la_tabla_ante_un_tipo_no_soportado():
    result = snapshot_data.build_seed(
        dialect="mysql",
        table="t",
        columns=["id", "v"],
        pk=["id"],
        rows=[(1, object())],
        mode="upsert",
        batch_rows=10,
        max_rows=100,
        max_bytes=10000,
    )
    assert result.included is False
    assert result.reason.startswith("unsupported_type:")
