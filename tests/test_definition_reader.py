"""
Tests PUROS (sin motor) del núcleo de ``get_definition`` (slice S2 de ``mcp-schema-definitions``):
``definition_reader.build_definition``, el helper de razones por versión de ``readonly_probe`` y los
modelos ``Definition*`` de ``app/schemas/mcp.py``.

Cubre:
- S2.1: ``body_available`` XOR ``unavailable_reason``; ``not_found`` NO es una razón.
- S2.2 / S5.3: tabla de razones por motor y versión (MariaDB 11.2/11.3, MySQL 5.7/8.0.19/8.0.20).
- S2.3: la huella ignora DEFINER y espacios (misma normalización que ``schema_diff``).
- S2.4: la cuenta del DEFINER no aparece en ningún campo; solo ``security``.
- S2.8: tope de 64 KiB sobre el JSON del cuerpo redactado, rechazo ``too_large`` sin truncar.

Lo que NO se verifica acá: las consultas contra un MariaDB/PostgreSQL reales (ver
``tests/test_definition_read_definition.py`` para el adapter con conexión falsa) ni el audit/gate de
la tool (slice S4).

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_definition_reader``
"""

import dataclasses
import hashlib
import json
from typing import get_args

import pytest
from pydantic import ValidationError

from app.schemas.mcp import (
    DefinitionOut,
    DefinitionRefOut,
    DefinitionsOut,
    DefinitionUnavailableReason,
    EventMetaOut,
    ObjectKind,
    RedactionCountOut,
    TriggerMetaOut,
    UnavailableReason,
)
from app.services.db_admin.definition_reader import (
    MAX_DEFINITION_BYTES,
    MAX_DEFINITIONS_PER_CALL,
    DefinitionResult,
    body_fingerprint,
    build_definition,
    json_encoded_size,
)
from app.services.db_admin.definition_redaction import RedactionResult
from app.services.db_admin.dtos import DefinitionRead
from app.services.db_admin.readonly_probe import (
    mariadb_routine_grants_for_version,
    proc_grant_supported,
    routine_body_reason,
)
from app.services.db_admin.schema_diff import normalize_body

_DISPATCHER_BUDGET_BYTES = 512 * 1024


def _identity_redact(text: str) -> RedactionResult:
    """Redactor neutro: aísla el tope y la huella de los patrones de credenciales."""
    return RedactionResult(text=text)


def _view_read(body: str | None, **overrides) -> DefinitionRead:
    fields = {"kind": "view", "name": "v_ventas", "body": body}
    fields.update(overrides)
    return DefinitionRead(**fields)


# --------------------------------------------------------------------------- #
# Constantes de tope                                                           #
# --------------------------------------------------------------------------- #
def test_topes_encajan_en_el_presupuesto_del_dispatcher():
    assert MAX_DEFINITIONS_PER_CALL == 5
    assert MAX_DEFINITION_BYTES == 65536
    assert MAX_DEFINITIONS_PER_CALL * MAX_DEFINITION_BYTES < _DISPATCHER_BUDGET_BYTES


# --------------------------------------------------------------------------- #
# S2.8 — tope de 64 KiB sobre el JSON                                          #
# --------------------------------------------------------------------------- #
def test_json_encoded_size_cuenta_comillas_escapes_y_utf8():
    assert json_encoded_size("") == 2
    assert json_encoded_size("abc") == 5
    assert json_encoded_size("\n") == 4  # "\n" viaja como dos caracteres, más las comillas
    assert json_encoded_size("é") == 4  # dos bytes en UTF-8, más las comillas


def test_cuerpo_justo_en_el_tope_es_disponible():
    body_chars = MAX_DEFINITION_BYTES - 2  # las dos comillas del JSON completan el tope
    body = "a" * body_chars

    result = build_definition(_view_read(body), redact=_identity_redact)

    assert result.body_available is True
    assert result.unavailable_reason is None
    assert result.size_bytes == MAX_DEFINITION_BYTES
    assert result.body == body


def test_cuerpo_un_byte_sobre_el_tope_es_too_large_sin_truncar():
    body = "a" * (MAX_DEFINITION_BYTES - 1)

    result = build_definition(_view_read(body), redact=_identity_redact)

    assert result.body_available is False
    assert result.unavailable_reason == "too_large"
    assert result.size_bytes == MAX_DEFINITION_BYTES + 1
    assert result.body is None
    assert result.body_fingerprint is None


