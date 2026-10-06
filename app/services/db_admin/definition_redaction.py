"""
Redacción de credenciales en el CÓDIGO de vistas, triggers, events y rutinas (``get_definition``).

ES BEST EFFORT Y NO ES UNA FRONTERA DE SEGURIDAD
------------------------------------------------
El código de un objeto es texto libre de terceros: un secreto puede venir con una forma que ningún
patrón de acá conoce (``SET @x = 'hunter2'`` no se distingue de cualquier otro literal) y SOBREVIVE.
La frontera real es el scope ``data.definitions``: quien lo tiene ya puede leer el código. Esta
redacción solo baja la probabilidad de que una credencial EN CLARO termine en el contexto de un
modelo, y la respuesta lo declara contando lo enmascarado en ``redactions``.

QUÉ SE ENMASCARA Y QUÉ SOLO SE CUENTA
-------------------------------------
Se enmascaran SOLO credenciales (``***``). Emails y hosts internos se CUENTAN en ``flagged`` y no
se tocan: enmascarar todos los literales (lo que hace ``sql_masking``) destruye el sentido del
código, que es justo lo que el lector viene a revisar. Los comentarios se conservan.

POR QUÉ LOS PATRONES SON ACOTADOS
---------------------------------
El texto de entrada lo controla un tercero y llega hasta 64 KiB por objeto. Un patrón con
cuantificadores sin tope y puntos de partida solapados es cuadrático sobre una entrada adversaria
(``"PASSWORD '" * 6000``). Por eso: los literales y los tokens llevan tope de largo, los patrones
de token arrancan solo al comienzo de una corrida (lookbehind) y los bloques PEM se barren con
``str.find`` en vez de un ``.*?`` que re-escanea hasta el final por cada ``BEGIN`` huérfano.

Las dos regex de URI/``password=`` que ya usaba ``MySQLAdapter._redact_embedded_credentials`` viven
acá (una sola fuente) y ese método conserva su comportamiento. NO se reutilizan sobre el cuerpo
completo: están pensadas para el literal de una opción de tabla y sobre un cuerpo entero se
comerían texto hasta el próximo ``@``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

REDACTED = "***"

# --------------------------------------------------------------------------- #
# Regex compartidas con MySQLAdapter._redact_embedded_credentials (sin cambios) #
# --------------------------------------------------------------------------- #
# ``scheme://usuario:CONTRASEÑA@host…`` (FEDERATED, y el URI de CONNECT).
# El ``@`` es OBLIGATORIO y la contraseña tiene que tener AL MENOS un carácter: sin eso no
# hay nada que redactar, que es justo el caso ``mysql://user@host/db/tbl`` (y el de
# ``CONNECTION='fedlink'``, un nombre de ``mysql.servers`` sin URI).
URI_USERINFO_PASSWORD_RE = re.compile(r"(://[^:/?#@'\s]+):(?:\\.|''|[^@'\\])+(@)")

# ``password=…`` / ``pwd=…`` dentro de un OPTION_LIST de CONNECT o de una cadena ODBC.
# El valor termina en ``,`` (separador de OPTION_LIST), ``;`` (ODBC) o el cierre del
# literal. ``\'``/``''`` se consumen como escape para no cortar el valor por la mitad, y
# el ``+`` evita inventar un ``***`` donde el valor venía vacío.
KV_PASSWORD_RE = re.compile(
    r"\b(password|passwd|pwd)(\s*=\s*)(?:\\.|''|[^,;'\\])+",
    re.IGNORECASE,
)

# --------------------------------------------------------------------------- #
# Patrones propios del cuerpo completo                                         #
# --------------------------------------------------------------------------- #
#: Largo máximo (en alternancias) de un literal entre comillas que se analiza. Un secreto real
#: es corto; el tope es lo que mantiene lineal el barrido ante una comilla sin cierre.
_MAX_LITERAL_CHARS = 2048
_SQ_LITERAL = rf"'(?:[^'\\]|\\.|''){{0,{_MAX_LITERAL_CHARS}}}'"
_DQ_LITERAL = rf'"(?:[^"\\]|\\.|""){{0,{_MAX_LITERAL_CHARS}}}"'
_ANY_LITERAL = rf"(?:{_SQ_LITERAL}|{_DQ_LITERAL})"

# ``IDENTIFIED [WITH plugin] BY|AS 'x'`` y ``[ENCRYPTED] PASSWORD 'x'`` (mismo criterio que
# ``query_policy.redact_secrets``, pero acotado y con conteo).
_IDENTIFIED_BY_RE = re.compile(
    r"(\bIDENTIFIED\s+(?:WITH\s+\S{1,64}\s+)?(?:BY|AS)\s+|\b(?:ENCRYPTED\s+)?PASSWORD\s+)"
    rf"({_ANY_LITERAL})",
    re.IGNORECASE,
)

# ``scheme://usuario:CONTRASEÑA@host`` en cualquier parte del cuerpo. La contraseña no admite
# espacios, comillas ni ``/`` y tiene tope: así no se come texto de por medio hasta un ``@`` lejano.
_BODY_URI_PASSWORD_RE = re.compile(
    r"(\b[A-Za-z][A-Za-z0-9+.\-]{0,31}://[^:/?#@'\"\s]{1,128}):([^@'\"\s/]{1,256})(@)"
)

# ``password=valor`` / ``pwd = 'valor'``. El valor SIN comillas solo se acepta pegado al ``=``:
# con espacios (``WHERE pwd = otra_columna``) es una comparación y enmascararla destruiría sentido.
_BODY_KV_PASSWORD_RE = re.compile(
    r"\b(password|passwd|pwd)(?:(\s*=\s*)(" + _ANY_LITERAL + r")|(=)([^\s,;'\"()]{1,256}))",
    re.IGNORECASE,
)

# ``SET @pass = 'x'`` / ``SET api_key := "x"``: asignación de un literal a una variable cuyo nombre
# delata un secreto. El tope ``{0,64}`` evita el cuadrático de dos ``*`` alrededor de la palabra.
_SECRET_NAME = r"[\w$]{0,64}(?:pass|secret|token|api_?key)[\w$]{0,64}"
_SECRET_SET_RE = re.compile(
    rf"(\bSET\s+@{{0,2}}{_SECRET_NAME}\s*(?::=|=)\s*)({_ANY_LITERAL})",
    re.IGNORECASE,
)
# ``DECLARE v_token VARCHAR(40) DEFAULT 'x'``.
_SECRET_DECLARE_RE = re.compile(
    rf"(\bDECLARE\s+{_SECRET_NAME}\s+[A-Za-z0-9_(), ]{{1,40}}?\s+DEFAULT\s+)({_ANY_LITERAL})",
    re.IGNORECASE,
)

# El lookbehind hace que el barrido arranque solo al comienzo de una corrida de caracteres de
# token: sin él, ``eyJ`` repetido dentro de una corrida larga sin ``.`` es cuadrático.
_JWT_RE = re.compile(
    r"(?<![A-Za-z0-9_\-])eyJ[A-Za-z0-9_\-]{2,2048}\.[A-Za-z0-9_\-]{5,2048}\.[A-Za-z0-9_\-]{5,2048}"
)
_AWS_ACCESS_KEY_RE = re.compile(r"(?<![A-Za-z0-9])AKIA[0-9A-Z]{16}(?![A-Za-z0-9])")
_GITHUB_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])gh[po]_[A-Za-z0-9]{20,255}")
_SLACK_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9_])xox[abprs]-[A-Za-z0-9\-]{10,255}")
_API_KEY_SK_RE = re.compile(r"(?<![A-Za-z0-9_])sk-[A-Za-z0-9_\-]{20,255}")

# Literal entre comillas que es SOLO hex o base64 de 40+ caracteres: la forma de un secreto
# generado. Sin ``_``/``-`` a propósito: un identificador largo en snake_case no es un secreto.
_LONG_TOKEN_LITERAL_RE = re.compile(r"(')([A-Za-z0-9+/]{40,4096}={0,2})(')")

_PEM_BEGIN_RE = re.compile(r"-----BEGIN [A-Z0-9 ]{1,40}-----")
_PEM_END_RE = re.compile(r"-----END [A-Z0-9 ]{1,40}-----")

# --------------------------------------------------------------------------- #
# Lo que solo se CUENTA                                                         #
# --------------------------------------------------------------------------- #
_EMAIL_RE = re.compile(
    r"(?<![A-Za-z0-9._%+\-])[A-Za-z0-9._%+\-]{1,64}@[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){1,8}"
)
#: Host "interno": IPv4 literal o nombre que termina en un sufijo de red privada. Es una heurística:
#: solo informa, nunca enmascara.
_HOST_RE = re.compile(
    r"(?<![A-Za-z0-9.\-])(?:"
    r"(?:\d{1,3}\.){3}\d{1,3}"
    r"|[A-Za-z0-9\-]{1,63}(?:\.[A-Za-z0-9\-]{1,63}){0,8}\.(?:internal|local|lan|corp|intranet|private|home|localdomain)"
    r")(?![A-Za-z0-9\-])",
    re.IGNORECASE,
)

CATEGORY_IDENTIFIED_BY = "identified_by"
CATEGORY_URI_PASSWORD = "uri_password"
CATEGORY_KV_PASSWORD = "kv_password"
CATEGORY_SECRET_ASSIGNMENT = "secret_assignment"
CATEGORY_JWT = "jwt"
CATEGORY_AWS_ACCESS_KEY = "aws_access_key"
CATEGORY_GITHUB_TOKEN = "github_token"
CATEGORY_SLACK_TOKEN = "slack_token"
CATEGORY_API_KEY = "api_key"
CATEGORY_PEM_BLOCK = "pem_block"
CATEGORY_LONG_TOKEN = "long_token_literal"
CATEGORY_EMAIL = "email"
CATEGORY_HOST = "host"


@dataclass(frozen=True)
class RedactionResult:
    """
    ``text``: el cuerpo con las credenciales reconocidas enmascaradas.
    ``redactions``: categoría -> cuántas se enmascararon (solo categorías con al menos una).
    ``flagged``: categoría -> cuántas apariciones se contaron SIN enmascarar (email, host).
    """

    text: str
    redactions: dict[str, int] = field(default_factory=dict)
    flagged: dict[str, int] = field(default_factory=dict)


def _redact_pem_blocks(body: str) -> tuple[str, int]:
    """
    Enmascara cada bloque ``-----BEGIN X----- … -----END X-----``.

    Se barre con búsquedas hacia adelante y no con un ``.*?`` DOTALL: ante N ``BEGIN`` sin ``END``
    la regex re-escanearía hasta el final por cada uno (cuadrático). Acá, si no hay ningún ``END``
    más adelante, ningún ``BEGIN`` posterior puede cerrar y el barrido termina.
    """
    pieces: list[str] = []
    cursor = 0
    count = 0
    while True:
        begin = _PEM_BEGIN_RE.search(body, cursor)
        if begin is None:
            break
        end = _PEM_END_RE.search(body, begin.end())
        if end is None:
            break
        pieces.append(body[cursor : begin.start()])
        pieces.append(REDACTED)
        cursor = end.end()
        count += 1
    pieces.append(body[cursor:])
    return "".join(pieces), count


def _mask_literal_after_prefix(match: re.Match[str]) -> str:
    return f"{match.group(1)}'{REDACTED}'"


def _mask_kv_password(match: re.Match[str]) -> str:
    key = match.group(1)
    if match.group(3) is not None:
        return f"{key}{match.group(2)}'{REDACTED}'"
    return f"{key}{match.group(4)}{REDACTED}"


def redact_definition(body: str) -> RedactionResult:
    """
    Enmascara credenciales del código de un objeto y cuenta emails/hosts. PURA.

    Sin credenciales reconocidas, ``text`` es IDÉNTICO al de entrada (byte a byte): ningún paso
    normaliza espacios ni cambia comillas.
    """
    text = body
    redactions: dict[str, int] = {}

    def record(category: str, hits: int) -> None:
        if hits:
            redactions[category] = redactions.get(category, 0) + hits

    # Los PEM primero: su contenido base64 podría matchear después como token largo.
    text, pem_hits = _redact_pem_blocks(text)
    record(CATEGORY_PEM_BLOCK, pem_hits)

    text, hits = _IDENTIFIED_BY_RE.subn(_mask_literal_after_prefix, text)
    record(CATEGORY_IDENTIFIED_BY, hits)

    text, hits = _BODY_URI_PASSWORD_RE.subn(rf"\1:{REDACTED}\3", text)
    record(CATEGORY_URI_PASSWORD, hits)

    text, hits = _BODY_KV_PASSWORD_RE.subn(_mask_kv_password, text)
    record(CATEGORY_KV_PASSWORD, hits)

    assignment_hits = 0
    text, hits = _SECRET_SET_RE.subn(_mask_literal_after_prefix, text)
    assignment_hits += hits
    text, hits = _SECRET_DECLARE_RE.subn(_mask_literal_after_prefix, text)
    assignment_hits += hits
    record(CATEGORY_SECRET_ASSIGNMENT, assignment_hits)

    text, hits = _JWT_RE.subn(REDACTED, text)
    record(CATEGORY_JWT, hits)
    text, hits = _AWS_ACCESS_KEY_RE.subn(REDACTED, text)
    record(CATEGORY_AWS_ACCESS_KEY, hits)
    text, hits = _GITHUB_TOKEN_RE.subn(REDACTED, text)
    record(CATEGORY_GITHUB_TOKEN, hits)
    text, hits = _SLACK_TOKEN_RE.subn(REDACTED, text)
    record(CATEGORY_SLACK_TOKEN, hits)
    text, hits = _API_KEY_SK_RE.subn(REDACTED, text)
    record(CATEGORY_API_KEY, hits)
    text, hits = _LONG_TOKEN_LITERAL_RE.subn(rf"\1{REDACTED}\3", text)
    record(CATEGORY_LONG_TOKEN, hits)

    # Se cuenta DESPUÉS de enmascarar: el ``@`` de un URI ya redactado (``u:***@h``) no es un email.
    flagged: dict[str, int] = {}
    email_hits = len(_EMAIL_RE.findall(text))
    if email_hits:
        flagged[CATEGORY_EMAIL] = email_hits
    host_hits = len(_HOST_RE.findall(text))
    if host_hits:
        flagged[CATEGORY_HOST] = host_hits

    return RedactionResult(text=text, redactions=redactions, flagged=flagged)
