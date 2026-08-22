"""
Contrato común de los adaptadores de servidor.

`ServerAdapter` define las operaciones que el gateway ejecuta contra un servidor
destino. La introspección (read-only) y test_connection son concretas aquí porque
el `Inspector` de SQLAlchemy es cross-dialect y nunca lee filas. Las operaciones
específicas de cada motor (listar BDs/usuarios, DDL/DCL) son abstractas.

Las operaciones de ESCRITURA (create/drop database/user, grants) están definidas
en el contrato e implementadas por cada subclase, pero NO se exponen vía API en la
Iteración 1 (solo se usarán a partir de la Iteración 2).
"""

import re
from abc import ABC, abstractmethod
from collections.abc import Sequence
from contextlib import contextmanager

from sqlalchemy import Connection, MetaData, Table, inspect, select, text
from sqlalchemy.exc import NoSuchTableError, SQLAlchemyError

from app.core.remote_engine import (
    ServerTarget,
    database_connection,
    map_driver_error,
    server_connection,
)
from app.exceptions import AppHttpException
from app.services.db_admin import snapshot_data
from app.services.db_admin.dtos import (
    CheckConstraintInfo,
    CollatableForeignKey,
    CollationInventory,
    CollationOptionInfo,
    ColumnCollationInfo,
    ColumnInfo,
    ComputedInfo,
    ConnectionInfo,
    DatabaseGranteeInfo,
    EngineUserInfo,
    EnumTypeInfo,
    EventInfo,
    ExtensionInfo,
    ExternalFkDependent,
    ForeignKeyInfo,
    GrantInfo,
    GrantLevel,
    IdentityInfo,
    IndexInfo,
    ObjectRef,
    RoutineGrantInfo,
    RoutineInfo,
    SchemaSnapshot,
    SeedResult,
    SequenceInfo,
    StructureDump,
    TableCollationInfo,
    TableSchema,
    TableStat,
    TriggerInfo,
    UniqueConstraintInfo,
    ViewInfo,
)
from app.services.db_admin.identifiers import (
    exclude_gateway_internal_tables,
    quote_identifier,
    validate_identifier,
)
from app.services.db_admin.schema_diff import (
    DiffItem,
    RenderedStatement,
    SchemaDiff,
)
from app.services.db_admin.sql_dialect import body_delimiter_wrapper