def test_el_tope_se_mide_sobre_el_json_y_no_sobre_los_caracteres():
    # 60.000 caracteres (bajo el tope) que viajan como ~90.000 bytes de JSON: cada salto de línea
    # pesa dos. El cuerpo NO puede ser solo saltos: un cuerpo en blanco es "no disponible".
    body = "a\n" * 30_000

    result = build_definition(_view_read(body), redact=_identity_redact)

    assert result.unavailable_reason == "too_large"
    assert result.size_bytes == json_encoded_size(body)


def test_el_tope_se_mide_sobre_el_cuerpo_ya_redactado():
    # El cuerpo crudo pasa el tope, pero el redactor lo achica: lo que viaja es lo redactado.
    raw_body = "x" * (MAX_DEFINITION_BYTES + 100)

    def shrinking_redact(text: str) -> RedactionResult:
        return RedactionResult(text="SELECT 1", redactions={"jwt": 1})

    result = build_definition(_view_read(raw_body), redact=shrinking_redact)

    assert result.body_available is True
    assert result.body == "SELECT 1"
    assert result.redactions == {"jwt": 1}


def test_too_large_no_filtra_conteos_de_redaccion():
    body = "a" * MAX_DEFINITION_BYTES

    def counting_redact(text: str) -> RedactionResult:
        return RedactionResult(text=text, redactions={"jwt": 3}, flagged={"email": 2})

    result = build_definition(_view_read(body), redact=counting_redact)

    assert result.unavailable_reason == "too_large"
    assert result.redactions == {}
    assert result.flagged == {}


# --------------------------------------------------------------------------- #
# S2.3 — huella                                                                #
# --------------------------------------------------------------------------- #
def test_huella_ignora_definer_y_espacios():
    with_definer = "CREATE DEFINER=`app`@`%` VIEW v AS SELECT 1"
    reformatted = "CREATE   VIEW v\nAS   SELECT 1;"

    first = build_definition(_view_read(with_definer))
    second = build_definition(_view_read(reformatted))

    assert first.body_fingerprint is not None
    assert first.body_fingerprint == second.body_fingerprint


def test_huella_distingue_cuerpos_distintos():
    first = build_definition(_view_read("CREATE VIEW v AS SELECT 1"))
    second = build_definition(_view_read("CREATE VIEW v AS SELECT 2"))

    assert first.body_fingerprint != second.body_fingerprint


def test_huella_es_sha256_de_la_normalizacion_de_schema_diff():
    redacted_text = "CREATE VIEW v AS\n  SELECT 1;"

    expected = hashlib.sha256(normalize_body(redacted_text).encode("utf-8")).hexdigest()

    assert body_fingerprint(redacted_text) == expected
    assert len(expected) == 64


def test_la_huella_se_calcula_sobre_el_texto_redactado():
    # Dos cuerpos que difieren SOLO en la credencial enmascarada tienen la misma huella.
    first = build_definition(_view_read("CREATE USER u IDENTIFIED BY 'first-secret'"))
    second = build_definition(_view_read("CREATE USER u IDENTIFIED BY 'second-secret'"))

    assert first.body == second.body == "CREATE USER u IDENTIFIED BY '***'"
    assert first.body_fingerprint == second.body_fingerprint


# --------------------------------------------------------------------------- #
# S2.4 — la cuenta del DEFINER no sale                                         #
# --------------------------------------------------------------------------- #
def _every_field_as_json(result: DefinitionResult) -> str:
    return json.dumps(dataclasses.asdict(result), default=str)


def test_la_cuenta_del_definer_no_aparece_en_ningun_campo_y_security_es_definer():
    body = (
        "CREATE ALGORITHM=UNDEFINED DEFINER=`admin_root`@`10.9.8.7` "
        "SQL SECURITY DEFINER VIEW `v` AS SELECT 1"
    )

    result = build_definition(_view_read(body))

    serialized = _every_field_as_json(result)
    assert result.security == "definer"
    assert "admin_root" not in serialized
    assert "10.9.8.7" not in serialized
    assert "DEFINER=" not in serialized


def test_sql_security_invoker_se_infiere_del_cuerpo():
    body = "CREATE DEFINER=`u`@`h` SQL SECURITY INVOKER VIEW `v` AS SELECT 1"

    result = build_definition(_view_read(body))

    assert result.security == "invoker"
    assert "`u`@`h`" not in _every_field_as_json(result)


def test_el_security_que_da_el_adapter_gana_sobre_la_inferencia():
    body = "CREATE VIEW `v` AS SELECT 1"

    result = build_definition(_view_read(body, security="invoker"))

    assert result.security == "invoker"


