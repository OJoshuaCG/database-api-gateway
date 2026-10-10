"""
Tools de blueprints: ``list_blueprints``, ``list_blueprint_migrations`` y ``get_blueprint_migration``.
**No tocan ningún motor.**

Leen la BD de metadatos del gateway a través de ``ToolContext``. Las dos listas devuelven SOLO
metadatos: el SQL de las migraciones no sale por ellas. Un blueprint es visible si está vinculado al
proyecto del token y a ningún otro; para cualquier otro caso (ajeno, compartido, inexistente) la
respuesta es la misma ``mcp.not_found`` con el mismo mensaje.

NO LLEVAN ``database``
----------------------
El sobre es ``BlueprintEnvelope`` (fuente ``gateway_blueprint``). Un blueprint no es una base, y la
visibilidad no depende de que alguna de sus bases sea alcanzable: un blueprint sin ninguna base
alcanzable se lista igual.

NOMBRES Y DESCRIPCIONES SON TEXTO DE TERCEROS
---------------------------------------------
Los escribió quien creó el blueprint o la migración. Salen por ``Tracker.free_text`` (saneados,
capados y anotados en ``untrusted_fields``) bajo el ``notice`` del sobre. El control real, como en
el resto del paquete, es que ninguna tool escribe.

``get_blueprint_migration`` ENTREGA SQL DE TERCEROS
---------------------------------------------------
Es la única de las tres que devuelve cuerpos SQL (``up_sql``, ``down_sql``, ``down_sql_suggested``),
y una migración ``kind='data'`` puede llevar filas semilla. Por eso vive bajo su propio scope
(``data.blueprint_sql``) y su kill switch, y el orden es el de ``get_definition``: switch primero,
argumentos después, y recién entonces la lectura con su auditoría fail-closed. Los cuerpos salen por
``Tracker.code_body``: redactados (best effort, NO una frontera), anotados y jamás recortados.

Si la respuesta no entra en el tope del despachador el error es ``mcp.blueprint_sql_too_large`` con
los tres tamaños en ``details``: un SQL cortado a mitad es peor que ausente. La medida usa la misma
fórmula que el despachador (``result_budget``), así que lo que esta tool da por bueno el despachador
no lo rechaza.
"""

from __future__ import annotations

import re

from app.exceptions import AppHttpException
from app.mcp.context import ToolContext
from app.mcp.result_budget import MAX_RESULT_BYTES, serialized_result_bytes
from app.mcp.tools._envelope import Tracker, clean, iso, now_iso
from app.schemas import mcp as out
from app.services import mcp_catalog as codes

#: Forma de una versión de migración tal como la valida la API de blueprints. Se escribe con
#: ``[0-9]`` y no con ``\d`` porque ``\d`` acepta dígitos de otros alfabetos. Se publica en el
#: schema (``registry``) y se vuelve a exigir acá, porque el despachador solo rechaza claves
#: no declaradas y no valida tipos ni patrones.
VERSION_PATTERN = r"^[0-9]{1,10}$"
_VERSION_REGEX = re.compile(r"[0-9]{1,10}")

#: Página de ``list_blueprint_migrations``. 200 filas sin cuerpos SQL caben de sobra en el
#: presupuesto de bytes del despachador; el predeterminado es la mitad para no gastarlo de entrada.
MIGRATIONS_PAGE_MIN = 1
MIGRATIONS_PAGE_MAX = 200
MIGRATIONS_PAGE_DEFAULT = 100


def _invalid(message: str) -> AppHttpException:
    return AppHttpException(
        message=message, status_code=422, public_context={"code": codes.CODE_INVALID_ARGUMENT}
    )


def _is_plain_int(value) -> bool:
    """``True`` es un ``int`` en Python: sin este chequeo ``blueprint_id: true`` pasaría por 1."""
    return isinstance(value, int) and not isinstance(value, bool)


def _blueprint_id(params: dict) -> int:
    blueprint_id = params.get("blueprint_id")
    if not _is_plain_int(blueprint_id) or blueprint_id < 1:
        raise _invalid(
            "'blueprint_id' tiene que ser un entero positivo (el id que devuelve list_blueprints)."
        )
    return blueprint_id


