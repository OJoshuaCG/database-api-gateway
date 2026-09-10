"""
Las dos ERAS del protocolo MCP, y cómo se distinguen.

QUÉ CAMBIÓ, Y POR QUÉ ESTE MÓDULO EXISTE
----------------------------------------
La revisión **2026-07-28** rehizo el transporte HTTP y sacó cosas que eran el centro del diseño
anterior:

- **No hay handshake.** ``initialize`` y ``notifications/initialized`` **no existen**: el
  protocolo es *stateless* y cada request trae su propio contexto en ``params._meta``.
- **No hay sesiones de protocolo.** ``Mcp-Session-Id`` se retiró; un servidor de esta revisión
  tiene que **ignorarlo y no emitirlo**.
- **No hay stream por GET.** ``GET`` y ``DELETE`` al endpoint responden ``405``.
- **Hay headers obligatorios**, y el servidor tiene que validar que **coincidan con el cuerpo**:
  ``MCP-Protocol-Version``, ``Mcp-Method`` y —en ``tools/call``— ``Mcp-Name``. La razón es
  concreta y de seguridad: un balanceador puede rutear por el header mientras el servidor
  ejecuta por el cuerpo, y ahí hay una discrepancia explotable.

CÓMO SE DETECTA LA ERA, Y POR QUÉ ASÍ
-------------------------------------
Por la **presencia del header ``MCP-Protocol-Version``**, que es lo que la propia spec autoriza:
un servidor que quiera atender clientes anteriores a ``2025-06-18`` —que no lo definían— *puede*
tratar un request sin ese header como ``2025-03-26``, o sea la era del handshake.

Se soportan las dos porque el parque real tiene las dos, y porque el propio documento describe la
ruta de compatibilidad: un cliente moderno prueba primero lo moderno y, si recibe un ``400`` cuyo
cuerpo **no** es un error moderno reconocible, cae al ``initialize``. Si sirviéramos solo una era,
la mitad de los clientes no conectaría.

LOS CÓDIGOS DE ERROR NO SE INVENTAN
-----------------------------------
El rango ``-32020`` a ``-32099`` está **reservado por la especificación**: una implementación
**no puede** emitir ahí un código que la spec no defina. Los tres que existen son los de abajo, y
son los únicos que este módulo usa.
"""

from dataclasses import dataclass
from typing import Any

#: Versiones que este servidor implementa, de la más nueva a la más vieja.
#:
#: Las dos primeras son de la era stateless; las tres últimas, de la era del handshake. Están
#: en la misma tupla porque la negociación es sobre el mismo eje, y el servidor decide por la
#: PRESENCIA DEL HEADER —no por el número— qué reglas aplica.
SUPPORTED_VERSIONS = ("2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")

#: Versiones de la era STATELESS: sin handshake, con headers obligatorios y `_meta` por request.
STATELESS_VERSIONS = frozenset({"2026-07-28", "2025-11-25"})

LATEST_VERSION = SUPPORTED_VERSIONS[0]

#: Versión que se asume cuando el request NO trae el header. La spec lo autoriza explícitamente
#: para atender clientes anteriores a `2025-06-18`.
LEGACY_ASSUMED_VERSION = "2025-03-26"

# --------------------------------------------------------------------------- #
# Códigos reservados por la especificación. NO se inventan otros en el rango.  #
# --------------------------------------------------------------------------- #
HEADER_MISMATCH = -32020
MISSING_CLIENT_CAPABILITY = -32021
UNSUPPORTED_PROTOCOL_VERSION = -32022

#: Claves reservadas de `_meta`. El prefijo `io.modelcontextprotocol/` está reservado por la
#: spec, así que se escriben completas y no se construyen por concatenación.
META_PROTOCOL_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT_INFO = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPABILITIES = "io.modelcontextprotocol/clientCapabilities"
META_SERVER_INFO = "io.modelcontextprotocol/serverInfo"