def test_un_trigger_no_tiene_modo_de_security():
    body = "CREATE DEFINER=`u`@`h` TRIGGER t BEFORE INSERT ON x FOR EACH ROW SET NEW.a = 1"
    read = DefinitionRead(kind="trigger", name="t", body=body, trigger_table="x")

    result = build_definition(read)

    assert result.security is None
    assert result.trigger_table == "x"
    assert "`u`@`h`" not in _every_field_as_json(result)


# --------------------------------------------------------------------------- #
# Disponibilidad explícita                                                     #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("missing_body", [None, "", "   \n\t"])
def test_cuerpo_ausente_en_blanco_o_nulo_no_es_un_exito_vacio(missing_body):
    result = build_definition(_view_read(missing_body))

    assert result.body_available is False
    assert result.unavailable_reason == "insufficient_privilege"
    assert result.body is None
    assert result.body_fingerprint is None
    assert result.size_bytes is None


def test_razon_por_version_gana_sobre_el_privilegio_generico():
    read = DefinitionRead(kind="routine", name="p", routine_kind="PROCEDURE")

    result = build_definition(read, missing_body_reason="flag_off")

    assert result.unavailable_reason == "flag_off"


def test_engine_unsupported_del_adapter_no_se_pisa():
    read = DefinitionRead(kind="event", name="e", unavailable_reason="engine_unsupported")

    result = build_definition(read, missing_body_reason="flag_off")

    assert result.unavailable_reason == "engine_unsupported"


def test_el_motivo_por_version_se_ignora_si_el_cuerpo_esta():
    result = build_definition(_view_read("CREATE VIEW v AS SELECT 1"), missing_body_reason="flag_off")

    assert result.body_available is True
    assert result.unavailable_reason is None


def test_lectura_con_cuerpo_y_razon_a_la_vez_se_trata_como_no_disponible():
    read = _view_read("CREATE VIEW v AS SELECT 1", unavailable_reason="insufficient_privilege")

    result = build_definition(read)

    assert result.body_available is False
    assert result.body is None


def test_los_metadatos_viajan_aunque_no_haya_cuerpo():
    read = DefinitionRead(
        kind="trigger",
        name="t",
        trigger_table="ventas",
        trigger_timing="BEFORE",
        trigger_events=["INSERT"],
    )

    result = build_definition(read)

    assert result.body_available is False
    assert (result.trigger_table, result.trigger_timing, result.trigger_events) == (
        "ventas",
        "BEFORE",
        ["INSERT"],
    )


def test_redacciones_y_flagged_se_propagan_cuando_hay_cuerpo():
    body = "CREATE USER u IDENTIFIED BY 'pw'; -- avisar a ops@example.org"

    result = build_definition(_view_read(body))

    assert result.redactions == {"identified_by": 1}
    assert result.flagged == {"email": 1}


# --------------------------------------------------------------------------- #
# S2.2 / S5.3 — tabla de razones por motor y versión                           #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("engine", "version", "proc_flag", "expected_reason"),
    [
        # MariaDB < 11.3: solo se lee con mysql.proc, que habilita la bandera.
        ("mariadb", "11.2.4-MariaDB", False, "flag_off"),
        ("mariadb", "11.2.4-MariaDB", True, None),
        ("mariadb", "10.11.6-MariaDB", False, "flag_off"),
        # MariaDB >= 11.3: SHOW CREATE ROUTINE por base; la bandera ya no explica nada.
        ("mariadb", "11.3.0-MariaDB", False, None),
        ("mariadb", "11.3.2-MariaDB-1:11.3.2+maria~ubu2204", False, None),
        # Un servidor registrado como ``mysql`` cuya versión dice MariaDB se trata como MariaDB.
        ("mysql", "5.5.5-10.11.6-MariaDB", False, "flag_off"),
        ("mysql", "11.4.2-MariaDB", False, None),
        # MySQL 5.7: mysql.proc existe.
        ("mysql", "5.7.44", False, "flag_off"),
        ("mysql", "5.7.44", True, None),
        # MySQL 8.0.0 a 8.0.19: ni mysql.proc ni SHOW_ROUTINE; ninguna bandera lo arregla.
        ("mysql", "8.0.19", False, "engine_unsupported"),
        ("mysql", "8.0.19", True, "engine_unsupported"),
        ("mysql", "8.0.0", False, "engine_unsupported"),
        # MySQL 8.0.20+: el motivo es privilegio o DEFINER, no la versión.
        ("mysql", "8.0.20", False, None),
        ("mysql", "8.4.0", False, None),
        # Conservador: sin versión legible o en PostgreSQL, la versión no explica nada.
        ("mysql", None, False, None),
        ("mysql", "no-es-una-version", False, None),
        ("postgresql", "16.2", False, None),
    ],
)
def test_routine_body_reason(engine, version, proc_flag, expected_reason):
    assert routine_body_reason(engine, version, proc_flag) == expected_reason


