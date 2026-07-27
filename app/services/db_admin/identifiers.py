"""
Seguridad de identificadores SQL (SEGURIDAD CRÍTICA).

Los nombres de objetos (base de datos, usuario, tabla, host) NO se pueden pasar
como bind params (:param) en DDL/DCL: van interpolados como identificadores. Para
evitar inyección usamos defensa en profundidad:

  1) VALIDACIÓN por whitelist estricta (lo más importante),
  2) QUOTING por dialecto con escape del delimitador,
  3) los VALORES (passwords) se parametrizan donde se puede; donde no, se escapan
     como string literal.

Nunca confiar solo en el quoting. Y nunca incluir el valor crudo en los mensajes
de error (evita reflejar payloads de inyección).
"""

import re

from app.exceptions import AppHttpException

# Whitelist conservadora común a todos los motores: letra/underscore inicial,
# luego alfanumérico/underscore. Elimina espacios, delimitadores, ';', '\', etc.
# Se usa para objetos que el gateway CREA (control total sobre el nombre).
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,62}$")

# Whitelist AMPLIADA para INTROSPECCIÓN de objetos PREEXISTENTES (que el gateway no
# creó): permite dígito inicial y los caracteres `. - $` comunes en nombres legados.
# Sigue rechazando espacios, comillas, backticks, ';', '\' y el byte nulo, así que el
# quoting por dialecto (quote_identifier) no puede romperse. Ver plan 00 (#3).
_IDENT_EXISTING_RE = re.compile(r"^[A-Za-z0-9_$][A-Za-z0-9_$.\-]{0,62}$")

# Longitud máxima del identificador por dialecto.
_MAX_LEN = {"mysql": 64, "mariadb": 64, "postgresql": 63}

# Host de MySQL: '%', o hostname/IP/subred. Conservador.
_HOST_RE = re.compile(r"^[A-Za-z0-9_.%:\-]{1,255}$")

# Privilegios permitidos en GRANT/REVOKE (tokens fijos, no input libre).
_PRIV_RE = re.compile(r"^[A-Z][A-Z ]*(,\s*[A-Z][A-Z ]*)*$")


# --------------------------------------------------------------------------- #
# Objetos INTERNOS del gateway que viven DENTRO de una BD gestionada            #
# --------------------------------------------------------------------------- #
# El gateway crea tablas de contabilidad propias dentro de las BDs que administra:
#
#   ``_gw_v_{slug}``   tabla de versión de Alembic por blueprint (migrations.py)
#   ``_gw_stg_{hash}`` tabla de staging temporal de la copia de datos (data_copy.py)
#
# Estas tablas NO son parte del esquema del usuario: son la infraestructura con la que el
# gateway se administra a sí mismo. **Tienen que quedar FUERA de todo snapshot
# estructural**, o se filtran al diff y el sistema termina generando DDL contra su propia
# contabilidad.
#
# Fallo REAL que motivó esto (2026-07-27): al comparar una BD origen (sin tabla de versión)
# contra una BD gestionada destino (con ``_gw_v_{slug}``), el diff clasificaba esa tabla
# como ``dropped`` —existe en el target, no en el source— y emitía
# ``DROP TABLE _gw_v_{slug}``. Adoptado como versión de blueprint y aplicado, Alembic leía
# la versión actual OK, ejecutaba todo el DDL (incluido el DROP de su propia tabla) y al
# registrar la versión nueva moría con ``(1146, "Table '..._gw_v_...' doesn't exist")``,
# dejando la BD con los cambios aplicados pero SIN puntero de versión.
#
# El caso simétrico es igual de grave: si el ORIGEN es una BD gestionada y el destino no,
# el diff emitiría ``CREATE TABLE _gw_v_{slug_del_origen}`` sobre el destino, inyectándole
# una tabla de versión ajena con la versión de otro blueprint.
#
# NOTA sobre ``gw_ou_{hash}`` (función + trigger que PostgreSQL usa para emular el
# ``ON UPDATE CURRENT_TIMESTAMP`` de MySQL): esos SÍ se dejan visibles a propósito.
# Implementan el comportamiento de una COLUMNA del usuario, así que son parte legítima del
# esquema resultante; excluirlos haría que el diff intentara recrearlos en cada corrida.
GATEWAY_TABLE_PREFIXES: tuple[str, ...] = ("_gw_v_", "_gw_stg_")


def is_gateway_internal_table(name: str) -> bool:
    """¿Esta tabla es contabilidad INTERNA del gateway (no del esquema del usuario)?"""
    if not name:
        return False
    lowered = name.lower()
    return any(lowered.startswith(p) for p in GATEWAY_TABLE_PREFIXES)


