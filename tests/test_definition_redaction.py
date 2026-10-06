"""
Tests PUROS (sin motor) de ``definition_redaction.redact_definition`` (slice S2 de
``mcp-schema-definitions``).

Cubre:
- S2.5: cada patrón de credencial se enmascara, el resto del cuerpo queda intacto y ``redactions``
  cuenta la categoría.
- S2.6: emails y hosts internos se CUENTAN en ``flagged`` pero no se tocan ni suben ``redactions``.
- Un cuerpo sin credenciales sale idéntico byte a byte.
- Tiempo lineal ante entradas adversarias de tamaño grande (el cuerpo lo controla un tercero).
- S2.7: un secreto con forma no reconocida PUEDE sobrevivir (límite documentado, no un bug).

Lo que NO se verifica acá: que la redacción sea una frontera de seguridad. No lo es: la frontera
es el scope ``data.definitions``.

Correr: ``.venv/bin/python scripts/run_tests_direct.py tests.test_definition_redaction``
"""

import time

import pytest

from app.services.db_admin import definition_redaction as redaction_module
from app.services.db_admin import mysql_adapter as mysql_adapter_module
from app.services.db_admin.definition_redaction import RedactionResult, redact_definition

# Tiempo máximo generoso para una entrada de ~200 KiB. Un patrón cuadrático tardaría órdenes de
# magnitud más; el margen evita falsos rojos en una máquina lenta (WSL2 sobre /mnt/).
_LINEAR_TIME_BUDGET_SECONDS = 5.0
_ADVERSARIAL_INPUT_CHARS = 200_000


# --------------------------------------------------------------------------- #
# S2.5 — cada patrón se enmascara                                              #
# --------------------------------------------------------------------------- #
_JWT_SAMPLE = (
    "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dBjftJeZ4CVPmB92K27uhbUJU1p1r"
)
_PEM_SAMPLE = (
    "-----BEGIN RSA PRIVATE KEY-----\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC\n-----END RSA PRIVATE KEY-----"
)

# (nombre del caso, cuerpo, secreto que NO debe sobrevivir, categoría esperada, prefijo intacto)
_MASKED_CASES = [
    (
        "identified_by",
        "CREATE USER 'u'@'%' IDENTIFIED BY 'sup3rS3cret';",
        "sup3rS3cret",
        "identified_by",
        "CREATE USER 'u'@'%' IDENTIFIED BY '***';",
    ),
    (
        "identified_with_plugin",
        "CREATE USER 'u' IDENTIFIED WITH mysql_native_password BY 'x1y2z3';",
        "x1y2z3",
        "identified_by",
        "CREATE USER 'u' IDENTIFIED WITH mysql_native_password BY '***';",
    ),
    (
        "uri_userinfo",
        "ENGINE=FEDERATED CONNECTION='mysql://app:Zk39pw@remote.example.org:3306/d/t'",
        "Zk39pw",
        "uri_password",
        "ENGINE=FEDERATED CONNECTION='mysql://app:***@remote.example.org:3306/d/t'",
    ),
    (
        "kv_password_unquoted",
        "OPTION_LIST='host=h,user=u,password=Sup3rS3c,port=3306'",
        "Sup3rS3c",
        "kv_password",
        "OPTION_LIST='host=h,user=u,password=***,port=3306'",
    ),
    (
        "kv_password_quoted",
        "SELECT * FROM t WHERE pwd = 'abc123xyz'",
        "abc123xyz",
        "kv_password",
        "SELECT * FROM t WHERE pwd = '***'",
    ),
    (
        "secret_named_set",
        "SET @api_token = 'abcd1234efgh';",
        "abcd1234efgh",
        "secret_assignment",
        "SET @api_token = '***';",
    ),
    (
        "secret_named_declare",
        "DECLARE v_secret VARCHAR(40) DEFAULT 'hush-hush-value';",
        "hush-hush-value",
        "secret_assignment",
        "DECLARE v_secret VARCHAR(40) DEFAULT '***';",
    ),
    (
        "jwt",
        f"SELECT '{_JWT_SAMPLE}' AS t",
        _JWT_SAMPLE,
        "jwt",
        "SELECT '***' AS t",
    ),
    (
        "aws_access_key",
        "-- key AKIAIOSFODNN7EXAMPLE rotated",
        "AKIAIOSFODNN7EXAMPLE",
        "aws_access_key",
        "-- key *** rotated",
    ),
    (
        "github_token",
        "SELECT 'ghp_" + "A1b2C3d4" * 5 + "'",
        "ghp_" + "A1b2C3d4" * 5,
        "github_token",
        "SELECT '***'",
    ),
    (
        "slack_token",
        "SELECT 'xoxb-1234567890-abcdefghij'",
        "xoxb-1234567890-abcdefghij",
        "slack_token",
        "SELECT '***'",
    ),
    (
        "sk_api_key",
        "SELECT 'sk-" + "abcDEF123456" * 2 + "'",
        "sk-" + "abcDEF123456" * 2,
        "api_key",
        "SELECT '***'",
    ),
    (
        "pem_block",
        f"SELECT 1; /* {_PEM_SAMPLE} */ SELECT 2;",
        "MIIEvQIBADANBgkqhkiG9w0BAQEFAASC",
        "pem_block",
        "SELECT 1; /* *** */ SELECT 2;",
    ),
    (
        "long_hex_literal",
        "SELECT '" + "a1" * 25 + "' AS h",
        "a1" * 25,
        "long_token_literal",
        "SELECT '***' AS h",
    ),
]