@pytest.mark.parametrize(
    ("version", "dialect", "expected_supported"),
    [
        ("11.2.4-MariaDB", "mysql", True),
        ("11.2.4-MariaDB", "mariadb", True),
        ("11.3.0-MariaDB", "mysql", False),
        ("5.7.44", "mysql", True),
        ("8.0.35", "mysql", False),
        ("16.2", "postgresql", False),
        (None, "mysql", False),
        ("no-es-una-version", "mysql", False),
    ],
)
def test_proc_grant_supported(version, dialect, expected_supported):
    assert proc_grant_supported(version, dialect=dialect) is expected_supported


@pytest.mark.parametrize(
    ("version", "expects_grant"),
    [
        ("11.3.0-MariaDB", True),
        ("5.5.5-11.4.2-MariaDB", True),
        ("11.2.9-MariaDB", False),
        ("10.11.6-MariaDB", False),
        ("8.0.35", False),
        (None, False),
    ],
)
def test_mariadb_routine_grants_for_version(version, expects_grant):
    grants, note = mariadb_routine_grants_for_version(version)

    if expects_grant:
        assert grants == ("SHOW CREATE ROUTINE",)
        assert note is None
    else:
        assert grants == ()
        assert isinstance(note, str) and note


# --------------------------------------------------------------------------- #
# S2.1 — modelos de salida                                                     #
# --------------------------------------------------------------------------- #
def _available_out(**overrides) -> dict:
    fields = {
        "kind": "view",
        "name": "v_ventas",
        "routine_kind": None,
        "identity_arguments": None,
        "body_available": True,
        "unavailable_reason": None,
        "body": "CREATE VIEW v AS SELECT 1",
        "size_bytes": 27,
        "body_fingerprint": "f" * 64,
        "security": "definer",
        "check_option": None,
        "trigger": None,
        "event": None,
        "redactions": [],
        "flagged": [],
    }
    fields.update(overrides)
    return fields


def _unavailable_out(reason: str = "insufficient_privilege", **overrides) -> dict:
    fields = _available_out(
        body_available=False,
        unavailable_reason=reason,
        body=None,
        size_bytes=None,
        body_fingerprint=None,
    )
    fields.update(overrides)
    return fields


def test_definition_out_disponible_valido():
    assert DefinitionOut(**_available_out()).body_available is True


@pytest.mark.parametrize(
    "reason",
    ["insufficient_privilege", "engine_unsupported", "scope_disabled", "flag_off"],
)
def test_definition_out_no_disponible_valido(reason):
    assert DefinitionOut(**_unavailable_out(reason)).unavailable_reason == reason


def test_definition_out_too_large_exige_size_bytes():
    valid = DefinitionOut(**_unavailable_out("too_large", size_bytes=70_000))
    assert valid.size_bytes == 70_000

    with pytest.raises(ValidationError):
        DefinitionOut(**_unavailable_out("too_large"))


@pytest.mark.parametrize(
    "invalid_fields",
    [
        # Disponible con razón.
        _available_out(unavailable_reason="flag_off"),
        # Disponible sin cuerpo o con cuerpo vacío.
        _available_out(body=None),
        _available_out(body=""),
        # No disponible sin razón.
        _available_out(body_available=False, body=None, body_fingerprint=None),
        # No disponible pero con cuerpo o con huella.
        _available_out(body_available=False, unavailable_reason="insufficient_privilege"),
        _unavailable_out(body_fingerprint="a" * 64),
    ],
    ids=[
        "available_with_reason",
        "available_without_body",
        "available_with_empty_body",
        "unavailable_without_reason",
        "unavailable_with_body",
        "unavailable_with_fingerprint",
    ],
)
def test_definition_out_rechaza_combinaciones_inconsistentes(invalid_fields):
    with pytest.raises(ValidationError):
        DefinitionOut(**invalid_fields)


def test_not_found_no_es_una_razon_valida():
    assert "not_found" not in get_args(DefinitionUnavailableReason)
    with pytest.raises(ValidationError):
        DefinitionOut(**_unavailable_out("not_found"))


