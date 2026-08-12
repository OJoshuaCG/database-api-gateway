"""
Servicio del catálogo de charsets/collations (tabla ``charset_collation_options``).

Responsabilidades (mismo rol que ``privilege_catalog.py`` para los privilegios):

- ``charset_option_seed_rows()`` / ``seed_charset_options()``: llenado idempotente en el
  arranque (lifespan). Crea las filas que falten y **preserva los toggles del operador**
  (``enabled`` / ``is_default``): un reinicio no vuelve a habilitar lo que alguien deshabilitó.
  El seed también corre en la migración Alembic; que exista en ambos lados es deliberado
  (una BD creada con ``Base.metadata.create_all`` —tests, dev rápido— no pasa por Alembic).
- ``list_options`` / ``create_option`` / ``update_option``: lectura y administración.
- ``resolve_enabled_combination`` / ``validate_enabled_combination``: **enforcement**. Es lo
  que ``create_database`` consulta ANTES de tocar el motor.

Por qué el match se hace en PYTHON y no con un ``WHERE charset = :x``:
la comparación de strings depende de la collation de la BD del gateway (MySQL/MariaDB
compara case-INsensitive por default; PostgreSQL, case-sensitive). Resolver en memoria hace
que la semántica sea idéntica corra el gateway sobre el motor que corra, y permite aplicar
la regla por familia (ver ``_norm_collation``). El catálogo es de decenas de filas: el costo
es irrelevante frente a la operación remota que sigue.
"""

import re

from app.core.database import Database
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.charset_collation_option import CharsetCollationOption

logger = get_logger(__name__)

FAMILY_MYSQL = "mysql"
FAMILY_POSTGRESQL = "postgresql"
ENGINE_FAMILIES = (FAMILY_MYSQL, FAMILY_POSTGRESQL)

# Cuántas combinaciones se listan en el error 422 (evita respuestas gigantes).
_MAX_LISTED = 50

# Whitelist de lo que un admin puede DAR DE ALTA en el catálogo. Es la última barrera antes
# de que un valor llegue al DDL: en MySQL/MariaDB charset y collation se interpolan como
# IDENTIFICADORES (el adapter los revalida) y en PostgreSQL el locale viaja como LITERAL de
# string —no es whitelisteable como identificador—, así que acotarlo acá es lo que impide
# que una entrada arbitraria del catálogo termine en un CREATE DATABASE.
_CHARSET_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
_COLLATION_MYSQL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,127}$")
# PostgreSQL: nombre de LOCALE del SO ('C', 'en_US.UTF-8', 'de_DE@euro').
_COLLATION_PG_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.@\-]{0,127}$")


# --------------------------------------------------------------------------- #
# Mapeo dialecto → familia                                                     #
# --------------------------------------------------------------------------- #
def engine_family(dialect: str) -> str:
    """
    Traduce el dialecto del servidor a la familia del catálogo.

    MySQL y MariaDB comparten catálogo de charsets/collations, así que caen en la MISMA
    familia; PostgreSQL va aparte. Un dialecto desconocido es un bug del gateway (el
    inventario solo guarda valores de ``EngineType``), no una entrada del usuario: se falla
    cerrado con 500 en vez de asumir una familia.
    """
    match dialect:
        case "mysql" | "mariadb":
            return FAMILY_MYSQL
        case "postgresql":
            return FAMILY_POSTGRESQL
        case _:
            raise AppHttpException(
                message="Motor no soportado por el catálogo de charsets/collations.",
                status_code=500,
                context={"dialect": dialect},
            )


def normalize_family(value: str) -> str:
    """Valida un ``engine_family`` recibido por API (422 si no es una familia conocida)."""
    family = (value or "").strip().casefold()
    if family not in ENGINE_FAMILIES:
        raise AppHttpException(
            message="engine_family inválida. Use: mysql (cubre MariaDB) o postgresql.",
            status_code=422,
            context={"engine_family": value},
            public_context={"allowed": list(ENGINE_FAMILIES)},
        )
    return family