def _after_version(params: dict) -> str | None:
    after_version = params.get("after_version")
    if after_version is None:
        return None
    if not isinstance(after_version, str) or _VERSION_REGEX.fullmatch(after_version) is None:
        raise _invalid("'after_version' tiene que ser una versión de solo dígitos (hasta 10).")
    return after_version


def _version(params: dict) -> str:
    version = params.get("version")
    if not isinstance(version, str) or _VERSION_REGEX.fullmatch(version) is None:
        raise _invalid(
            "'version' tiene que ser una versión de solo dígitos (hasta 10), tal como la devuelve "
            "list_blueprint_migrations."
        )
    return version


def _limit(params: dict) -> int:
    limit = params.get("limit")
    if limit is None:
        return MIGRATIONS_PAGE_DEFAULT
    if not _is_plain_int(limit) or not (MIGRATIONS_PAGE_MIN <= limit <= MIGRATIONS_PAGE_MAX):
        raise _invalid(
            f"'limit' tiene que ser un entero entre {MIGRATIONS_PAGE_MIN} y {MIGRATIONS_PAGE_MAX}."
        )
    return limit


def _blueprint_envelope(
    data, *, tracker: Tracker, warnings: list[out.WarningOut] | None = None
) -> dict:
    envelope = out.BlueprintEnvelope(
        data=data,
        source="gateway_blueprint",
        untrusted_content=True,
        untrusted_fields=tracker.untrusted,
        clipped_fields=tracker.clipped,
        warnings=warnings or [],
        generated_at=now_iso(),
    )
    return envelope.model_dump(mode="json")


def list_blueprints(ctx: ToolContext, params: dict) -> dict:
    """
    Los blueprints del proyecto del token, ordenados por ``slug``. Sin paginación: si hay más de
    los que se pueden listar la llamada falla con ``mcp.too_many_objects`` en lugar de cortar.
    """
    summaries = ctx.list_blueprints()
    tracker = Tracker()
    items = [
        out.BlueprintOut(
            blueprint_id=summary.blueprint_id,
            slug=clean(summary.slug),
            name=tracker.free_text(summary.name, f"data.blueprints[{index}].name"),
            description=tracker.free_text(
                summary.description, f"data.blueprints[{index}].description"
            ),
            current_version=clean(summary.current_version),
            is_active=summary.is_active,
            charset=clean(summary.charset),
            collation=clean(summary.collation),
            migration_count=summary.migration_count,
        )
        for index, summary in enumerate(summaries)
    ]
    data = out.BlueprintListOut(blueprints=items, count=len(items))
    return _blueprint_envelope(data, tracker=tracker)


def list_blueprint_migrations(ctx: ToolContext, params: dict) -> dict:
    """
    Una página de migraciones de un blueprint visible, sin cuerpos SQL, en orden numérico de
    versión. ``next_after_version`` apunta a la página siguiente; ``total`` es el del blueprint.
    """
    blueprint_id = _blueprint_id(params)
    after_version = _after_version(params)
    limit = _limit(params)

    page = ctx.list_blueprint_migrations(blueprint_id, after_version, limit)

    tracker = Tracker()
    items = [
        out.BlueprintMigrationOut(
            version=clean(migration.version),
            name=tracker.free_text(migration.name, f"data.migrations[{index}].name"),
            kind=clean(migration.kind),
            is_baseline=migration.is_baseline,
            reviewed=migration.reviewed,
            has_rollback=migration.has_rollback,
            source_engine=clean(migration.source_engine),
            has_procedural_objects=migration.has_procedural_objects,
            checksum=clean(migration.checksum),
            created_at=iso(migration.created_at),
        )
        for index, migration in enumerate(page.migrations)
    ]
    data = out.BlueprintMigrationListOut(
        blueprint=out.BlueprintRefOut(
            blueprint_id=page.blueprint_id,
            slug=clean(page.blueprint_slug),
            current_version=clean(page.blueprint_current_version),
        ),
        migrations=items,
        count=len(items),
        total=page.total,
        next_after_version=page.next_after_version,
    )
    return _blueprint_envelope(data, tracker=tracker)


def _utf8_size(text: str | None) -> int:
    return len(text.encode("utf-8")) if text is not None else 0