@pytest.mark.parametrize(
    ("body", "secret", "category", "expected_text"),
    [case[1:] for case in _MASKED_CASES],
    ids=[case[0] for case in _MASKED_CASES],
)
def test_cada_patron_se_enmascara_y_el_resto_queda_intacto(body, secret, category, expected_text):
    result = redact_definition(body)

    assert secret not in result.text
    assert result.text == expected_text
    assert result.redactions == {category: 1}


def test_dos_credenciales_de_la_misma_categoria_se_cuentan_dos_veces():
    body = "CREATE USER a IDENTIFIED BY 'one'; CREATE USER b IDENTIFIED BY 'two';"

    result = redact_definition(body)

    assert "'one'" not in result.text
    assert "'two'" not in result.text
    assert result.redactions == {"identified_by": 2}


def test_resultado_es_un_redaction_result_inmutable():
    result = redact_definition("SELECT 1")

    assert isinstance(result, RedactionResult)
    with pytest.raises(AttributeError):
        result.text = "otro"  # type: ignore[misc]


def test_comparar_columna_con_pwd_sin_comillas_no_se_enmascara():
    # ``pwd = otra_columna`` es una comparación: enmascararla destruiría el sentido del código.
    body = "SELECT 1 FROM t WHERE pwd = other_column"

    result = redact_definition(body)

    assert result.text == body
    assert result.redactions == {}


def test_los_comentarios_se_conservan():
    body = "-- reporte semanal\nSELECT 1 /* sin secretos */"

    assert redact_definition(body).text == body


# --------------------------------------------------------------------------- #
# S2.6 — emails y hosts: contados, no enmascarados                             #
# --------------------------------------------------------------------------- #
def test_emails_y_hosts_se_cuentan_sin_enmascarar():
    body = "-- avisar a a@b.com\nSELECT * FROM remoto WHERE origen = 'db1.internal'"

    result = redact_definition(body)

    assert result.text == body
    assert result.redactions == {}
    assert result.flagged == {"email": 1, "host": 1}


def test_ip_literal_cuenta_como_host():
    body = "SELECT 'conectar a 10.0.0.12'"

    result = redact_definition(body)

    assert result.text == body
    assert result.flagged == {"host": 1}


def test_uri_enmascarado_no_se_cuenta_como_email():
    body = "CONNECTION='mysql://app:Zk39pw@remote.example.org:3306/d/t'"

    result = redact_definition(body)

    assert result.redactions == {"uri_password": 1}
    assert "email" not in result.flagged