# --------------------------------------------------------------------------- #
# Normalización de valores                                                     #
# --------------------------------------------------------------------------- #
def validate_option_values(
    family: str, charset: str, collation: str | None
) -> tuple[str, str]:
    """
    Valida sintácticamente una combinación que se quiere DAR DE ALTA y la devuelve lista para
    persistir (``collation`` con el centinela ``""`` si venía vacía/None). 422 si no pasa.
    """
    charset = (charset or "").strip()
    collation = (collation or "").strip()

    if not _CHARSET_RE.match(charset):
        raise AppHttpException(
            message="Nombre de charset inválido.",
            status_code=422,
            context={"charset": charset},
            public_context={"pattern": _CHARSET_RE.pattern},
        )
    if collation:
        pattern = _COLLATION_MYSQL_RE if family == FAMILY_MYSQL else _COLLATION_PG_RE
        if not pattern.match(collation):
            raise AppHttpException(
                message="Nombre de collation/locale inválido para esa familia de motor.",
                status_code=422,
                context={"collation": collation, "engine_family": family},
                public_context={"pattern": pattern.pattern},
            )
    return charset, collation


def _norm_charset(value: str) -> str:
    # El nombre del charset/encoding es case-insensitive en los tres motores
    # ('utf8mb4' == 'UTF8MB4'; PostgreSQL canonicaliza 'utf8'/'UTF8').
    return value.strip().casefold()


def _norm_collation(value: str, family: str) -> str:
    """
    MySQL/MariaDB: el nombre de collation es un identificador case-insensitive.
    PostgreSQL: NO es una collation SQL sino el LOCALE del sistema operativo
    ('en_US.UTF-8'), y ahí el case SÍ importa → se compara tal cual.
    """
    value = value.strip()
    return value.casefold() if family == FAMILY_MYSQL else value


# --------------------------------------------------------------------------- #
# Seed                                                                         #
# --------------------------------------------------------------------------- #
def charset_option_seed_rows() -> list[dict]:
    """
    Semilla inicial del catálogo. Habilitadas: solo las combinaciones seguras/modernas.
    Las demás se siembran DESHABILITADAS como referencia: existen en el motor, el operador
    las ve en la UI y decide explícitamente si quiere ofrecerlas.
    """
    return [
        # ---- MySQL / MariaDB ---------------------------------------------- #
        {
            "engine_family": FAMILY_MYSQL,
            "charset": "utf8mb4",
            "collation": "utf8mb4_unicode_ci",
            "enabled": True,
            "is_default": True,
        },
        {
            "engine_family": FAMILY_MYSQL,
            "charset": "utf8mb4",
            "collation": "utf8mb4_general_ci",
            "enabled": True,
            "is_default": False,
        },
        # Solo MySQL 8+ (MariaDB no la tiene): se deja deshabilitada para que el operador
        # la habilite a conciencia sabiendo que romperá la creación en MariaDB.
        {
            "engine_family": FAMILY_MYSQL,
            "charset": "utf8mb4",
            "collation": "utf8mb4_0900_ai_ci",
            "enabled": False,
            "is_default": False,
        },
        # utf8mb3 y latin1: legado. No cubren el plano Unicode completo (emoji, etc.).
        {
            "engine_family": FAMILY_MYSQL,
            "charset": "utf8mb3",
            "collation": "utf8mb3_general_ci",
            "enabled": False,
            "is_default": False,
        },
        {
            "engine_family": FAMILY_MYSQL,
            "charset": "latin1",
            "collation": "latin1_swedish_ci",
            "enabled": False,
            "is_default": False,
        },
        # ---- PostgreSQL ---------------------------------------------------- #
        # CAVEAT: el locale debe existir en el SO del servidor destino. Este catálogo es un
        # menú curado, no una garantía: el motor tiene la última palabra.
        {
            "engine_family": FAMILY_POSTGRESQL,
            "charset": "UTF8",
            "collation": "en_US.UTF-8",
            "enabled": True,
            "is_default": True,
        },
        {
            "engine_family": FAMILY_POSTGRESQL,
            "charset": "UTF8",
            "collation": "C",
            "enabled": False,
            "is_default": False,
        },
        {
            "engine_family": FAMILY_POSTGRESQL,
            "charset": "UTF8",
            "collation": "C.UTF-8",
            "enabled": False,
            "is_default": False,
        },
    ]