def exclude_gateway_internal_tables(names) -> list[str]:
    """Filtra de una lista de tablas las que son contabilidad interna del gateway."""
    return [n for n in names if not is_gateway_internal_table(n)]


def references_gateway_internal_table(sql: str) -> list[str]:
    """
    Prefijos de tablas internas del gateway mencionados en un script SQL.

    Lista vacía = el SQL no toca la contabilidad del gateway. Se usa como guard: ninguna
    migración de blueprint tiene motivo legítimo para nombrar estas tablas, así que
    encontrarlas es siempre un error de generación (o una versión creada antes del fix del
    snapshot). Se compara por SUBSTRING a propósito: los prefijos son reservados y
    distintivos, así que no hace falta parsear el SQL para decidir con certeza — y un
    falso positivo sería una tabla de usuario llamada ``_gw_v_…``, que el gateway
    confundiría con su propia contabilidad de todos modos.
    """
    if not sql:
        return []
    lowered = sql.lower()
    return [p for p in GATEWAY_TABLE_PREFIXES if p in lowered]


def validate_identifier(
    name: str, dialect: str, kind: str = "identificador", *, allow_existing: bool = False
) -> str:
    """
    Valida un identificador contra la whitelist. Devuelve el nombre intacto o
    lanza AppHttpException(422). NO incluye el valor crudo en el error.

    ``allow_existing=True`` usa la whitelist AMPLIADA (dígito inicial, `. - $`) para
    INTROSPECCIÓN de objetos preexistentes; sigue rechazando caracteres peligrosos.
    Para objetos que el gateway CREA, usar siempre la whitelist estricta (default).
    """
    pattern = _IDENT_EXISTING_RE if allow_existing else _IDENT_RE
    allowed_desc = (
        "[A-Za-z0-9_$][A-Za-z0-9_$.-]*" if allow_existing else "[A-Za-z_][A-Za-z0-9_]*"
    )
    max_len = _MAX_LEN.get(dialect, 63)
    if not isinstance(name, str) or not name:
        raise AppHttpException(
            message=f"El {kind} es vacío o inválido.",
            status_code=422,
            context={"kind": kind},
        )
    if len(name) > max_len:
        raise AppHttpException(
            message=f"El {kind} excede la longitud máxima.",
            status_code=422,
            context={"kind": kind, "max": max_len},
        )
    if not pattern.match(name):
        raise AppHttpException(
            message=f"El {kind} contiene caracteres no permitidos.",
            status_code=422,
            context={"kind": kind, "allowed": allowed_desc},
        )
    return name


def validate_host(host: str) -> str:
    """Valida el host de un usuario MySQL ('user'@'host'). Lanza 422 si inválido."""
    if not isinstance(host, str) or not _HOST_RE.match(host):
        raise AppHttpException(
            message="El host del usuario contiene caracteres no permitidos.",
            status_code=422,
            context={"kind": "host"},
        )
    return host


def validate_privileges(privileges: str) -> str:
    """Valida una lista de privilegios para GRANT/REVOKE."""
    value = (privileges or "").strip().upper()
    if not _PRIV_RE.match(value):
        raise AppHttpException(
            message="La lista de privilegios es inválida.",
            status_code=422,
            context={"kind": "privileges"},
        )
    return value


def quote_identifier(name: str, dialect: str) -> str:
    """Quotea un identificador YA validado (segunda capa de defensa)."""
    if dialect in ("mysql", "mariadb"):
        return "`" + name.replace("`", "``") + "`"
    return '"' + name.replace('"', '""') + '"'  # postgresql


def quote_string_literal(value: str, dialect: str) -> str:
    """
    Escapa un VALOR como string literal SQL. Solo para casos donde el dialecto no
    admite bind param (p.ej. `IDENTIFIED BY '...'`). Rechaza bytes nulos.
    """
    if not isinstance(value, str):
        raise AppHttpException(
            message="Valor de cadena inválido.", status_code=422
        )
    if "\x00" in value:
        raise AppHttpException(
            message="El valor contiene un byte nulo no permitido.", status_code=422
        )
    if dialect in ("mysql", "mariadb"):
        # Doblar la comilla simple ('') escapa el quote en CUALQUIER sql_mode,
        # incluido NO_BACKSLASH_ESCAPES (donde '\'' NO escaparía y permitiría
        # romper el literal). El backslash se dobla para el sql_mode por defecto.
        escaped = value.replace("\\", "\\\\").replace("'", "''")
        return "'" + escaped + "'"
    # postgresql: doblar comilla simple; usar E'' si hay backslash.
    escaped = value.replace("'", "''")
    if "\\" in value:
        return "E'" + escaped.replace("\\", "\\\\") + "'"
    return "'" + escaped + "'"