# --------------------------------------------------------------------------- #
# Cuerpo sin credenciales: idéntico                                            #
# --------------------------------------------------------------------------- #
def test_cuerpo_sin_credenciales_sale_identico_byte_a_byte():
    body = (
        "CREATE VIEW `v_ventas` AS\n"
        "\tSELECT `id`,  `total`  -- importe en centavos\n"
        "  FROM `ventas`\r\n"
        "WHERE `estado` = 'pagada' AND `nota` <> \"it''s ok\";\n"
        "SET @contador = 42;  /* sin literales sensibles */\n"
    )

    result = redact_definition(body)

    assert result.text == body
    assert result.redactions == {}
    assert result.flagged == {}


def test_cuerpo_vacio_sale_vacio():
    result = redact_definition("")

    assert result == RedactionResult(text="", redactions={}, flagged={})


# --------------------------------------------------------------------------- #
# S2.7 — límite documentado: un secreto de forma no reconocida sobrevive       #
# --------------------------------------------------------------------------- #
def test_un_secreto_con_forma_no_reconocida_puede_sobrevivir():
    # Ningún patrón distingue este literal de cualquier otro: es el límite de la redacción
    # best effort y por eso la frontera es el scope ``data.definitions``, no esta función.
    body = "SET @nota = 'tangerine-orbit-77';"

    result = redact_definition(body)

    assert "tangerine-orbit-77" in result.text
    assert result.redactions == {}


# --------------------------------------------------------------------------- #
# Tiempo lineal ante entradas adversarias                                      #
# --------------------------------------------------------------------------- #
_ADVERSARIAL_INPUTS = {
    "password_sin_cierre": "PASSWORD '" + "a" * _ADVERSARIAL_INPUT_CHARS,
    "password_repetido": "PASSWORD 'a " * (_ADVERSARIAL_INPUT_CHARS // 12),
    "jwt_repetido": "eyJ-" * (_ADVERSARIAL_INPUT_CHARS // 4),
    "pem_begin_huerfanos": "-----BEGIN X-----" * (_ADVERSARIAL_INPUT_CHARS // 17),
    "set_con_nombre_secreto": "SET a_pass " * (_ADVERSARIAL_INPUT_CHARS // 11),
    "declare_con_nombre_secreto": "DECLARE a_pass " * (_ADVERSARIAL_INPUT_CHARS // 15),
    "comilla_y_token_largo": "'" + "a" * _ADVERSARIAL_INPUT_CHARS,
    "uri_repetido": "://a:" * (_ADVERSARIAL_INPUT_CHARS // 5),
    "arrobas": "a@" * (_ADVERSARIAL_INPUT_CHARS // 2),
    "puntos": "a." * (_ADVERSARIAL_INPUT_CHARS // 2),
    "corrida_sin_separadores": "a" * _ADVERSARIAL_INPUT_CHARS,
    "kv_password_repetido": "password=" * (_ADVERSARIAL_INPUT_CHARS // 9),
}


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL_INPUTS))
def test_entrada_adversaria_grande_se_procesa_en_tiempo_acotado(name):
    body = _ADVERSARIAL_INPUTS[name]

    started_at = time.perf_counter()
    result = redact_definition(body)
    elapsed_seconds = time.perf_counter() - started_at

    assert isinstance(result.text, str)
    assert elapsed_seconds < _LINEAR_TIME_BUDGET_SECONDS


# --------------------------------------------------------------------------- #
# Una sola fuente con MySQLAdapter._redact_embedded_credentials                #
# --------------------------------------------------------------------------- #
def test_mysql_adapter_reusa_las_regex_movidas():
    assert mysql_adapter_module._URI_USERINFO_PASSWORD_RE is redaction_module.URI_USERINFO_PASSWORD_RE
    assert mysql_adapter_module._KV_PASSWORD_RE is redaction_module.KV_PASSWORD_RE
    assert mysql_adapter_module._REDACTED == redaction_module.REDACTED