def seed_charset_options() -> None:
    """Idempotente. Crea las filas faltantes; NO pisa ``enabled``/``is_default`` existentes."""
    session = Database().get_declarative_base_session()
    try:
        existing = {
            (o.engine_family, o.charset, o.collation)
            for o in session.query(CharsetCollationOption).all()
        }
        created = 0
        for row in charset_option_seed_rows():
            key = (row["engine_family"], row["charset"], row["collation"])
            if key not in existing:
                session.add(CharsetCollationOption(**row))
                created += 1
        session.commit()
        if created:
            logger.info("Catálogo de charsets/collations sembrado: %d nuevos.", created)
    except Exception:  # noqa: BLE001 — el seeding no debe tumbar el arranque
        session.rollback()
        logger.exception("No se pudo sembrar el catálogo de charsets/collations.")
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Lectura / administración                                                     #
# --------------------------------------------------------------------------- #
def list_options(
    engine_family_filter: str | None = None, only_enabled: bool = False
) -> list[CharsetCollationOption]:
    session = Database().get_declarative_base_session()
    try:
        q = session.query(CharsetCollationOption)
        if engine_family_filter is not None:
            q = q.filter(CharsetCollationOption.engine_family == engine_family_filter)
        if only_enabled:
            q = q.filter(CharsetCollationOption.enabled.is_(True))
        rows = q.order_by(
            CharsetCollationOption.engine_family,
            CharsetCollationOption.charset,
            CharsetCollationOption.collation,
        ).all()
        for r in rows:
            session.expunge(r)
        return rows
    finally:
        session.close()


def find_option(family: str, charset: str, collation: str) -> CharsetCollationOption | None:
    """Busca la fila EXACTA de una combinación (ya normalizada por el llamador)."""
    session = Database().get_declarative_base_session()
    try:
        row = (
            session.query(CharsetCollationOption)
            .filter(
                CharsetCollationOption.engine_family == family,
                CharsetCollationOption.charset == charset,
                CharsetCollationOption.collation == collation,
            )
            .first()
        )
        if row is not None:
            session.expunge(row)
        return row
    finally:
        session.close()


def create_option(
    *, family: str, charset: str, collation: str, enabled: bool
) -> CharsetCollationOption:
    """
    Alta de una combinación custom (no sembrada). 409 si ya existe.

    NO se marca ``is_default`` en el alta: el default es una decisión aparte (PATCH), para no
    desplazar el default vigente por accidente al agregar una fila nueva.
    """
    session = Database().get_declarative_base_session()
    try:
        duplicate = (
            session.query(CharsetCollationOption)
            .filter(
                CharsetCollationOption.engine_family == family,
                CharsetCollationOption.charset == charset,
                CharsetCollationOption.collation == collation,
            )
            .first()
        )
        if duplicate is not None:
            raise AppHttpException(
                message="La combinación ya existe en el catálogo.",
                status_code=409,
                context={"id": duplicate.id},
                public_context={
                    "id": duplicate.id,
                    "engine_family": family,
                    "charset": charset,
                    "collation": collation or None,
                    "enabled": duplicate.enabled,
                },
            )
        row = CharsetCollationOption(
            engine_family=family,
            charset=charset,
            collation=collation,
            enabled=enabled,
            is_default=False,
        )
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row
    finally:
        session.close()


def update_option(
    option_id: int, *, enabled: bool | None, is_default: bool | None
) -> CharsetCollationOption:
    """
    PATCH parcial de ``enabled`` / ``is_default``. Reglas (todas en UNA transacción):

    - 404 si la fila no existe.
    - ``is_default=True`` desmarca el default previo de la MISMA familia (invariante "a lo
      sumo un default por familia"). Se hace en la aplicación y no con un índice único
      parcial porque MySQL/MariaDB no soportan índices parciales, y un UNIQUE plano sobre
      ``(engine_family, is_default)`` prohibiría también tener dos filas con ``False``.
    - Un default DEBE quedar habilitado (si no, la UI ofrecería por defecto algo que el
      enforcement rechaza). Se valida el estado RESULTANTE, no el enviado.
    """
    session = Database().get_declarative_base_session()
    try:
        row = session.get(CharsetCollationOption, option_id)
        if row is None:
            raise AppHttpException(
                message="Combinación charset/collation no encontrada en el catálogo.",
                status_code=404,
                context={"option_id": option_id},
            )

        new_enabled = row.enabled if enabled is None else enabled
        new_is_default = row.is_default if is_default is None else is_default

        if new_is_default and not new_enabled:
            raise AppHttpException(
                message=(
                    "Una combinación marcada como default debe estar habilitada; "
                    "habilítala o designa otro default primero."
                ),
                status_code=422,
                context={"option_id": option_id},
                public_context={"enabled": new_enabled, "is_default": new_is_default},
            )

        if new_is_default and not row.is_default:
            # Desmarcar el default previo de la familia (a lo sumo uno).
            session.query(CharsetCollationOption).filter(
                CharsetCollationOption.engine_family == row.engine_family,
                CharsetCollationOption.is_default.is_(True),
                CharsetCollationOption.id != row.id,
            ).update({"is_default": False}, synchronize_session=False)

        row.enabled = new_enabled
        row.is_default = new_is_default
        session.commit()
        session.refresh(row)
        session.expunge(row)
        return row
    finally:
        session.close()