def test_definition_out_rechaza_campos_extra():
    with pytest.raises(ValidationError):
        DefinitionOut(**_available_out(definer="root@%"))


def test_vocabularios_cerrados_incluyen_lo_nuevo():
    assert set(get_args(DefinitionUnavailableReason)) == {
        "insufficient_privilege",
        "engine_unsupported",
        "scope_disabled",
        "flag_off",
        "too_large",
    }
    assert {"flag_off", "too_large"} <= set(get_args(UnavailableReason))
    assert "event" in get_args(ObjectKind)


def test_campos_de_definition_out_congelados():
    assert set(DefinitionOut.model_fields) == {
        "kind",
        "name",
        "routine_kind",
        "identity_arguments",
        "body_available",
        "unavailable_reason",
        "body",
        "size_bytes",
        "body_fingerprint",
        "security",
        "check_option",
        "trigger",
        "event",
        "redactions",
        "flagged",
    }
    assert set(DefinitionsOut.model_fields) == {"objects", "missing"}
    assert set(DefinitionRefOut.model_fields) == {"kind", "name", "routine_kind"}
    assert set(TriggerMetaOut.model_fields) == {"table", "timing", "events"}
    assert set(EventMetaOut.model_fields) == {"schedule", "status"}
    assert set(RedactionCountOut.model_fields) == {"category", "count"}


def test_ningun_campo_definition_contiene_subcadenas_prohibidas():
    forbidden_substrings = ("host", "port", "password", "encrypted", "confirm_token", "definer")
    models = (
        DefinitionOut,
        DefinitionsOut,
        DefinitionRefOut,
        TriggerMetaOut,
        EventMetaOut,
        RedactionCountOut,
    )

    offending = [
        f"{model.__name__}.{field_name}"
        for model in models
        for field_name in model.model_fields
        if any(substring in field_name for substring in forbidden_substrings)
    ]

    assert offending == []


# --------------------------------------------------------------------------- #
# El resultado puro mapea sin fricción al modelo de salida                     #
# --------------------------------------------------------------------------- #
def _map_to_out(result: DefinitionResult) -> DefinitionOut:
    """Mapeo campo por campo, como lo hará la tool (nunca ``model_validate`` del resultado)."""
    trigger = None
    if result.trigger_table is not None:
        trigger = TriggerMetaOut(
            table=result.trigger_table,
            timing=result.trigger_timing,
            events=result.trigger_events,
        )
    event = None
    if result.event_schedule is not None or result.event_status is not None:
        event = EventMetaOut(schedule=result.event_schedule, status=result.event_status)
    return DefinitionOut(
        kind=result.kind,
        name=result.name,
        routine_kind=result.routine_kind,
        identity_arguments=result.identity_arguments,
        body_available=result.body_available,
        unavailable_reason=result.unavailable_reason,
        body=result.body,
        size_bytes=result.size_bytes,
        body_fingerprint=result.body_fingerprint,
        security=result.security,
        check_option=result.check_option,
        trigger=trigger,
        event=event,
        redactions=[
            RedactionCountOut(category=category, count=count)
            for category, count in sorted(result.redactions.items())
        ],
        flagged=[
            RedactionCountOut(category=category, count=count)
            for category, count in sorted(result.flagged.items())
        ],
    )


@pytest.mark.parametrize(
    "read",
    [
        _view_read("CREATE VIEW v AS SELECT 1"),
        _view_read(None),
        _view_read("a" * MAX_DEFINITION_BYTES),
        DefinitionRead(kind="event", name="e", unavailable_reason="engine_unsupported"),
        DefinitionRead(
            kind="trigger",
            name="t",
            body="CREATE TRIGGER t BEFORE INSERT ON x FOR EACH ROW SET NEW.a = 1",
            trigger_table="x",
            trigger_timing="BEFORE",
            trigger_events=["INSERT"],
        ),
        DefinitionRead(
            kind="routine",
            name="f",
            routine_kind="FUNCTION",
            identity_arguments="integer, text",
            body="CREATE FUNCTION f() RETURNS int RETURN 1",
            security="invoker",
        ),
    ],
    ids=["view_ok", "view_no_body", "view_too_large", "event_pg", "trigger_ok", "pg_overload"],
)
def test_todo_resultado_de_build_definition_cumple_el_modelo_de_salida(read):
    result = build_definition(read)

    out = _map_to_out(result)

    assert out.body_available is result.body_available