class ServerAdapter(ABC):
    dialect: str

    # ¿El motor modela usuarios por par ``'user'@'host'`` (varios hosts por username)?
    # MySQL/MariaDB: True. PostgreSQL: False (un rol no tiene host; el acceso por host se
    # controla en pg_hba.conf, fuera del alcance SQL). Gobierna la vista agrupada y si se
    # permite "agregar host".
    supports_hosts: bool = True

    # Tipos de rutina admitidos en grants de EXECUTE/ALTER ROUTINE.
    _ROUTINE_KINDS = frozenset({"FUNCTION", "PROCEDURE"})

    def __init__(self, target: ServerTarget):
        self.target = target

    # ---- Helpers de validación de object_ref (compartidos por los adapters) --- #
    @staticmethod
    def _require_field(value: str | None, kind: str) -> str:
        if not value:
            raise AppHttpException(
                message=f"Falta '{kind}' para la operación de permiso.",
                status_code=422,
                context={"missing": kind},
            )
        return value

    @classmethod
    def _routine_kind(cls, routine) -> str:
        if routine is None:
            raise AppHttpException(
                message="Falta la rutina (routine) para el grant.", status_code=422
            )
        kind = (routine.kind or "").upper()
        if kind not in cls._ROUTINE_KINDS:
            raise AppHttpException(
                message="Tipo de rutina inválido (use FUNCTION o PROCEDURE).",
                status_code=422,
                context={"allowed": sorted(cls._ROUTINE_KINDS)},
            )
        return kind

    # ------------------------------------------------------------------ #
    # Snapshot: sanitización de DEFINER/owner (compartida; Plan 09)       #
    # ------------------------------------------------------------------ #
    # MySQL: DEFINER=`user`@`host`  |  SQL SECURITY DEFINER (vistas/rutinas/triggers).
    _DEFINER_RE = re.compile(
        r"\s+DEFINER\s*=\s*(`[^`]*`@`[^`]*`|'[^']*'@'[^']*'|\"[^\"]*\"@\"[^\"]*\"|\S+)",
        re.IGNORECASE,
    )

    @classmethod
    def _strip_definer_clause(cls, ddl: str) -> str:
        """
        Quita la cláusula ``DEFINER=...`` de un DDL capturado (MySQL/MariaDB).

        Capturar el DEFINER literal haría fallar el re-apply en otro servidor donde ese
        usuario no existe. Tras quitarlo, el motor usa el invocador/owner del destino.
        ``SQL SECURITY DEFINER`` se deja intacto (es válido y no referencia un usuario
        concreto); el riesgo de escalada se documenta para revisión del admin.
        """
        return cls._DEFINER_RE.sub("", ddl)

    # ------------------------------------------------------------------ #
    # Específico de dialecto                                              #
    # ------------------------------------------------------------------ #
    @abstractmethod
    def _version_sql(self) -> str:
        """Sentencia que devuelve la versión del servidor."""

    @abstractmethod
    def _inspect_schema(self, database: str) -> str:
        """Schema que el Inspector debe usar para esta BD (MySQL: la BD; PG: 'public')."""

    @abstractmethod
    def list_databases(self) -> list[str]:
        """Lista BDs reales del servidor, excluyendo las del sistema."""

    @abstractmethod
    def list_users(self) -> list[EngineUserInfo]:
        """Lista usuarios/roles del motor, excluyendo los internos."""

    def external_fk_dependents(self, database: str) -> list[ExternalFkDependent]:
        """
        FKs desde una tabla de OTRA base de datos del MISMO servidor hacia una tabla de
        ``database``. El snapshot estructural (``structural_snapshot``) es de una sola BD
        y nunca puede ver esto — usado para advertir ANTES de limpiar/dropear ``database``
        (el clon: ``clean_mode=objects``/``drop_database``), ya que esas referencias
        externas pueden bloquear un ``DROP TABLE``/``DROP DATABASE`` con
        ``(1451, 'Cannot delete or update a parent row...')`` sin que el orden topológico
        de los DROP (que solo ve tablas DENTRO de ``database``) tenga forma de evitarlo.

        Default: lista vacía (PostgreSQL no soporta FKs cross-database por arquitectura —
        una BD no puede referenciar tablas de otra). MySQL/MariaDB lo sobreescriben.
        """
        return []

    # ------------------------------------------------------------------ #
    # Conversión de charset/collation (feature collation-conversion)      #
    # ------------------------------------------------------------------ #
    # DOS operaciones distintas bajo el mismo recurso, según el motor:
    #
    # - MySQL/MariaDB (modo ``universal``): BD + tablas + los 5 tipos de objeto con la
    #   collation CONGELADA en el momento de crearlos (PROCEDURE/FUNCTION/TRIGGER/EVENT/
    #   VIEW), que solo se arregla con DROP+CREATE verbatim.
    # - PostgreSQL (modo ``columns``): SOLO ``ALTER TABLE ... ALTER COLUMN ... TYPE ...
    #   COLLATE ...``. No hay objetos que recrear (PostgreSQL resuelve la collation
    #   DINÁMICAMENTE en cada llamada, no la congela al crear) ni ``ALTER DATABASE`` posible
    #   (el ``ENCODING``/``LC_COLLATE`` es INMUTABLE tras el ``CREATE DATABASE``).
    #
    # Por eso ``capture_object_ddl``/``routine_grants``/``apply_routine_grants`` siguen en
    # 422 para PostgreSQL: no son "pendientes de implementar", son pasos que su modo NUNCA
    # ejecuta. Un 422 desde ahí significaría que el despacho por modo se rompió.
    supports_collation_conversion: bool = False

    def collation_inventory(
        self, database: str, *, target_collation: str | None = None
    ) -> "CollationInventory":
        """
        Inventario de conversión: default de charset/collation de la BD, tablas con su
        charset/collation actual (+ cuántas columnas quedan fuera del objetivo), resumen
        agrupado por par ``(charset, collation)`` y los 5 tipos de objeto con collation
        congelada. Solo lectura del catálogo.
        """
        raise AppHttpException(
            message=(
                "La conversión de charset/collation no está soportada para este motor. "
                "Solo MySQL/MariaDB arrastran la collation congelada en rutinas, triggers, "
                "eventos y vistas."
            ),
            status_code=422,
            context={"dialect": self.dialect},
        )

    def capture_object_ddl(self, database: str, object_type: str, name: str) -> str:
        """
        DDL EXACTO de UN objeto (``SHOW CREATE PROCEDURE|FUNCTION|TRIGGER|EVENT|VIEW``),
        para recrearlo verbatim tras el DROP. Puntual a propósito: usar el dump completo
        del schema para 3 de 200 objetos sería trabajo desperdiciado.
        """
        raise AppHttpException(
            message="La captura puntual de DDL no está soportada para este motor.",
            status_code=422,
            context={"dialect": self.dialect},
        )

    def routine_grants(
        self, database: str, routine_type: str, name: str
    ) -> list["RoutineGrantInfo"]:
        """
        Privilegios a nivel de RUTINA sobre una PROCEDURE/FUNCTION concreta, para poder
        reaplicarlos tras el DROP+CREATE (el motor los borra junto con la rutina).
        """
        raise AppHttpException(
            message="La lectura de privilegios de rutina no está soportada para este motor.",
            status_code=422,
            context={"dialect": self.dialect},
        )

    def apply_routine_grants(
        self, database: str, grants: list["RoutineGrantInfo"]
    ) -> int:
        """Reaplica los privilegios de rutina capturados. Devuelve cuántos se aplicaron."""
        raise AppHttpException(
            message="La reaplicación de privilegios de rutina no está soportada para este motor.",
            status_code=422,
            context={"dialect": self.dialect},
        )

    def list_collations(self, database: str) -> list["CollationOptionInfo"]:
        """
        Collations que EXISTEN en el servidor y son usables por ``database`` (modo
        ``columns``). Lista vacía por default: en MySQL/MariaDB el objetivo se valida contra
        el catálogo GLOBAL del gateway (``charset_collation_options``), no contra el motor.

        PostgreSQL lo sobreescribe leyendo ``pg_collation`` EN VIVO porque su catálogo
        depende de los locales instalados en el SO de CADA servidor: no hay lista global
        posible ni compartible entre servidores.
        """
        return []

    def columns_to_convert(
        self, table: "TableCollationInfo", collation: str
    ) -> list["ColumnCollationInfo"]:
        """
        Columnas de ``table`` que hay que alterar para llegar a ``collation`` (modo
        ``columns``). Lista vacía por default: en el modo ``universal`` la unidad de cambio
        es la TABLA entera (``CONVERT TO CHARACTER SET``), no la columna.
        """
        return []

    def collatable_foreign_keys(self, database: str) -> list["CollatableForeignKey"]:
        """
        FKs INTERNAS de ``database`` entre columnas colacionables, para advertir sobre una
        conversión PARCIAL. Lista vacía por default.

        Es específico del modo ``columns``: en PostgreSQL convertir un lado de una FK de
        texto y el otro no deja dos columnas con collations explícitas distintas, y las
        comparaciones entre ellas fallan al EJECUTARSE (no al crear el constraint). El modo
        ``universal`` ya cubre su caso equivalente por otra vía (MySQL/MariaDB rechazan el
        propio DDL) y no necesita esta consulta.
        """
        return []

    @abstractmethod
    def dump_structure(
        self, database: str, *, conn: Connection | None = None
    ) -> "StructureDump":
        """
        Dump estructural COMPLETO de la BD (tablas, vistas, rutinas, triggers, y
        según motor: secuencias, tipos, extensiones, events). SOLO estructura, jamás
        filas. Las sentencias vienen YA en orden de dependencia para re-aplicarse.
        Plan 09 (adopción + snapshot como blueprint baseline).

        ``conn`` (§6.4 del módulo de exportación): leer con la conexión del llamador para
        que el dump entre en su transacción. ``None`` = comportamiento histórico.
        """

    @abstractmethod
    def _estimate_rows(self, conn, table: str, schema: str) -> int | None:
        """
        Estimación de filas de una tabla desde el catálogo (rápida, aproximada; NO
        cuenta filas). MySQL: ``information_schema.TABLES.TABLE_ROWS``; PostgreSQL:
        ``pg_class.reltuples``. Solo para informar la selección de datos-semilla.

        ``None`` = **el catálogo no lo sabe** (``reltuples`` en -1 porque nunca corrió
        ``ANALYZE``, o ``TABLE_ROWS`` en NULL). Devolver 0 en ese caso hacía que una tabla
        de millones de filas se informara como vacía, que es peor que no informar nada.
        """

    # ------------------------------------------------------------------ #
    # Escritura (contrato; uso por API a partir de la Iteración 2)        #
    # ------------------------------------------------------------------ #
    @abstractmethod
    def create_database(
        self, db_name: str, charset: str | None = None, collation: str | None = None,
        owner: str | None = None,
    ) -> None: ...

    @abstractmethod
    def drop_database(self, db_name: str, *, force_disconnect: bool = False) -> None: ...

    def supports_charset_combination(
        self, charset: str | None, collation: str | None
    ) -> bool | None:
        """
        ¿Este servidor concreto ofrece esa combinación de charset/collation?

        Existe porque el catálogo del gateway es necesario pero **no suficiente**:
        ``engine_family`` mete MySQL y MariaDB en la misma familia y no comparten todas las
        collations (``utf8mb4_0900_ai_ci`` es de MySQL 8; las ``utf8mb4_uca1400_*`` de
        MariaDB reciente), y en PostgreSQL la collation es un locale del SISTEMA OPERATIVO
        del host. Preguntarle al motor ANTES de ejecutar evita el peor caso del clon: con
        ``clean_mode='drop_database'`` el pipeline hace DROP y después CREATE, así que un par
        que el motor rechaza deja el destino BORRADO.

        Tres respuestas, y la tercera importa: ``True`` = disponible, ``False`` = el motor NO
        la tiene (bloquea), ``None`` = **no se pudo determinar**. ``None`` no bloquea: un
        "no sé" que se comporta como "no" prohibiría combinaciones perfectamente válidas.
        """
        return None

    def active_connections(self, db_name: str) -> int:
        """
        Cantidad de conexiones activas a ``db_name`` (informativo para el preview de
        borrado). Default 0; cada motor lo implementa (MySQL ``information_schema.
        PROCESSLIST``; PostgreSQL ``pg_stat_activity``).
        """
        return 0

    def list_database_grantees(self, db_name: str) -> list["DatabaseGranteeInfo"]:
        """
        Usuarios/roles con ALGÚN privilegio sobre ``db_name`` (consulta INVERSA, agrupada
        por grantee). Default ``[]``; cada motor lo implementa. En MySQL/MariaDB incluye
        los privilegios globales ``*.*`` (marcados ``is_global``); en PostgreSQL combina el
        ``datacl`` de la BD (CONNECT/CREATE/TEMP + owner) con los grants de objeto.
        """
        return []

    @abstractmethod
    def create_user(self, username: str, password: str, host: str = "%") -> None: ...

    @abstractmethod
    def drop_user(self, username: str, host: str = "%") -> None: ...

    @abstractmethod
    def change_password(self, username: str, new_password: str, host: str = "%") -> None: ...

    def add_user_host(
        self,
        username: str,
        source_host: str,
        new_host: str,
        *,
        new_password: str | None = None,
    ) -> None:
        """
        Clona una cuenta existente a un ``new_host`` (agregar host a un usuario).

        Solo tiene sentido en motores con ``supports_hosts=True`` (MySQL/MariaDB): ahí
        ``'user'@'hostA'`` y ``'user'@'hostB'`` son cuentas separadas. ``new_password``
        None ⇒ se copia el hash de la cuenta origen (misma contraseña, sin conocerla en
        claro); con valor ⇒ se fija esa contraseña nueva. El default rechaza (422); cada
        motor que lo soporte sobreescribe.
        """
        raise AppHttpException(
            message="Este motor no soporta múltiples hosts por usuario (no aplica 'agregar host').",
            status_code=422,
            context={"dialect": self.dialect},
        )

    def copy_user_grants(self, username: str, source_host: str, new_host: str) -> int:
        """
        Replica los permisos de ``'user'@'source_host'`` a ``'user'@'new_host'`` (mismo
        servidor/motor). Best-effort: omite el USAGE base y privilegios no portables por
        seguridad. Devuelve cuántas sentencias GRANT se aplicaron. Default: 422.
        """
        raise AppHttpException(
            message="Este motor no soporta copiar grants entre hosts de un usuario.",
            status_code=422,
            context={"dialect": self.dialect},
        )

    @abstractmethod
    def grant_database(
        self, username: str, db_name: str, host: str = "%", privileges: str = "ALL PRIVILEGES",
    ) -> None: ...

    @abstractmethod
    def revoke_database(
        self, username: str, db_name: str, host: str = "%", privileges: str = "ALL PRIVILEGES",
    ) -> None: ...

    # ---- GRANT/REVOKE GRANULAR (Plan 07) — por nivel de objeto ---------------- #
    @abstractmethod
    def grant_object(
        self,
        grantee: EngineUserInfo,
        level: GrantLevel,
        object_ref: ObjectRef,
        privileges: list[str],
        *,
        with_grant_option: bool = False,
    ) -> None:
        """Otorga ``privileges`` al ``grantee`` sobre el objeto del ``object_ref``."""

    @abstractmethod
    def revoke_object(
        self,
        grantee: EngineUserInfo,
        level: GrantLevel,
        object_ref: ObjectRef,
        privileges: list[str],
        *,
        cascade: bool = False,
    ) -> None:
        """
        Revoca ``privileges`` del ``grantee`` sobre el objeto del ``object_ref``.

        ``cascade`` solo aplica a PostgreSQL (revoca en cascada los privilegios que el
        ``grantee`` haya delegado a su vez). En MySQL/MariaDB no existe y debe
        rechazarse. Por defecto ``RESTRICT`` (no cascada).
        """

    @abstractmethod
    def list_grants(
        self, grantee: EngineUserInfo, database: str | None = None
    ) -> list[GrantInfo]:
        """
        Introspecciona los privilegios efectivos del ``grantee``. En PostgreSQL los
        grants de objeto son POR BASE DE DATOS: ``database`` es necesario para ver
        tablas/columnas/secuencias/rutinas; en MySQL/MariaDB se ignora (info_schema
        es a nivel servidor).
        """

    @abstractmethod
    def can_grant(
        self, level: GrantLevel, object_ref: ObjectRef, privileges: list[str]
    ) -> bool:
        """
        ¿La credencial del gateway (grantor) puede DELEGAR ``privileges`` sobre el
        objeto? Pre-chequeo de capability: superusuario/owner o privilegio con grant
        option. Se consulta ANTES de ejecutar el GRANT (el error del motor es la red
        secundaria).
        """

    def reassign_database_owner(
        self,
        db_name: str,
        new_owner: str,
        *,
        new_host: str = "%",
        old_owner: str | None = None,
        old_host: str = "%",
    ) -> None:
        """
        Reasigna la propiedad de una BD al usuario ``new_owner``.

        Implementación por defecto (propiedad LÓGICA vía privilegios, válida para
        MySQL/MariaDB): revoca al propietario anterior (si se indica) y otorga al
        nuevo. PostgreSQL la sobreescribe para usar OWNER nativo (ALTER DATABASE).
        La semántica de "propiedad" es específica de cada motor, por eso vive en el
        adapter y nunca en el controller.
        """
        if old_owner:
            self.revoke_database(old_owner, db_name, host=old_host)
        self.grant_database(new_owner, db_name, host=new_host)

    # ------------------------------------------------------------------ #
    # Inyección de conexión en la lectura de catálogo (módulo 10, §6.4)   #
    # ------------------------------------------------------------------ #
    @contextmanager
    def _conn_ctx(self, database: str, conn: Connection | None = None):
        """
        Conexión con la que leer el catálogo: la que da el llamador, o una nueva.

        Existe por el requisito de **punto único en el tiempo** de la exportación (§19.4):
        para que la estructura entre en el mismo snapshot que los datos hay que leerla
        DENTRO de la transacción de lectura del job, y hasta ahora cada método de
        introspección abría su propia conexión (y por tanto su propia vista de los datos).

        Dos invariantes, ambas deliberadas:

        - Con ``conn`` dado **no se cierra la conexión ni se toca su nivel de aislamiento**.
          Es del llamador: un ``execution_options(isolation_level=…)`` acá revertiría la
          transacción REPEATABLE READ que ``export_session`` acaba de abrir, y cerrarla
          dejaría al resto del job sin snapshot.
        - Con ``conn=None`` el comportamiento es EXACTAMENTE el histórico
          (``database_connection`` propia, cerrada al salir). El parámetro es aditivo:
          los cinco consumidores actuales de estos métodos (clon, schema-comparisons,
          conversión de collation, migraciones y adopción/snapshot) no cambian de firma
          efectiva ni de semántica.
        """
        if conn is not None:
            yield conn
            return
        with database_connection(self.target, database) as owned:
            yield owned

    # ------------------------------------------------------------------ #
    # Concreto: conexión e introspección (read-only, cross-dialect)       #
    # ------------------------------------------------------------------ #
    def test_connection(self) -> ConnectionInfo:
        try:
            with server_connection(self.target) as conn:
                version = conn.execute(text(self._version_sql())).scalar()
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op="test_connection", target=self.target)
        return ConnectionInfo(
            ok=True,
            dialect=self.dialect,
            server_version=str(version) if version is not None else None,
        )

    def list_tables(self, database: str, *, conn: Connection | None = None) -> list[str]:
        # Introspección de un objeto PREEXISTENTE: whitelist ampliada (nombres legados).
        # Se oculta la contabilidad interna del gateway (``_gw_v_*``/``_gw_stg_*``): no es
        # esquema del usuario y aparecer en el listado solo invita a operar sobre ella.
        # ``conn`` (§6.4): leer dentro de la transacción del llamador. Ver ``_conn_ctx``.
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with self._conn_ctx(database, conn) as conn:
                return exclude_gateway_internal_tables(
                    sorted(inspect(conn).get_table_names(schema=schema))
                )
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_tables", target=self.target, extra={"database": database}
            )

    def get_table_schema(self, database: str, table: str) -> TableSchema:
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        validate_identifier(table, self.dialect, "tabla", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with database_connection(self.target, database) as conn:
                insp = inspect(conn)
                try:
                    return self._build_table_schema(insp, conn, database, table, schema)
                except NoSuchTableError:
                    raise AppHttpException(
                        message="La tabla no existe en la base de datos indicada.",
                        status_code=404,
                        context={"database": database, "table": table},
                    )
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc,
                op="get_table_schema",
                target=self.target,
                extra={"database": database, "table": table},
            )

    # ------------------------------------------------------------------ #
    # Construcción de TableSchema extendido (compartido; usa el Inspector) #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _index_from_raw(ix: dict) -> IndexInfo:
        """Traduce un índice del Inspector a IndexInfo, sin descartar dialect_options."""
        dialect_opts = ix.get("dialect_options") or {}
        method = None
        predicate = None
        include_columns: list[str] = []
        for key, val in dialect_opts.items():
            if key.endswith("_using") and val:
                method = str(val)
            elif key.endswith("_where") and val:
                predicate = str(val)
            elif key.endswith("_include") and val:
                include_columns = list(val)
        # column_sorting: {col: ('desc', 'nulls_first')} solo cuando no es el default.
        column_sort: dict[str, list[str]] = {}
        for col, opts in (ix.get("column_sorting") or {}).items():
            if opts:
                column_sort[col] = list(opts)
        # expressions (índice funcional): SQLAlchemy las expone en 'expressions' cuando
        # alguna posición de column_names es None (es una expresión, no una columna).
        raw_names = ix.get("column_names") or []
        expressions = []
        if None in raw_names:
            expressions = [str(e) for e in (ix.get("expressions") or []) if e is not None]
        cols = [c for c in raw_names if c is not None]
        return IndexInfo(
            name=ix.get("name"),
            columns=cols,
            unique=bool(ix.get("unique")),
            method=method,
            predicate=predicate,
            expressions=expressions,
            column_sort=column_sort,
            include_columns=include_columns,
        )

    def _build_table_schema(
        self, insp, conn, database: str, table: str, schema: str
    ) -> TableSchema:
        """
        Construye un ``TableSchema`` COMPLETO reutilizando lo que el Inspector ya expone
        (columnas + computed/identity, FKs + options, índices + dialect_options, checks,
        uniques, comment) más los hooks por adapter (collation/charset/on_update de
        columna y storage_options de tabla) para lo que el Inspector no expone fiable.
        """
        columns_raw = insp.get_columns(table, schema=schema)
        pk = insp.get_pk_constraint(table, schema=schema)
        pk_cols = pk.get("constrained_columns") or []
        pk_name = pk.get("name")
        fks_raw = insp.get_foreign_keys(table, schema=schema)
        idx_raw = insp.get_indexes(table, schema=schema)
        try:
            checks_raw = insp.get_check_constraints(table, schema=schema)
        except (NotImplementedError, SQLAlchemyError):
            checks_raw = []
        try:
            uniques_raw = insp.get_unique_constraints(table, schema=schema)
        except (NotImplementedError, SQLAlchemyError):
            uniques_raw = []
        try:
            comment = (insp.get_table_comment(table, schema=schema) or {}).get("text")
        except (NotImplementedError, SQLAlchemyError):
            comment = None

        extras = self._column_extras(conn, database, table, schema)
        storage = self._table_storage_options(conn, database, table, schema)

        pk_set = set(pk_cols)
        columns: list[ColumnInfo] = []
        for c in columns_raw:
            ex = extras.get(c["name"], {})
            computed = None
            comp_raw = c.get("computed")
            if comp_raw:
                computed = ComputedInfo(
                    # Preferir la expresión canónica de ``information_schema`` (hook por
                    # adapter, p.ej. MySQL ``GENERATION_EXPRESSION``) sobre la reflexión de
                    # ``SHOW CREATE TABLE``, que se corrompe cuando el COMMENT de una columna
                    # generada contiene paréntesis. Motores sin el hook (p.ej. PG, que
                    # refleja con ``pg_get_expr`` y no sufre el bug) caen al ``sqltext`` de
                    # la reflexión → comportamiento intacto.
                    sqltext=ex.get("generation_expression")
                    or str(comp_raw.get("sqltext") or ""),
                    persisted=bool(comp_raw.get("persisted")),
                )
            identity = None
            id_raw = c.get("identity")
            if id_raw:
                identity = IdentityInfo(
                    always=bool(id_raw.get("always")),
                    start=id_raw.get("start"),
                    increment=id_raw.get("increment"),
                )
            columns.append(
                ColumnInfo(
                    # ``column_type`` (extra por-adapter, p.ej. MySQL ``COLUMN_TYPE``) es el
                    # tipo CANÓNICO cuando el hook lo provee; ``str(reflected_type)`` pierde
                    # detalle en MySQL (ENUM/SET sin valores, UNSIGNED, display width).
                    name=c["name"],
                    type=ex.get("column_type") or str(c["type"]),
                    nullable=bool(c.get("nullable", True)),
                    default=None if c.get("default") is None else str(c.get("default")),
                    primary_key=c["name"] in pk_set,
                    autoincrement=c.get("autoincrement") in (True, "auto"),
                    # ``comment`` del hook (MySQL ``COLUMN_COMMENT``) SOLO para columnas
                    # GENERADAS: es el único caso donde la reflexión de ``SHOW CREATE`` se
                    # traga el comentario junto con la expresión. Las columnas normales
                    # conservan el valor reflejado → cero cambio de comportamiento (y cero
                    # ruido en el diff) en las tablas que hoy funcionan bien.
                    comment=(ex.get("comment") if computed is not None else None)
                    or c.get("comment"),
                    collation=ex.get("collation"),
                    charset=ex.get("charset"),
                    computed=computed,
                    identity=identity,
                    on_update=ex.get("on_update"),
                )
            )
        foreign_keys = [
            ForeignKeyInfo(
                name=fk.get("name"),
                columns=fk.get("constrained_columns") or [],
                referred_table=fk.get("referred_table") or "",
                referred_columns=fk.get("referred_columns") or [],
                referred_schema=fk.get("referred_schema"),
                on_delete=(fk.get("options") or {}).get("ondelete"),
                on_update=(fk.get("options") or {}).get("onupdate"),
                deferrable=(fk.get("options") or {}).get("deferrable"),
                initially=(fk.get("options") or {}).get("initially"),
            )
            for fk in fks_raw
        ]
        indexes = [self._index_from_raw(ix) for ix in idx_raw]
        check_constraints = [
            CheckConstraintInfo(name=ck.get("name"), sqltext=str(ck.get("sqltext") or ""))
            for ck in checks_raw
            if ck.get("sqltext")
        ]
        unique_constraints = [
            UniqueConstraintInfo(
                name=uc.get("name"), columns=uc.get("column_names") or []
            )
            for uc in uniques_raw
        ]
        return TableSchema(
            database=database,
            table=table,
            columns=columns,
            primary_key=list(pk_cols),
            primary_key_name=pk_name,
            foreign_keys=foreign_keys,
            indexes=indexes,
            check_constraints=check_constraints,
            unique_constraints=unique_constraints,
            comment=comment,
            storage_options=storage,
        )

    # ------------------------------------------------------------------ #
    # Exportación (módulo 10) — capacidades por motor                     #
    # ------------------------------------------------------------------ #
    def export_supported_types(self) -> frozenset[str]:
        """
        Tipos de objeto que ESTE motor puede exportar (§7 del diseño de exportación).

        Vive en el adapter y no en el controller por el criterio de aceptación
        arquitectónico del módulo: agregar un cuarto motor debe ser "implementar el
        adapter y nada más". Una tabla motor→tipos en la capa de orquestación obligaría a
        tocar dos lugares y a mantenerlos sincronizados a mano.

        El default es el núcleo común a los tres motores soportados. Cada adapter agrega lo
        suyo: eventos en la familia MySQL; vistas materializadas, secuencias autónomas,
        tipos ENUM y extensiones en PostgreSQL.
        """
        return frozenset({"table", "view", "routine", "trigger"})

    # ---- Emisión del artefacto: toda la diferencia de dialecto (§7) ------ #
    # CRITERIO DE ACEPTACIÓN ARQUITECTÓNICO del módulo: ``export_writer`` no lleva ni un
    # ``if engine ==``. Todo lo que cambia entre motores entra y sale por estos métodos, así
    # que agregar un cuarto motor es implementar el adapter y nada más.

    # Palabra clave del ``DROP`` por tipo de objeto. Los tipos que no aparecen (rutinas) los
    # resuelve ``_export_drop_keyword``, que necesita mirar el payload.
    _EXPORT_DROP_KEYWORDS: dict[str, str] = {
        "table": "TABLE",
        "view": "VIEW",
        "materialized_view": "MATERIALIZED VIEW",
        "sequence": "SEQUENCE",
        "enum_type": "TYPE",
        "extension": "EXTENSION",
        "event": "EVENT",
        "trigger": "TRIGGER",
        "routine": "FUNCTION",
    }

    # ¿El motor admite ``CASCADE`` en el DROP? Solo PostgreSQL. MySQL/MariaDB ACEPTAN la
    # palabra en ``DROP TABLE`` pero la IGNORAN, y emitirla sugeriría al operador una
    # garantía de arrastre de dependencias que no existe.
    _EXPORT_SUPPORTS_CASCADE: bool = False

    # Familias de tipo cuyo ``ORDER BY`` no da un orden fiable, para el desempate por tupla
    # completa de columnas cuando la tabla NO tiene PK (§8.3). No es solo "el motor lo
    # rechaza": en MySQL ordenar por un TEXT/BLOB trunca a ``max_sort_length`` (1024 por
    # defecto), así que dos filas que difieren más allá de ese prefijo quedan empatadas y el
    # orden vuelve a ser arbitrario. Fail-closed: ante la duda, ``deterministic=False``.
    _EXPORT_UNORDERABLE_TYPE_TOKENS: tuple[str, ...] = (
        "blob", "text", "json", "xml", "clob", "geometry", "bytea", "tsvector", "tsquery",
    )

    def export_scope_ddl(
        self,
        database: str,
        mode: str,
        *,
        charset: str | None = None,
        collation: str | None = None,
        if_exists: bool = True,
    ) -> list[str]:
        """
        DDL del CONTENEDOR (la base de datos / el esquema) según ``ScopeDdl``.

        ``mode`` llega como el valor del enumerado (``NONE``/``CREATE``/``DROP_CREATE``/
        ``CREATE_IF_NOT_EXISTS``) y no como el enumerado en sí: el adapter no depende del
        módulo del spec.
        """
        raise NotImplementedError(f"{self.dialect}: export_scope_ddl")

    def export_entity_drop(
        self,
        object_type: str,
        name: str,
        *,
        payload=None,
        if_exists: bool = True,
        cascade: bool = False,
    ) -> str:
        """
        ``DROP`` de UN objeto para el artefacto. ``payload`` es su DTO del snapshot cuando
        el motor necesita más que el nombre (el tipo de rutina, la tabla de un trigger).

        ``if_exists`` no es cosmético: un script que aborta al intentar eliminar algo que no
        existe no sirve en la práctica, que es justamente el caso de uso de ``DROP_CREATE``
        contra un destino vacío.
        """
        keyword = self._export_drop_keyword(object_type, payload)
        parts = ["DROP", keyword]
        if if_exists:
            parts.append("IF EXISTS")
        parts.append(self._q(name, object_type))
        parts.extend(self._export_drop_suffix(object_type, payload))
        if cascade and self._EXPORT_SUPPORTS_CASCADE:
            parts.append("CASCADE")
        return " ".join(parts)

    def _export_drop_keyword(self, object_type: str, payload) -> str:
        keyword = self._EXPORT_DROP_KEYWORDS.get(object_type)
        if keyword is None:
            raise AppHttpException(
                message="Tipo de objeto no exportable para este motor.",
                status_code=422,
                context={"object_type": object_type, "dialect": self.dialect},
            )
        if object_type == "routine" and payload is not None:
            return "PROCEDURE" if str(payload.kind).upper() == "PROCEDURE" else "FUNCTION"
        return keyword

    def _export_drop_suffix(self, object_type: str, payload) -> list[str]:
        """Cola del DROP que depende del motor (PostgreSQL: ``DROP TRIGGER … ON tabla``)."""
        return []

    def export_session_preamble(
        self,
        *,
        charset: str | None = None,
        collation: str | None = None,
        suspend_constraints: bool = True,
    ) -> list[str]:
        """
        Sentencias de preparación de la sesión que EJECUTA el artefacto.

        Todo lo que se cambie acá tiene que restaurarse en ``export_session_epilogue``: un
        script que deja la sesión con ``FOREIGN_KEY_CHECKS=0`` es un fallo grave (§8.4).
        """
        return []

    def export_session_epilogue(self) -> list[str]:
        """RESTAURA exactamente lo que tocó ``export_session_preamble``."""
        return []

    def export_use_scope(self, database: str) -> str | None:
        """Fija el contexto: ``USE db`` en MySQL, ``SET search_path`` en PostgreSQL."""
        return None

    def export_counter_reset(
        self, table: str, value: int | None, *, column: str | None = None
    ) -> str | None:
        """
        Ajuste del contador de autoincremento al final del artefacto.

        ``column`` es la columna que lo lleva: MySQL/MariaDB no la necesitan (el contador es
        una opción de la TABLA) pero PostgreSQL sí (vive en una secuencia asociada a la
        columna). Se pasa siempre y cada motor usa lo que le sirve.

        ``None`` (sin valor conocido) = no se emite nada. Nunca se inventa un valor: un
        contador equivocado es peor que ninguno — deja la tabla generando ids que ya
        existen.
        """
        return None

    def export_counter_value_sql(
        self, database: str, table: str, column: str
    ) -> tuple[str, dict] | None:
        """
        Consulta que LEE el contador de autoincremento actual, o ``None`` si no aplica.

        Es la contraparte de ``export_counter_reset`` y vive en el MISMO adapter a propósito:
        el valor que hay que leer depende de la semántica del ``reset`` de cada motor y las
        dos mitades no pueden divergir. En MySQL/MariaDB ``AUTO_INCREMENT = n`` fija el
        PRÓXIMO id, así que se lee el próximo; en PostgreSQL ``setval(seq, n, true)`` fija el
        ÚLTIMO usado, así que se lee el último. Leer "el máximo de la columna" en ambos —lo
        obvio— dejaría a MySQL generando un id que ya existe.

        Devuelve ``(sql, params)`` para ejecutar con parámetros ligados: los nombres viajan
        como VALORES a ``information_schema``/``pg_get_serial_sequence``, nunca interpolados.
        El default ``None`` es fail-closed: un motor que no lo implemente no emite ningún
        ajuste de contador, en vez de heredar la consulta de otro y devolver un número que
        no significa lo mismo.
        """
        return None

    # Tipos cuyo ``CREATE`` admite ``IF NOT EXISTS`` en ESTE motor, y tipos cuyo renderer YA
    # emite una forma idempotente (``CREATE OR REPLACE``). Todo lo que no esté en ninguno de
    # los dos conjuntos NO se puede hacer idempotente: se emite el CREATE normal y el writer
    # lo reporta. Fail-closed a propósito — varias de estas cláusulas dependen de la VERSIÓN
    # del motor (``CREATE PROCEDURE IF NOT EXISTS`` es de MySQL 8.0.29+), y el gateway no
    # conoce la versión del destino donde se va a ejecutar el artefacto, que ni siquiera
    # tiene por qué ser el servidor de origen.
    _EXPORT_IF_NOT_EXISTS_TYPES: frozenset[str] = frozenset()
    _EXPORT_ALREADY_IDEMPOTENT_TYPES: frozenset[str] = frozenset()

    # ``CREATE [OR REPLACE] [MATERIALIZED|UNIQUE|…] <OBJETO>`` — la cabecera tras la que va
    # el ``IF NOT EXISTS``.
    _EXPORT_CREATE_HEAD_RE = re.compile(
        r"\s*CREATE\s+(?:OR\s+REPLACE\s+)?"
        r"(?:TEMPORARY\s+|TEMP\s+|UNLOGGED\s+|MATERIALIZED\s+|UNIQUE\s+|FULLTEXT\s+"
        r"|SPATIAL\s+|DEFINER\s*=\s*\S+\s+)*"
        r"(?:TABLE|VIEW|SEQUENCE|INDEX|EXTENSION|TRIGGER|EVENT|TYPE|SCHEMA|DATABASE)\s+",
        re.IGNORECASE,
    )
    _EXPORT_IF_NOT_EXISTS_RE = re.compile(r"\bIF\s+NOT\s+EXISTS\b", re.IGNORECASE)

    def export_make_idempotent(self, sql: str, object_type: str) -> str | None:
        """
        Convierte un ``CREATE`` en su forma idempotente, o ``None`` si no es expresable.

        ``None`` NO es un error: es la respuesta honesta para un tipo cuyo motor no tiene
        ``IF NOT EXISTS`` (un ``CREATE TYPE`` de PostgreSQL, una rutina de MySQL). El writer
        emite entonces el ``CREATE`` normal y lo declara en los avisos, en vez de inventar
        una sintaxis que hace fallar el script justo cuando el usuario pidió que no fallara.
        """
        if object_type in self._EXPORT_ALREADY_IDEMPOTENT_TYPES:
            return sql
        if object_type not in self._EXPORT_IF_NOT_EXISTS_TYPES:
            return None
        if self._EXPORT_IF_NOT_EXISTS_RE.search(sql):
            return sql
        match = self._EXPORT_CREATE_HEAD_RE.match(sql)
        if match is None:
            return None
        head = match.group(0)
        return f"{head}IF NOT EXISTS {sql[len(head):]}"

    def export_body_wrapper(self, object_type: str) -> tuple[str, str] | None:
        """
        ``(prefijo, sufijo)`` para envolver un cuerpo procedural, o ``None``.

        Delega en ``sql_dialect.body_delimiter_wrapper``, que es la fuente ÚNICA del
        criterio (la comparte con la descarga de schema-comparisons).
        """
        return body_delimiter_wrapper(object_type, self.dialect)

    def export_definer_clause(self, sql: str, *, mode: str, value: str | None) -> str:
        """
        Aplica ``sanitize.definer`` sobre el DDL de un objeto con cuerpo.

        Default no-op: PostgreSQL no tiene cláusula ``DEFINER`` (la propiedad del objeto y
        ``SECURITY DEFINER`` son mecanismos distintos), y la matriz ya rechaza ahí
        ``omit``/``replace``.
        """
        return sql

    def export_insert_wrapper(
        self,
        table: str,
        columns: Sequence[str],
        *,
        variant: str = "insert",
        primary_key: Sequence[str] = (),
    ) -> tuple[str, str]:
        """
        ``(prefijo, sufijo)`` de una sentencia de datos: ``prefijo`` + tuplas + ``sufijo``.

        ``columns`` vacío = sin lista de columnas (``data.include_column_list=false``).

        El default cubre solo ``insert``, que es el único portable. Las demás variantes son
        sintaxis PROPIETARIA de cada motor (``INSERT IGNORE``/``REPLACE`` de MySQL,
        ``ON CONFLICT`` de PostgreSQL) y las implementa cada adapter; un motor que no tenga
        equivalente devuelve un 422 accionable en vez de emitir un artefacto que su destino
        rechaza.
        """
        prefix = f"INSERT INTO {self._q(table, 'tabla')}{self._export_column_list(columns)} VALUES"
        if variant in ("insert", "none"):
            return prefix, ""
        raise self._export_unsupported_variant(variant)

    def _export_column_list(self, columns: Sequence[str]) -> str:
        if not columns:
            return ""
        return " (" + ", ".join(self._q(c, "columna") for c in columns) + ")"

    def _export_unsupported_variant(self, variant: str) -> AppHttpException:
        return AppHttpException(
            message=(
                f"El motor {self.dialect} no admite la variante de INSERT '{variant}'."
            ),
            status_code=422,
            public_context={
                "code": "export.incompatible_option",
                "field": "data.insert_variant",
                "engine": self.dialect,
            },
        )

    def export_row_order_by(self, table: TableSchema) -> list[str]:
        """
        Columnas del ``ORDER BY`` que hace determinista el volcado de una tabla (§8.3).

        1. la PK, que es el caso normal;
        2. sin PK, la tupla COMPLETA de columnas si todas son ordenables de forma fiable;
        3. si no, lista vacía ⇒ el objeto sale **sin orden garantizado**, se marca
           ``deterministic: false`` y se avisa. Fingir determinismo ahí sería mentir sobre
           la comparabilidad de dos volcados, que es justamente para lo que existe §8.3.

        Las columnas GENERADAS se excluyen: no viajan en el ``INSERT`` y ordenar por una
        expresión calculada no aporta nada que la tupla de columnas reales no dé ya.
        """
        if table.primary_key:
            return list(table.primary_key)
        plain = [c for c in table.columns if c.computed is None]
        if not plain:
            return []
        if any(not self._export_type_is_orderable(c.type) for c in plain):
            return []
        return [c.name for c in plain]

    def _export_type_is_orderable(self, col_type: str) -> bool:
        lowered = (col_type or "").lower()
        return not any(tok in lowered for tok in self._EXPORT_UNORDERABLE_TYPE_TOKENS)

    # ------------------------------------------------------------------ #
    # Snapshot estructural CANÓNICO (Plan diff) — SchemaSnapshot           #
    # ------------------------------------------------------------------ #
    def structural_snapshot(
        self, database: str, *, conn: Connection | None = None
    ) -> SchemaSnapshot:
        """
        Snapshot estructural canónico y COMPLETO de la BD (entrada del motor de diff).

        Reutiliza el Inspector para tablas y los hooks por adapter para vistas/rutinas/
        triggers/secuencias/tipos/extensiones/events. Solo estructura, jamás filas.
        PostgreSQL cubre solo el schema ``public`` (limitación conocida del sistema).

        ``conn`` (§6.4 del módulo de exportación): lee con la conexión del llamador en vez
        de abrir una propia, para que el catálogo entre en su transacción de lectura. En
        PostgreSQL eso da estructura y datos del MISMO instante; en MySQL/MariaDB el
        diccionario de datos no participa del snapshot MVCC y la garantía sigue siendo solo
        para datos (§6.2 — se reporta, no se tapa). ``None`` = comportamiento histórico.
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with self._conn_ctx(database, conn) as conn:
                insp = inspect(conn)
                # Se EXCLUYE la contabilidad interna del gateway (``_gw_v_*`` /
                # ``_gw_stg_*``): no es esquema del usuario. Ver el porqué completo en
                # ``identifiers.GATEWAY_TABLE_PREFIXES`` — sin este filtro el diff
                # generaba ``DROP TABLE _gw_v_{slug}`` contra la propia tabla de versión
                # de Alembic y la migración moría al registrar la versión nueva.
                tables = [
                    self._build_table_schema(insp, conn, database, t, schema)
                    for t in exclude_gateway_internal_tables(
                        sorted(insp.get_table_names(schema=schema))
                    )
                ]
                views = self._snapshot_views(conn, database, schema)
                routines = self._snapshot_routines(conn, database, schema)
                triggers = self._snapshot_triggers(conn, database, schema)
                sequences = self._snapshot_sequences(conn, database, schema)
                enum_types = self._snapshot_enum_types(conn, database, schema)
                extensions = self._snapshot_extensions(conn, database, schema)
                events = self._snapshot_events(conn, database, schema)
                db_defaults = self._database_defaults(conn, database, schema)
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="structural_snapshot", target=self.target,
                extra={"database": database},
            )
        return SchemaSnapshot(
            database=database,
            source_engine=self.dialect,
            db_charset=db_defaults.get("db_charset"),
            db_collation=db_defaults.get("db_collation"),
            tables=tables,
            views=views,
            routines=routines,
            triggers=triggers,
            sequences=sequences,
            enum_types=enum_types,
            extensions=extensions,
            events=events,
        )

    # ---- Hooks del snapshot (default vacío; cada adapter sobreescribe) --- #
    def _column_extras(self, conn, database: str, table: str, schema: str) -> dict[str, dict]:
        """{col: {collation, charset, on_update}} — lo que el Inspector no expone fiable."""
        return {}

    def _database_defaults(self, conn, database: str, schema: str) -> dict[str, str | None]:
        """
        Default de charset/collation A NIVEL BD → ``{"db_charset", "db_collation"}``.

        Default vacío (ningún dato) para que el clon caiga al default del motor. Cada adapter
        que sepa determinarlo lo sobreescribe. NO dispara diff — solo alimenta el
        ``CREATE DATABASE`` del clon para que el destino herede el mismo default que la origen.
        """
        return {}

    def _table_storage_options(self, conn, database: str, table: str, schema: str) -> dict[str, str]:
        """engine/charset/collation por tabla + db_charset/db_collation (herencia)."""
        return {}

    def _snapshot_views(self, conn, database: str, schema: str) -> list[ViewInfo]:
        return []

    def _snapshot_routines(self, conn, database: str, schema: str) -> list[RoutineInfo]:
        return []

    def _snapshot_triggers(self, conn, database: str, schema: str) -> list[TriggerInfo]:
        return []

    def _snapshot_sequences(self, conn, database: str, schema: str) -> list[SequenceInfo]:
        return []

    def _snapshot_enum_types(self, conn, database: str, schema: str) -> list[EnumTypeInfo]:
        return []

    def _snapshot_extensions(self, conn, database: str, schema: str) -> list[ExtensionInfo]:
        return []

    def _snapshot_events(self, conn, database: str, schema: str) -> list[EventInfo]:
        return []

    # ------------------------------------------------------------------ #
    # Generación de DDL desde un SchemaDiff (Plan diff — Fase 3)           #
    # ------------------------------------------------------------------ #
    # El diff YA viene ordenado por fase (1..9). Cada RenderedStatement lleva los
    # flags de riesgo calculados por el motor de diff (Fase 2). Los identificadores
    # derivados del motor origen se revalidan (validate_identifier, allow_existing) y
    # se re-emiten con quote_identifier: nunca se interpola texto crudo.
    def render_diff(self, diff: SchemaDiff) -> list[RenderedStatement]:
        """Traduce un ``SchemaDiff`` a sentencias DDL para el motor de este adapter."""
        out: list[RenderedStatement] = []
        for item in diff.items:
            out.extend(self._render_item(item))
        return out

    def _q(self, name: str, kind: str = "objeto") -> str:
        return quote_identifier(
            validate_identifier(name, self.dialect, kind, allow_existing=True), self.dialect
        )

    def _stmt(
        self, item: DiffItem, sql: str, *, down_sql: str | None = None,
        down_confirmed: bool = False,
    ) -> RenderedStatement:
        return RenderedStatement(
            sql=sql,
            object_type=item.object_type,
            object_name=item.object_name,
            change_type=item.change_type,
            phase=item.phase,
            risk=item.risk,
            down_sql=down_sql,
            down_confirmed=down_confirmed,
            op_group=item.op_key(),
            depends_on=list(item.depends_on),
        )

    def _stmts(
        self,
        item: DiffItem,
        forward: list[str],
        reverse: list[str] | None = None,
        *,
        down_confirmed: bool = False,
    ) -> list[RenderedStatement]:
        """
        Renderiza un ítem que produce VARIAS sentencias, con su reverso COMPLETO.

        El reverso se adjunta a la ÚLTIMA sentencia del grupo (las demás quedan con
        ``down_sql=None``) porque el ``down_sql`` de una versión se ensambla recorriendo
        las sentencias en ORDEN INVERSO: así el reverso del ítem se ejecuta UNA sola vez,
        completo y en el lugar correcto respecto de los otros ítems.

        Esto corrige un fallo real de rollback: un índice/UNIQUE/CHECK/FK REDEFINIDO se
        renderiza como ``DROP viejo`` + ``CREATE nuevo``, y antes solo la segunda
        sentencia llevaba reverso (``CREATE viejo``) — sin borrar primero el nuevo. Al
        revertir, el objeto nuevo seguía existiendo con el MISMO nombre (el emparejamiento
        ``pair_by_name`` de ``_diff_collection`` es justamente por nombre) y el motor
        respondía ``(1061, "Duplicate key name")`` / ``42P07 relation already exists``.
        El reverso correcto del par es ``DROP nuevo`` + ``CREATE viejo``.
        """
        if not forward:
            return []
        out = [self._stmt(item, s) for s in forward[:-1]]
        rev_sql = ";\n".join(s for s in (reverse or []) if s) or None
        out.append(
            self._stmt(
                item, forward[-1], down_sql=rev_sql,
                down_confirmed=bool(rev_sql) and down_confirmed,
            )
        )
        return out

    def _render_item(self, item: DiffItem) -> list[RenderedStatement]:
        ot, ct = item.object_type, item.change_type
        handler = {
            ("table", "new"): self._ri_table_new,
            ("table", "dropped"): self._ri_table_dropped,
            ("column", "new"): self._ri_column_new,
            ("column", "dropped"): self._ri_column_dropped,
            ("column", "modified"): self._ri_column_modified,
            ("primary_key", "new"): self._ri_pk_changed,
            ("primary_key", "modified"): self._ri_pk_changed,
            ("primary_key", "dropped"): self._ri_pk_changed,
            ("foreign_key", "new"): self._ri_fk_new,
            ("foreign_key", "modified"): self._ri_fk_modified,
            ("foreign_key", "dropped"): self._ri_fk_dropped,
            ("unique_constraint", "new"): self._ri_unique_new,
            ("unique_constraint", "modified"): self._ri_unique_modified,
            ("unique_constraint", "dropped"): self._ri_unique_dropped,
            ("check_constraint", "new"): self._ri_check_new,
            ("check_constraint", "modified"): self._ri_check_modified,
            ("check_constraint", "dropped"): self._ri_check_dropped,
            ("index", "new"): self._ri_index_new,
            ("index", "modified"): self._ri_index_modified,
            ("index", "dropped"): self._ri_index_dropped,
            ("view", "new"): self._ri_view_upsert,
            ("view", "modified"): self._ri_view_upsert,
            ("view", "dropped"): self._ri_view_dropped,
            ("materialized_view", "new"): self._ri_view_upsert,
            ("materialized_view", "modified"): self._ri_view_upsert,
            ("materialized_view", "dropped"): self._ri_view_dropped,
            ("routine", "new"): self._ri_routine_upsert,
            ("routine", "modified"): self._ri_routine_upsert,
            ("routine", "dropped"): self._ri_routine_dropped,
            ("trigger", "new"): self._ri_trigger_upsert,
            ("trigger", "modified"): self._ri_trigger_upsert,
            ("trigger", "dropped"): self._ri_trigger_dropped,
            ("event", "new"): self._ri_event_upsert,
            ("event", "modified"): self._ri_event_upsert,
            ("event", "dropped"): self._ri_event_dropped,
            ("sequence", "new"): self._ri_sequence_new,
            ("sequence", "modified"): self._ri_sequence_modified,
            ("sequence", "dropped"): self._ri_sequence_dropped,
            ("enum_type", "new"): self._ri_enum_new,
            ("enum_type", "modified"): self._ri_enum_modified,
            ("enum_type", "dropped"): self._ri_enum_dropped,
            ("extension", "new"): self._ri_extension_new,
            ("extension", "dropped"): self._ri_extension_dropped,
        }.get((ot, ct))
        if handler is None:
            return []  # tipo/cambio no soportado en v1: se omite (nunca se inventa DDL)
        return handler(item)

    # ---- Portables (mismo SQL en ambos motores, solo cambia el quoting) ---- #
    def _ri_table_new(self, item: DiffItem) -> list[RenderedStatement]:
        tbl = item.source_payload
        sql = self._render_create_table(tbl)
        return [self._stmt(item, sql, down_sql=f"DROP TABLE {self._q(tbl.table, 'tabla')}",
                           down_confirmed=True)]

    def _ri_table_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        tbl = item.target_payload
        # Reverso SUGERIDO: recrea la ESTRUCTURA. Los datos ya se perdieron -> nunca
        # confirmado (un rollback deja la tabla vacía, no como estaba).
        reverse = [self._render_create_table(tbl)] if tbl is not None else []
        return self._stmts(
            item, [f"DROP TABLE {self._q(item.object_name, 'tabla')}"], reverse,
            down_confirmed=False,
        )

    def _ri_column_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, col = item.parent_table, item.source_payload
        coldef = self._render_column_def(col)
        sql = f"ALTER TABLE {self._q(table, 'tabla')} ADD COLUMN {coldef}"
        down = f"ALTER TABLE {self._q(table, 'tabla')} DROP COLUMN {self._q(col.name, 'columna')}"
        return [self._stmt(item, sql, down_sql=down, down_confirmed=True)]

    def _ri_column_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        table, col = item.parent_table, item.target_payload
        sql = f"ALTER TABLE {self._q(table, 'tabla')} DROP COLUMN {self._q(col.name, 'columna')}"
        # Reverso SUGERIDO (no confirmado): recrea la columna, pero los datos ya se perdieron.
        down = f"ALTER TABLE {self._q(table, 'tabla')} ADD COLUMN {self._render_column_def(col)}"
        return [self._stmt(item, sql, down_sql=down, down_confirmed=False)]

    def _ri_fk_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, fk = item.parent_table, item.source_payload
        return [self._stmt(item, self._render_add_fk(table, fk),
                           down_sql=self._render_drop_fk(table, fk), down_confirmed=True)]

    def _ri_fk_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        return self._stmts(
            item,
            [
                self._render_drop_fk(table, item.target_payload),
                self._render_add_fk(table, item.source_payload),
            ],
            [
                self._render_drop_fk(table, item.source_payload),
                self._render_add_fk(table, item.target_payload),
            ],
            # Re-crear la FK vieja VALIDA los datos actuales: puede fallar -> sugerido.
            down_confirmed=False,
        )

    def _ri_fk_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        table, fk = item.parent_table, item.target_payload
        return [self._stmt(item, self._render_drop_fk(table, fk),
                           down_sql=self._render_add_fk(table, fk), down_confirmed=False)]

    def _render_add_unique(self, table: str, uc) -> str:
        cols = ", ".join(self._q(c, "columna") for c in uc.columns)
        name = self._q(uc.name, "constraint") if uc.name else None
        clause = f"ADD CONSTRAINT {name} UNIQUE" if name else "ADD UNIQUE"
        return f"ALTER TABLE {self._q(table, 'tabla')} {clause} ({cols})"

    def _ri_unique_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, uc = item.parent_table, item.source_payload
        return [self._stmt(item, self._render_add_unique(table, uc),
                           down_sql=self._render_drop_unique(table, uc), down_confirmed=True)]

    def _ri_unique_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        return self._stmts(
            item,
            [
                self._render_drop_unique(table, item.target_payload),
                self._render_add_unique(table, item.source_payload),
            ],
            [
                self._render_drop_unique(table, item.source_payload),
                self._render_add_unique(table, item.target_payload),
            ],
            down_confirmed=False,  # re-crear la UNIQUE valida los datos actuales
        )

    def _ri_unique_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        table, uc = item.parent_table, item.target_payload
        return self._stmts(
            item, [self._render_drop_unique(table, uc)],
            [self._render_add_unique(table, uc)], down_confirmed=False,
        )

    def _render_add_check(self, table: str, ck) -> str:
        name = self._q(ck.name, "constraint") if ck.name else None
        clause = f"ADD CONSTRAINT {name} CHECK" if name else "ADD CHECK"
        return f"ALTER TABLE {self._q(table, 'tabla')} {clause} ({ck.sqltext})"

    def _ri_check_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, ck = item.parent_table, item.source_payload
        return [self._stmt(item, self._render_add_check(table, ck),
                           down_sql=self._render_drop_check(table, ck), down_confirmed=True)]

    def _ri_check_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        return self._stmts(
            item,
            [
                self._render_drop_check(table, item.target_payload),
                self._render_add_check(table, item.source_payload),
            ],
            [
                self._render_drop_check(table, item.source_payload),
                self._render_add_check(table, item.target_payload),
            ],
            down_confirmed=False,  # re-crear el CHECK valida los datos actuales
        )

    def _ri_check_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        table, ck = item.parent_table, item.target_payload
        return self._stmts(
            item, [self._render_drop_check(table, ck)],
            [self._render_add_check(table, ck)], down_confirmed=False,
        )

    def _ri_index_new(self, item: DiffItem) -> list[RenderedStatement]:
        table, ix = item.parent_table, item.source_payload
        return [self._stmt(item, self._render_create_index(table, ix),
                           down_sql=self._render_drop_index(table, ix), down_confirmed=True)]

    def _ri_index_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        old, new = item.target_payload, item.source_payload
        # Un índice NO único no valida datos al crearse -> el reverso es exacto y seguro.
        confirmed = not (bool(getattr(old, "unique", False)) or bool(getattr(new, "unique", False)))
        return self._stmts(
            item,
            [self._render_drop_index(table, old), self._render_create_index(table, new)],
            [self._render_drop_index(table, new), self._render_create_index(table, old)],
            down_confirmed=confirmed,
        )

    def _ri_index_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        table, ix = item.parent_table, item.target_payload
        return [self._stmt(item, self._render_drop_index(table, ix),
                           down_sql=self._render_create_index(table, ix), down_confirmed=False)]

    def _ri_column_modified(self, item: DiffItem) -> list[RenderedStatement]:
        table = item.parent_table
        return self._stmts(
            item,
            self._render_modify_column(table, item.source_payload, item.target_payload,
                                       item.changed_attributes),
            # El reverso es la MISMA operación con los payloads invertidos. Antes se
            # adjuntaba a CADA sentencia del grupo, así que un cambio que rendereaba N
            # sentencias (PostgreSQL) revertía N veces lo mismo.
            self._render_modify_column(table, item.target_payload, item.source_payload,
                                       item.changed_attributes),
            down_confirmed=False,  # una conversión de tipo puede no ser reversible
        )

    def _ri_pk_changed(self, item: DiffItem) -> list[RenderedStatement]:
        # Cubre new/modified/dropped: _render_alter_pk decide DROP/ADD/ambos según payloads.
        # El reverso es la misma llamada con las tablas invertidas (restaura la PK previa).
        table = item.parent_table
        return self._stmts(
            item,
            self._render_alter_pk(table, item.source_payload, item.target_payload),
            self._render_alter_pk(table, item.target_payload, item.source_payload),
            down_confirmed=False,  # ADD PRIMARY KEY valida unicidad/NOT NULL de los datos
        )

    def _ri_view_upsert(self, item: DiffItem) -> list[RenderedStatement]:
        replace = item.change_type == "modified"
        view = item.source_payload
        if replace and item.target_payload is not None:
            reverse = self._render_view(item.target_payload, True)  # restaura la anterior
        else:
            reverse = [self._render_drop_view(view)]
        # Una vista es pura definición: el reverso es exacto. Una MATVIEW guarda datos
        # derivados -> recrearla los recalcula/pierde: nunca confirmado.
        return self._stmts(
            item, self._render_view(view, replace), reverse,
            down_confirmed=not bool(getattr(view, "is_materialized", False)),
        )

    def _ri_view_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        view = item.target_payload
        return self._stmts(
            item, [self._render_drop_view(view)], self._render_view(view, False),
            down_confirmed=not bool(getattr(view, "is_materialized", False)),
        )

    def _ri_routine_upsert(self, item: DiffItem) -> list[RenderedStatement]:
        replace = item.change_type == "modified"
        routine = item.source_payload
        if replace and item.target_payload is not None:
            reverse = self._render_routine(item.target_payload, True)
        else:
            reverse = [self._render_drop_routine(routine)]
        return self._stmts(
            item, self._render_routine(routine, replace), reverse, down_confirmed=True,
        )

    def _ri_routine_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        routine = item.target_payload
        return self._stmts(
            item, [self._render_drop_routine(routine)],
            self._render_routine(routine, False), down_confirmed=True,
        )

    def _ri_trigger_upsert(self, item: DiffItem) -> list[RenderedStatement]:
        replace = item.change_type == "modified"
        trigger = item.source_payload
        if replace and item.target_payload is not None:
            reverse = self._render_trigger(item.target_payload, True)
        else:
            reverse = [self._render_drop_trigger(trigger)]
        return self._stmts(
            item, self._render_trigger(trigger, replace), reverse, down_confirmed=True,
        )

    def _ri_trigger_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        trigger = item.target_payload
        return self._stmts(
            item, [self._render_drop_trigger(trigger)],
            self._render_trigger(trigger, False), down_confirmed=True,
        )

    def _ri_event_upsert(self, item: DiffItem) -> list[RenderedStatement]:
        replace = item.change_type == "modified"
        event = item.source_payload
        if replace and item.target_payload is not None:
            reverse = self._render_event(item.target_payload, True)
        else:
            reverse = [self._render_drop_event(event)]
        return self._stmts(
            item, self._render_event(event, replace), reverse, down_confirmed=True,
        )

    def _ri_event_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        event = item.target_payload
        return self._stmts(
            item, [self._render_drop_event(event)],
            self._render_event(event, False), down_confirmed=True,
        )

    def _ri_sequence_new(self, item: DiffItem) -> list[RenderedStatement]:
        return self._stmts(
            item, self._render_sequence(item.source_payload, alter=False),
            [self._render_drop_sequence(item.source_payload)], down_confirmed=True,
        )

    def _ri_sequence_modified(self, item: DiffItem) -> list[RenderedStatement]:
        return self._stmts(
            item, self._render_sequence(item.source_payload, alter=True),
            self._render_sequence(item.target_payload, alter=True), down_confirmed=True,
        )

    def _ri_sequence_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        # Recrear la secuencia NO restaura su valor actual (last_value es estado, y el
        # snapshot lo excluye a propósito) -> sugerido, nunca confirmado.
        return self._stmts(
            item, [self._render_drop_sequence(item.target_payload)],
            self._render_sequence(item.target_payload, alter=False), down_confirmed=False,
        )

    def _ri_enum_new(self, item: DiffItem) -> list[RenderedStatement]:
        return self._stmts(
            item, self._render_enum(item.source_payload, item.target_payload),
            [self._render_drop_enum(item.source_payload)], down_confirmed=True,
        )

    def _ri_enum_modified(self, item: DiffItem) -> list[RenderedStatement]:
        # PostgreSQL no puede QUITAR un valor de un ENUM: el ADD VALUE es irreversible
        # sin recrear el tipo y todas sus columnas -> sin reverso (nunca se inventa uno).
        return self._stmts(
            item, self._render_enum(item.source_payload, item.target_payload), None,
        )

    def _ri_enum_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return self._stmts(
            item, [self._render_drop_enum(item.target_payload)],
            self._render_enum(item.target_payload, None), down_confirmed=False,
        )

    def _ri_extension_new(self, item: DiffItem) -> list[RenderedStatement]:
        return self._stmts(
            item, [self._render_extension(item.source_payload)],
            [self._render_drop_extension(item.source_payload)], down_confirmed=False,
        )

    def _ri_extension_dropped(self, item: DiffItem) -> list[RenderedStatement]:
        return self._stmts(
            item, [self._render_drop_extension(item.target_payload)],
            [self._render_extension(item.target_payload)], down_confirmed=False,
        )

    # ---- Renderer portable de FK (ambos motores comparten esta sintaxis) ---- #
    def _render_add_fk(self, table: str, fk) -> str:
        cols = ", ".join(self._q(c, "columna") for c in fk.columns)
        ref_cols = ", ".join(self._q(c, "columna") for c in fk.referred_columns)
        ref = self._q(fk.referred_table, "tabla")
        name = f"CONSTRAINT {self._q(fk.name, 'constraint')} " if fk.name else ""
        sql = (
            f"ALTER TABLE {self._q(table, 'tabla')} ADD {name}"
            f"FOREIGN KEY ({cols}) REFERENCES {ref} ({ref_cols})"
        )
        if fk.on_delete:
            sql += f" ON DELETE {self._sanitize_referential_action(fk.on_delete)}"
        if fk.on_update:
            sql += f" ON UPDATE {self._sanitize_referential_action(fk.on_update)}"
        return sql

    @staticmethod
    def _sanitize_referential_action(action: str) -> str:
        """Whitelist de acciones referenciales (nunca interpola texto crudo del motor)."""
        allowed = {"CASCADE", "SET NULL", "RESTRICT", "NO ACTION", "SET DEFAULT"}
        norm = (action or "").strip().upper()
        if norm not in allowed:
            raise AppHttpException(
                message="Acción referencial de FK no reconocida.",
                status_code=422, context={"action": norm},
            )
        return norm

    # ------------------------------------------------------------------ #
    # Hooks de rendering específicos de dialecto                           #
    # ------------------------------------------------------------------ #
    # NO son @abstractmethod a propósito: un ServerAdapter puede existir solo para
    # introspección (p.ej. dobles de test) sin capacidad de rendering. Los adapters
    # reales (MySQL/MariaDB/PostgreSQL) los implementan todos; llamar a uno no
    # implementado falla ruidosamente (nunca genera DDL silenciosamente incorrecto).
    def _render_column_def(self, col) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_column_def")

    def _render_create_table(self, tbl) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_create_table")

    def _render_modify_column(self, table, src_col, tgt_col, changed: list[str]) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_modify_column")

    def _render_drop_fk(self, table: str, fk) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_fk")

    def _render_drop_unique(self, table: str, uc) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_unique")

    def _render_drop_check(self, table: str, ck) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_check")

    def _render_create_index(self, table: str, ix) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_create_index")

    def _render_drop_index(self, table: str, ix) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_index")

    def _render_alter_pk(self, table: str, src_tbl, tgt_tbl) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_alter_pk")

    def _render_view(self, view, replace: bool) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_view")

    def _render_drop_view(self, view) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_view")

    def _render_routine(self, routine, replace: bool) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_routine")

    def _render_drop_routine(self, routine) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_routine")

    def _render_trigger(self, trigger, replace: bool) -> list[str]:
        raise NotImplementedError(f"{self.dialect}: _render_trigger")

    def _render_drop_trigger(self, trigger) -> str:
        raise NotImplementedError(f"{self.dialect}: _render_drop_trigger")

    # Los siguientes solo aplican a un motor; el default degrada a no-op para el otro.
    def _render_event(self, event, replace: bool) -> list[str]:
        return []

    def _render_drop_event(self, event) -> str:
        return f"DROP EVENT {self._q(event.name, 'event')}"

    def _render_sequence(self, seq, *, alter: bool) -> list[str]:
        return []

    def _render_drop_sequence(self, seq) -> str:
        return f"DROP SEQUENCE {self._q(seq.name, 'secuencia')}"

    def _render_enum(self, src_enum, tgt_enum) -> list[str]:
        return []

    def _render_drop_enum(self, enum) -> str:
        return f"DROP TYPE {self._q(enum.name, 'tipo')}"

    def _render_extension(self, ext) -> str:
        return f"CREATE EXTENSION IF NOT EXISTS {self._q(ext.name, 'extension')}"

    def _render_drop_extension(self, ext) -> str:
        return f"DROP EXTENSION {self._q(ext.name, 'extension')}"

    # ------------------------------------------------------------------ #
    # Datos-semilla (snapshot selectivo) — read-only, cross-dialect       #
    # ------------------------------------------------------------------ #
    def list_table_stats(
        self, database: str, *, conn: Connection | None = None
    ) -> list[TableStat]:
        """
        Estimación por tabla (filas + tiene PK) para informar la selección de datos.
        Solo métricas del catálogo, NUNCA valores de filas.

        ``conn`` (§6.4): ver ``_conn_ctx``. ``None`` = comportamiento histórico.
        """
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with self._conn_ctx(database, conn) as conn:
                insp = inspect(conn)
                out: list[TableStat] = []
                # Sin la contabilidad interna del gateway: nunca es candidata a sembrarse.
                for t in exclude_gateway_internal_tables(
                    sorted(insp.get_table_names(schema=schema))
                ):
                    pk = (
                        insp.get_pk_constraint(t, schema=schema).get("constrained_columns")
                        or []
                    )
                    estimate = self._estimate_rows(conn, t, schema)
                    out.append(
                        TableStat(
                            table=t,
                            estimated_rows=estimate if estimate is not None else 0,
                            estimated_rows_known=estimate is not None,
                            has_primary_key=bool(pk),
                        )
                    )
                return out
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="list_table_stats", target=self.target, extra={"database": database}
            )

    def dump_table_data(
        self,
        database: str,
        table: str,
        *,
        mode: str = "upsert",
        max_rows: int,
        max_bytes: int,
        batch_rows: int,
    ) -> SeedResult:
        """
        Extrae datos-semilla de UNA tabla como INSERT idempotente + rollback por PK.

        Fail-closed: sin PK, sin filas o si supera los guardrails (filas/bytes) → se
        OMITE (``included=False`` + ``reason``), nunca se emite SQL parcial. Los topes
        se acotan además por los techos duros de ``snapshot_data``.
        """
        max_rows, max_bytes = snapshot_data.effective_limits(max_rows, max_bytes)
        validate_identifier(database, self.dialect, "base de datos", allow_existing=True)
        validate_identifier(table, self.dialect, "tabla", allow_existing=True)
        schema = self._inspect_schema(database)
        try:
            with database_connection(self.target, database) as conn:
                insp = inspect(conn)
                pk = (
                    insp.get_pk_constraint(table, schema=schema).get("constrained_columns")
                    or []
                )
                if not pk:
                    return SeedResult(table=table, included=False, reason="no_primary_key")
                tbl = Table(table, MetaData(), autoload_with=conn, schema=schema)
                columns = [c.name for c in tbl.columns]
                # Defensa en dos capas: validar (no solo quotear) los identificadores
                # reflejados. Un nombre anómalo omite la tabla (fail-closed).
                try:
                    for c in columns:
                        validate_identifier(c, self.dialect, "columna", allow_existing=True)
                except AppHttpException:
                    return SeedResult(table=table, included=False, reason="invalid_identifier")
                order_cols = [tbl.c[c] for c in pk]
                # Streaming (yield_per) + LIMIT max_rows+1: acota la memoria (no materializa
                # filas grandes antes del guard de bytes) y detecta "supera el máximo".
                result = conn.execution_options(
                    stream_results=True, yield_per=max(1, batch_rows)
                ).execute(select(tbl).order_by(*order_cols).limit(max_rows + 1))
                return snapshot_data.build_seed(
                    dialect=self.dialect, table=table, columns=columns, pk=pk,
                    rows=result, mode=mode, batch_rows=batch_rows,
                    max_rows=max_rows, max_bytes=max_bytes,
                )
        except SQLAlchemyError as exc:
            raise map_driver_error(
                exc, op="dump_table_data", target=self.target,
                extra={"database": database, "table": table},
            )

    # ------------------------------------------------------------------ #
    # Helpers para DDL/DCL (usados por las operaciones de escritura)      #
    # ------------------------------------------------------------------ #
    def _execute_server(
        self, statements: list[str], *, op: str, extra: dict | None = None
    ) -> None:
        """Ejecuta sentencias a NIVEL SERVIDOR (AUTOCOMMIT). Para DDL/DCL."""
        try:
            with server_connection(self.target) as conn:
                for stmt in statements:
                    conn.execute(text(stmt))
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op=op, target=self.target, extra=extra)

    def _execute_database(
        self, database: str, statements: list[str], *, op: str, extra: dict | None = None
    ) -> None:
        """Ejecuta sentencias conectado a una BD CONCRETA (grants schema-level PG)."""
        try:
            with database_connection(self.target, database) as conn:
                conn = conn.execution_options(isolation_level="AUTOCOMMIT")
                for stmt in statements:
                    conn.execute(text(stmt))
        except SQLAlchemyError as exc:
            raise map_driver_error(exc, op=op, target=self.target, extra=extra)