# --------------------------------------------------------------------------- #
# Enforcement (lo consume create_database)                                     #
# --------------------------------------------------------------------------- #
def enabled_combinations(family: str) -> list[dict]:
    """Combinaciones habilitadas de una familia, en formato apto para un mensaje de error."""
    return [
        {
            "charset": o.charset,
            "collation": o.collation or None,
            "is_default": o.is_default,
        }
        for o in list_options(engine_family_filter=family, only_enabled=True)
    ]


def resolve_enabled_combination(
    dialect: str, charset: str | None, collation: str | None
) -> tuple[str | None, str | None]:
    """
    Verifica que la combinación pedida esté HABILITADA en el catálogo y devuelve los valores
    CANÓNICOS del catálogo (los que deben viajar al DDL).

    Devolver la forma canónica —y no el texto del cliente— es la parte que aporta seguridad:
    en PostgreSQL ``collation`` viaja al DDL como literal de string (es un locale del SO, no
    un identificador whitelisteable), así que acotarlo a un valor que salió de la tabla cierra
    esa superficie en lugar de solo taparla con un patrón.

    Semántica:
    - ``charset`` y ``collation`` ambos ``None`` → no se valida nada (el adapter aplica su
      default, que siempre corresponde a una fila ``is_default``). Devuelve ``(None, None)``.
    - solo ``charset`` → basta con que ALGUNA combinación habilitada use ese charset (el motor
      aplicará su collation por defecto para ese charset).
    - solo ``collation`` → debe existir una combinación habilitada con esa collation.
    - ambos → el PAR exacto debe estar habilitado.

    Nunca se rellena el valor que el llamador NO envió: hacerlo cambiaría en silencio el DDL
    respecto de lo que pidió.
    """
    if charset is None and collation is None:
        return None, None

    family = engine_family(dialect)
    options = list_options(engine_family_filter=family, only_enabled=True)

    wanted_cs = _norm_charset(charset) if charset is not None else None
    wanted_co = _norm_collation(collation, family) if collation is not None else None

    match (wanted_cs, wanted_co):
        case (str(), str()):
            hit = next(
                (
                    o
                    for o in options
                    if _norm_charset(o.charset) == wanted_cs
                    and _norm_collation(o.collation, family) == wanted_co
                ),
                None,
            )
            if hit is not None:
                return hit.charset, hit.collation
        case (str(), None):
            hit = next(
                (o for o in options if _norm_charset(o.charset) == wanted_cs), None
            )
            if hit is not None:
                return hit.charset, None
        case (None, str()):
            hit = next(
                (
                    o
                    for o in options
                    if o.collation
                    and _norm_collation(o.collation, family) == wanted_co
                ),
                None,
            )
            if hit is not None:
                return None, hit.collation

    allowed = [
        {"charset": o.charset, "collation": o.collation or None, "is_default": o.is_default}
        for o in options[:_MAX_LISTED]
    ]
    raise AppHttpException(
        message=(
            "La combinación charset/collation no está habilitada en el catálogo del gateway."
        ),
        status_code=422,
        context={
            "engine_family": family,
            "charset": charset,
            "collation": collation,
        },
        # public_context viaja SIEMPRE (context solo en development): sin esto, en producción
        # el operador recibiría "no está habilitada" sin saber qué sí puede elegir.
        public_context={
            "engine_family": family,
            "requested": {"charset": charset, "collation": collation},
            "allowed": allowed,
            "truncated": len(options) > _MAX_LISTED,
        },
    )


def validate_enabled_combination(
    engine_family_or_dialect: str, charset: str | None, collation: str | None
) -> None:
    """Guard sin valor de retorno (misma validación que ``resolve_enabled_combination``)."""
    resolve_enabled_combination(engine_family_or_dialect, charset, collation)