def _redaction_counts(counter: dict[str, int]) -> list[out.RedactionCountOut]:
    """Conteos por categoría en orden estable. Jamás el valor enmascarado."""
    return [
        out.RedactionCountOut(category=category, count=count)
        for category, count in sorted(counter.items())
    ]


def _sql_too_large(sql_bytes: int, response_bytes: int) -> AppHttpException:
    """
    El 413 de un SQL que no entra. ``details`` lleva los tres tamaños para que el agente (o quien lo
    opera) sepa por cuánto no entró; el mensaje y los detalles nunca llevan SQL.
    """
    return AppHttpException(
        message=(
            f"El SQL de la migración no entra en la respuesta (tope de {MAX_RESULT_BYTES} bytes). "
            "No se recorta a propósito: un SQL cortado a mitad es peor que ausente."
        ),
        status_code=413,
        public_context={
            "code": codes.CODE_BLUEPRINT_SQL_TOO_LARGE,
            "details": {
                "sql_bytes": sql_bytes,
                "response_bytes": response_bytes,
                "max_response_bytes": MAX_RESULT_BYTES,
            },
        },
    )


def get_blueprint_migration(ctx: ToolContext, params: dict) -> dict:
    """
    El SQL de UNA migración de un blueprint visible, identificada por ``(blueprint_id, version)``.

    Orden: kill switch, argumentos, lectura (con la auditoría de intención fail-closed). Un
    blueprint ajeno, compartido o inexistente y una versión que no existe vuelven con la misma
    ``mcp.not_found``. ``down_sql`` y ``down_sql_suggested`` pueden ser ``None``; la llamada no
    falla por eso. Si la respuesta superaría el tope del despachador falla con
    ``mcp.blueprint_sql_too_large`` y sin ningún cuerpo.
    """
    ctx.assert_blueprint_sql_enabled()
    blueprint_id = _blueprint_id(params)
    version = _version(params)

    migration = ctx.get_blueprint_migration(blueprint_id, version)

    tracker = Tracker()
    name = tracker.free_text(migration.name, "data.name")
    up_sql = tracker.code_body(migration.up_sql, "data.up_sql", redact=ctx.redact_text)
    down_sql = tracker.code_body(migration.down_sql, "data.down_sql", redact=ctx.redact_text)
    down_sql_suggested = tracker.code_body(
        migration.down_sql_suggested, "data.down_sql_suggested", redact=ctx.redact_text
    )
    sql_bytes = _utf8_size(up_sql) + _utf8_size(down_sql) + _utf8_size(down_sql_suggested)

    data = out.BlueprintMigrationSqlOut(
        blueprint=out.BlueprintRefOut(
            blueprint_id=migration.blueprint_id,
            slug=clean(migration.blueprint_slug),
            current_version=clean(migration.blueprint_current_version),
        ),
        version=clean(migration.version),
        name=name,
        kind=clean(migration.kind),
        is_baseline=migration.is_baseline,
        reviewed=migration.reviewed,
        source_engine=clean(migration.source_engine),
        has_procedural_objects=migration.has_procedural_objects,
        checksum=clean(migration.checksum),
        up_sql=up_sql,
        down_sql=down_sql,
        down_sql_suggested=down_sql_suggested,
        sql_bytes=sql_bytes,
        redactions=_redaction_counts(migration.redactions),
        created_at=iso(migration.created_at),
    )

    warnings: list[out.WarningOut] = []
    if migration.kind == "data":
        warnings.append(
            out.WarningOut(
                code=codes.WARN_BLUEPRINT_DATA_MIGRATION,
                message=(
                    "La migración es de datos (kind='data'): sus sentencias pueden llevar filas "
                    "semilla de terceros, no solo estructura. Es contenido no confiable."
                ),
            )
        )
    if migration.redactions:
        warnings.append(
            out.WarningOut(
                code=codes.WARN_BLUEPRINT_SQL_REDACTED,
                message=(
                    "Se enmascararon credenciales en algún cuerpo SQL. La redacción es best "
                    "effort y no garantiza que no quede ninguna."
                ),
            )
        )

    envelope = _blueprint_envelope(data, tracker=tracker, warnings=warnings)
    response_bytes = serialized_result_bytes(envelope)
    if response_bytes > MAX_RESULT_BYTES:
        raise _sql_too_large(sql_bytes, response_bytes)
    return envelope