#: Headers del transporte. Los nombres se comparan sin distinguir mayúsculas —lo pide RFC 9110 y
#: lo repite la spec— así que se guardan en minúscula y el lector normaliza.
H_PROTOCOL_VERSION = "mcp-protocol-version"
H_METHOD = "mcp-method"
H_NAME = "mcp-name"

#: Métodos que exigen `Mcp-Name`, tomado de `params.name` o `params.uri`.
METHODS_WITH_NAME = {
    "tools/call": "name",
    "resources/read": "uri",
    "prompts/get": "name",
}


@dataclass(frozen=True, slots=True)
class Respuesta:
    """
    Lo que el transporte tiene que devolver: status HTTP **y** cuerpo.

    Los dos juntos y no solo el cuerpo, porque en esta revisión el status **es parte del
    contrato**: una notificación es ``202`` sin cuerpo, un método desconocido es ``404``, una
    validación de headers es ``400`` y un origen ajeno es ``403``. Un servidor que contestara
    ``200`` a todo sería inválido incluso con el JSON correcto.
    """

    status: int
    body: dict | None = None


def _base64_sentinela(valor: str) -> str | None:
    """
    Decodifica el formato ``=?base64?…?=`` que la spec define para valores de header que no se
    pueden representar en ASCII plano.

    Devuelve ``None`` si no tiene esa forma. Los marcadores son **case-sensitive** y tienen que
    aparecer exactamente así: comparar sin distinguir mayúsculas aceptaría un valor que un
    cliente conforme nunca manda.
    """
    if valor.startswith("=?base64?") and valor.endswith("?="):
        import base64
        import binascii

        crudo = valor[len("=?base64?"): -len("?=")]
        try:
            return base64.b64decode(crudo, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return ""
    return None


def _valor_header(headers: dict[str, str], nombre: str) -> str | None:
    """Lee un header sin distinguir mayúsculas y le aplica el decodificado del sentinela."""
    for k, v in headers.items():
        if k.lower() == nombre:
            decodificado = _base64_sentinela(v)
            return decodificado if decodificado is not None else v
    return None


def es_stateless(headers: dict[str, str]) -> bool:
    """
    ``True`` si el request pertenece a la era sin handshake.

    Se decide por la PRESENCIA del header, no por su valor: un valor desconocido igual pertenece
    a la era nueva —y hay que contestarle ``UnsupportedProtocolVersion``, que es un error moderno
    reconocible— mientras que su ausencia es un cliente viejo al que hay que dejarle el
    ``initialize``.
    """
    return _valor_header(headers, H_PROTOCOL_VERSION) is not None


def error(code: int, message: str, *, rid: Any = None, data: dict | None = None) -> dict:
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    cuerpo: dict = {"jsonrpc": "2.0", "error": err}
    # Un error por un request malformado puede no tener `id` legible, y la spec permite
    # omitirlo en ese caso. Cuando sí lo hay, tiene que coincidir.
    if rid is not None:
        cuerpo["id"] = rid
    return cuerpo


def validar(payload: dict, headers: dict[str, str]) -> Respuesta | None:
    """
    Valida un request de la era stateless. Devuelve la ``Respuesta`` de rechazo, o ``None`` si
    está bien.

    El ORDEN importa y es el de la spec: primero la versión —porque un cliente con una versión
    que no soportamos tiene que recibir la lista de las que sí, no un error sobre un header
    que quizá su versión ni define—, después el calce header/cuerpo, y al final ``_meta``.
    """
    rid = payload.get("id")
    metodo = payload.get("method")
    # `isinstance` y no `or {}`, por el MISMO motivo que en el dispatch: un `params` truthy que
    # no sea dict —una cadena, un número— pasa el `or` y revienta en el `.get()` con un 500.
    # El arreglo tuvo que ir en los dos lados: este validador corre ANTES del dispatch, así que
    # arreglar solo allá movía el 500 de lugar en vez de eliminarlo. Lo detectó la sonda en
    # vivo, no los tests.
    crudos = payload.get("params")
    params = crudos if isinstance(crudos, dict) else {}
    meta_cruda = params.get("_meta")
    meta = meta_cruda if isinstance(meta_cruda, dict) else {}

    version_header = _valor_header(headers, H_PROTOCOL_VERSION)

    # 1) ¿Hablamos esa versión?
    if version_header not in SUPPORTED_VERSIONS:
        return Respuesta(
            400,
            error(
                UNSUPPORTED_PROTOCOL_VERSION,
                f"Versión de protocolo no soportada: {version_header!r}.",
                rid=rid,
                # `supported` es lo que le permite al cliente reintentar con una que sí
                # hablamos, en vez de caer al `initialize` de la era vieja.
                data={"supported": list(SUPPORTED_VERSIONS)},
            ),
        )

    # 2) El header tiene que COINCIDIR con `_meta`. No es redundancia: un intermediario puede
    #    rutear por el header mientras el servidor ejecuta por el cuerpo, y la discrepancia es
    #    explotable.
    version_meta = meta.get(META_PROTOCOL_VERSION)
    if version_meta != version_header:
        return Respuesta(
            400,
            error(
                HEADER_MISMATCH,
                (
                    f"MCP-Protocol-Version ({version_header!r}) no coincide con "
                    f"_meta.{META_PROTOCOL_VERSION} ({version_meta!r})."
                ),
                rid=rid,
            ),
        )

    metodo_header = _valor_header(headers, H_METHOD)
    if metodo_header is None:
        return Respuesta(
            400, error(HEADER_MISMATCH, "Falta el header Mcp-Method.", rid=rid)
        )
    if metodo_header != metodo:
        return Respuesta(
            400,
            error(
                HEADER_MISMATCH,
                f"Mcp-Method ({metodo_header!r}) no coincide con el método ({metodo!r}).",
                rid=rid,
            ),
        )

    campo = METHODS_WITH_NAME.get(metodo or "")
    if campo:
        nombre_header = _valor_header(headers, H_NAME)
        nombre_cuerpo = params.get(campo)
        if nombre_header is None:
            return Respuesta(
                400,
                error(HEADER_MISMATCH, f"Falta el header Mcp-Name para {metodo!r}.", rid=rid),
            )
        if nombre_header != nombre_cuerpo:
            return Respuesta(
                400,
                error(
                    HEADER_MISMATCH,
                    f"Mcp-Name ({nombre_header!r}) no coincide con params.{campo}.",
                    rid=rid,
                ),
            )

    # 3) `_meta` obligatorio. `clientCapabilities` puede ser `{}` pero tiene que ESTAR: un
    #    servidor no puede apoyarse en una capacidad que el cliente no declaró, así que la
    #    ausencia de la declaración no es lo mismo que una declaración vacía.
    if META_CLIENT_CAPABILITIES not in meta:
        return Respuesta(
            400,
            error(
                -32602,
                f"Falta _meta.{META_CLIENT_CAPABILITIES} (puede ser un objeto vacío).",
                rid=rid,
            ),
        )

    return None


def envolver_result(result: dict, *, server_info: dict) -> dict:
    """
    Completa un ``result`` con lo que la spec exige y recomienda.

    - ``resultType`` es **obligatorio** ("el `result` MUST include a `resultType` field"), y su
      ausencia un cliente la trata como ``"complete"`` solo por compatibilidad con versiones
      viejas. Se declara explícito.
    - ``_meta.serverInfo`` es un SHOULD por request: en un protocolo sin handshake es la única
      vía por la que el cliente sabe con qué habla.

    No sobreescribe un ``resultType`` que el llamador ya puso: el flujo de MRTR devuelve
    ``"input_required"`` y machacarlo rompería esa conversación.
    """
    salida = {"resultType": "complete", **result}
    meta = dict(salida.get("_meta") or {})
    meta.setdefault(META_SERVER_INFO, server_info)
    salida["_meta"] = meta
    return salida
