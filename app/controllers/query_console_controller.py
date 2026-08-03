"""
Controller de la CONSOLA SQL: ejecutar queries ad-hoc sobre una BD de un servidor
destino, con el usuario del motor que se elija, en modo seguro.

Orquesta tres piezas que viven fuera de aquí:
- ``query_policy`` (PURO) decide QUÉ se puede ejecutar.
- ``query_runner`` decide CÓMO se ejecuta (credencial, transacción, topes).
- ``confirm_token`` + ``audit`` aportan la confirmación y el rastro.

CONFIRMACIÓN DE DOBLE FACTOR, igual que el DROP de bases de datos: ``confirm_target_name``
(obliga a identificar CUÁL base se toca) + ``confirm_token`` firmado con TTL (frescura y
anti-replay). Con una diferencia importante: aquí el token se ata además al **hash del
SQL** y al **usuario elegido**. Sin eso se podría pedir el preview de un ``SELECT`` y
canjear el token para ejecutar un ``DROP``, que es justo lo que la confirmación existe
para impedir.
"""

from datetime import datetime, timezone

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.core.context import current_http_identifier, current_request_ip
from app.core.crypto import CryptoConfigError, CryptoError, decrypt
from app.core.database import Database
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    QUERY_HISTORY_SQL_MAX_CHARS,
    QUERY_MAX_CELL_CHARS,
    QUERY_MAX_ROWS,
    QUERY_MAX_SQL_BYTES,
    QUERY_MAX_TIMEOUT_MS,
    QUERY_SAFE_MODE,
    QUERY_TIMEOUT_MS,
)
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.query_execution import (
    STATUS_BLOCKED,
    STATUS_ERROR,
    STATUS_SUCCESS,
    QueryExecution,
)
from app.models.server_user import ServerUser
from app.schemas.query_console import (
    QueryConnectionIn,
    QueryErrorOut,
    QueryExecuteOut,
    QueryPreviewOut,
    QueryReasonOut,
    QueryStatementPlanOut,
    QueryStatementResultOut,
)
from app.services import audit, confirm_token
from app.services.db_admin import query_policy, query_runner
from app.services.db_admin.identifiers import (
    reserved_database_names,
    validate_identifier,
)
from app.services.db_admin.query_runner import (
    MODE_ADMIN,
    MODE_IMPERSONATE,
    MODE_PROVIDED,
    MODE_STORED,
    QueryCredential,
)

logger = get_logger(__name__)

_CONSOLE_OP = "sql-console"


class QueryConsoleController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Contexto y credencial                                               #
    # ------------------------------------------------------------------ #
    def _load_context(self, server_id: int, database: str, connection: QueryConnectionIn):
        """
        (dialect, target, credential). Resuelve TODO lo que necesita la BD del gateway y
        cierra la sesión antes de tocar el motor destino.
        """
        session = self._session()
        try:
            server = get_server_or_404(session, server_id)
            dialect = engine_value(server)
            target = build_target(server)
            credential = self._resolve_credential(
                session,
                server_id=server_id,
                dialect=dialect,
                root_username=server.root_username,
                connection=connection,
            )
        finally:
            session.close()

        # La consola nunca debe poder apuntarse a la propia base de metadatos del
        # gateway: si vive en un servidor del inventario, un DROP desde aquí se llevaría
        # el inventario, la auditoría y el historial.
        if query_policy.is_gateway_metadata_target(
            host=target.host,
            port=target.port,
            database=database,
            gateway_host=DB_HOST,
            gateway_port=DB_PORT,
            gateway_database=DB_NAME,
        ):
            raise AppHttpException(
                message=(
                    "El destino es la propia base de metadatos del gateway. La consola no "
                    "puede operar sobre ella."
                ),
                status_code=409,
                context={"database": database},
            )

        return dialect, target, credential

    def _resolve_credential(
        self,
        session,
        *,
        server_id: int,
        dialect: str,
        root_username: str,
        connection: QueryConnectionIn,
    ) -> QueryCredential:
        mode = connection.mode

        if mode == MODE_ADMIN:
            return QueryCredential(mode=mode, username=root_username)

        if mode == MODE_IMPERSONATE:
            if dialect != "postgresql":
                raise AppHttpException(
                    message=(
                        "La impersonación con SET ROLE solo existe en PostgreSQL. En "
                        "MySQL/MariaDB un usuario solo puede adoptar roles que ya le "
                        "fueron otorgados, así que hace falta la credencial real: usá "
                        "connection.mode='provided' o 'stored'."
                    ),
                    status_code=422,
                    context={"engine": dialect},
                )
            role = (connection.role or connection.username or "").strip()
            if not role:
                raise AppHttpException(
                    message="El modo impersonate requiere 'connection.role'.",
                    status_code=422,
                    context={"required": "connection.role"},
                )
            validate_identifier(role, dialect, "rol", allow_existing=True)
            return QueryCredential(mode=mode, username=role, impersonate_role=role)

        username = (connection.username or "").strip()
        if not username:
            raise AppHttpException(
                message=f"El modo {mode} requiere 'connection.username'.",
                status_code=422,
                context={"required": "connection.username"},
            )
        validate_identifier(username, dialect, "usuario", allow_existing=True)

        if mode == MODE_PROVIDED:
            if not connection.password:
                raise AppHttpException(
                    message=(
                        "El modo provided requiere 'connection.password'. Si el gateway "
                        "fijó la contraseña de este usuario, usá el modo 'stored'."
                    ),
                    status_code=422,
                    context={"required": "connection.password"},
                )
            return QueryCredential(
                mode=mode, username=username, password=connection.password
            )

        if mode != MODE_STORED:
            raise AppHttpException(
                message=f"Modo de conexión no soportado: {mode}",
                status_code=422,
                context={"mode": mode},
            )

        # --- stored: el gateway conoce la contraseña (Fernet reversible) ---
        query = session.query(ServerUser).filter(
            ServerUser.server_id == server_id, ServerUser.username == username
        )
        if dialect != "postgresql":
            query = query.filter(ServerUser.host == (connection.host or "%"))
        row = query.first()
        if row is None:
            raise AppHttpException(
                message=(
                    "El usuario no está en el inventario del gateway, así que no hay "
                    "contraseña almacenada. Usá el modo 'provided' con la contraseña."
                ),
                status_code=404,
                context={"username": username, "host": connection.host},
            )
        if not row.password_encrypted:
            raise AppHttpException(
                message=(
                    "El usuario está en el inventario pero el gateway nunca fijó su "
                    "contraseña (el motor solo guarda un hash irreversible). Usá el modo "
                    "'provided'."
                ),
                status_code=409,
                context={"username": username, "server_user_id": row.id},
            )
        try:
            password = decrypt(row.password_encrypted)
        except (CryptoError, CryptoConfigError) as exc:
            raise AppHttpException(
                message="No se pudo descifrar la contraseña almacenada del usuario.",
                status_code=500,
                context={"server_user_id": row.id},
            ) from exc
        return QueryCredential(mode=mode, username=username, password=password)

    # ------------------------------------------------------------------ #
    # Validación de entrada                                               #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _validate_sql(sql: str) -> str:
        text = (sql or "").strip()
        if not text:
            raise AppHttpException(
                message="El SQL está vacío.", status_code=422, context={}
            )
        size = len(text.encode("utf-8"))
        if size > QUERY_MAX_SQL_BYTES:
            raise AppHttpException(
                message=(
                    f"El SQL supera el tope de {QUERY_MAX_SQL_BYTES} bytes "
                    f"({size} enviados)."
                ),
                status_code=422,
                context={"max_bytes": QUERY_MAX_SQL_BYTES, "bytes": size},
            )
        return text

    @staticmethod
    def _token_subject(
        plan: query_policy.QueryPlan,
        credential: QueryCredential,
        connection: QueryConnectionIn,
    ) -> str:
        """
        Ata el token al SQL EXACTO y a la IDENTIDAD elegida, no solo a la base de datos.
        Cambiar cualquiera de esos datos invalida el token y obliga a repetir el preview.

        El ``host`` entra en el sujeto porque en MySQL/MariaDB ``'app'@'localhost'`` y
        ``'app'@'%'`` son cuentas SEPARADAS, con contraseñas y privilegios distintos: sin
        él, un preview hecho con una identidad se podría canjear con la otra.
        """
        return (
            f"{plan.sql_hash}|{credential.mode}|{credential.username}|"
            f"{credential.impersonate_role or ''}|{connection.host or ''}"
        )

    @staticmethod
    def _warnings(
        *, dialect: str, credential: QueryCredential, plan: query_policy.QueryPlan
    ) -> list[str]:
        warnings: list[str] = []
        if credential.mode == MODE_ADMIN:
            warnings.append(
                "Se ejecutará con la credencial pseudo-root del servidor: los permisos NO "
                "se están probando, se están evitando. Elegí un usuario concreto para "
                "verificar permisos."
            )
        if credential.mode == MODE_IMPERSONATE:
            warnings.append(
                "SET ROLE reproduce los permisos del rol para esta sesión, pero es una "
                "herramienta de prueba, no una frontera de seguridad."
            )
        if dialect in ("mysql", "mariadb") and plan.danger == query_policy.DDL:
            warnings.append(
                "MySQL/MariaDB hacen COMMIT implícito en cada sentencia DDL: si el lote "
                "falla a mitad, lo ya ejecutado NO se revierte."
            )
        if len(plan.statements) > 1:
            warnings.append(
                f"El lote tiene {len(plan.statements)} sentencias y se ejecuta en orden, "
                "deteniéndose en el primer error."
            )
        return warnings

    # ------------------------------------------------------------------ #
    # Preview                                                             #
    # ------------------------------------------------------------------ #
    def preview(
        self,
        server_id: int,
        *,
        database: str,
        sql: str,
        connection: QueryConnectionIn,
        estimate_impact: bool = True,
        admin: dict | None = None,
    ) -> QueryPreviewOut:
        sql = self._validate_sql(sql)
        dialect, target, credential = self._load_context(server_id, database, connection)
        validate_identifier(database, dialect, "base de datos", allow_existing=True)

        plan = query_policy.classify(sql, engine=dialect, max_rows=QUERY_MAX_ROWS)

        estimates: dict[int, int | None] = {}
        if estimate_impact and not plan.is_blocked:
            pending = [
                (s.seq, s.impact_query) for s in plan.statements if s.impact_query
            ]
            if pending:
                # El preview TOCA el motor: se conecta con la credencial elegida y
                # ejecuta los COUNT. Sin rastro sería un oráculo de contraseñas sin
                # auditar (el error distingue "clave mala" de "clave buena sin permiso"),
                # y un scan completo sobre una tabla arbitraria sin confirmación.
                audit.record_intent(
                    "query_console.preview",
                    admin=admin,
                    target_type="server_database",
                    server_id=server_id,
                    touched_engine=True,
                    detail=(
                        f"{database} as {credential.username} ({credential.mode}): "
                        f"estimación de impacto de {len(pending)} sentencia(s)"
                    )[:500],
                )
                estimates = query_runner.estimate_impact(
                    target,
                    database=database,
                    engine=dialect,
                    credential=credential,
                    impact_queries=pending,
                    timeout_ms=QUERY_TIMEOUT_MS,
                )

        token: str | None = None
        expires_at: datetime | None = None
        if plan.requires_confirmation and not plan.is_blocked:
            token, expires_at = confirm_token.issue(
                _CONSOLE_OP,
                server_id,
                database,
                subject=self._token_subject(plan, credential, connection),
            )

        return QueryPreviewOut(
            server_id=server_id,
            database=database,
            engine=dialect,
            run_as=credential.username,
            connection_mode=credential.mode,
            danger=plan.danger,
            requires_confirmation=plan.requires_confirmation and QUERY_SAFE_MODE,
            blocked=plan.is_blocked,
            statements=[
                QueryStatementPlanOut(
                    seq=s.seq,
                    sql=s.sql,
                    kind=s.kind,
                    danger=s.danger,
                    reasons=[QueryReasonOut(code=r.code, message=r.message) for r in s.reasons],
                    estimated_rows=estimates.get(s.seq),
                )
                for s in plan.statements
            ],
            reasons=[QueryReasonOut(code=r.code, message=r.message) for r in plan.reasons],
            warnings=self._warnings(dialect=dialect, credential=credential, plan=plan),
            confirm_token=token,
            expires_at=expires_at,
        )

    def _reject_blocked(
        self,
        *,
        server_id: int,
        database: str,
        dialect: str,
        plan: query_policy.QueryPlan,
        credential: QueryCredential,
        admin: dict | None,
        dry_run: bool,
        message: str,
        reasons: list[dict],
    ) -> None:
        """
        Rechaza sin tocar el motor, dejando rastro en historial y auditoría.

        Los motivos van en ``public_context`` y NO en ``context``: este último solo se
        expone en desarrollo (es info de debug), así que en producción el operador habría
        recibido "hay sentencias prohibidas" sin saber cuál ni por qué — lo contrario de
        lo que esta consola necesita comunicar.
        """
        detail = "; ".join(r["message"] for r in reasons)[:400]
        self._record_history(
            server_id=server_id,
            database=database,
            engine=dialect,
            plan=plan,
            credential=credential,
            admin=admin,
            status=STATUS_BLOCKED,
            read_only=False,
            dry_run=dry_run,
            committed=False,
            error_code=None,
            error_message=detail,
        )
        audit.record(
            "query_console.blocked",
            status="denied",
            admin=admin,
            target_type="server_database",
            server_id=server_id,
            touched_engine=False,
            detail=f"{database}: {detail}"[:500],
        )
        raise AppHttpException(
            message=message,
            status_code=403,
            public_context={
                "database": database,
                "blocked_statements": [
                    {"seq": st.seq, "sql": st.sql[:500]}
                    for st in plan.blocked_statements
                ],
                "reasons": reasons,
            },
        )

    # ------------------------------------------------------------------ #
    # Execute                                                             #
    # ------------------------------------------------------------------ #
    def execute(
        self,
        server_id: int,
        *,
        database: str,
        sql: str,
        connection: QueryConnectionIn,
        confirm_token_value: str | None = None,
        confirm_target_name: str | None = None,
        dry_run: bool = False,
        max_rows: int | None = None,
        timeout_ms: int | None = None,
        admin: dict | None = None,
    ) -> QueryExecuteOut:
        sql = self._validate_sql(sql)
        dialect, target, credential = self._load_context(server_id, database, connection)
        validate_identifier(database, dialect, "base de datos", allow_existing=True)

        effective_rows = min(max_rows or QUERY_MAX_ROWS, QUERY_MAX_ROWS)
        effective_timeout = min(timeout_ms or QUERY_TIMEOUT_MS, QUERY_MAX_TIMEOUT_MS)

        plan = query_policy.classify(sql, engine=dialect, max_rows=effective_rows)
        read_only = plan.danger == query_policy.READ

        # Escribir sobre una BD de SISTEMA del motor, aunque el SQL no la nombre: la BD
        # se elige por fuera de la sentencia, así que ``UPDATE user SET …`` conectado a
        # ``mysql`` no menciona ningún esquema y el guard textual no lo ve.
        if not read_only and database.lower() in reserved_database_names(dialect):
            self._reject_blocked(
                server_id=server_id,
                database=database,
                dialect=dialect,
                plan=plan,
                credential=credential,
                admin=admin,
                dry_run=dry_run,
                message=(
                    f"'{database}' es una base de datos de sistema del motor: la consola "
                    "solo permite leerla."
                ),
                reasons=[
                    {
                        "code": "system_database_write",
                        "message": "Modificar una base de datos de sistema corrompería el "
                        "propio servidor.",
                    }
                ],
            )

        # --- 1) Prohibido: no se toca el motor ni con confirmación --- #
        if plan.is_blocked:
            self._reject_blocked(
                server_id=server_id,
                database=database,
                dialect=dialect,
                plan=plan,
                credential=credential,
                admin=admin,
                dry_run=dry_run,
                message=(
                    "La consulta contiene sentencias prohibidas por la política del modo "
                    "seguro y no se ejecuta ni con confirmación."
                ),
                reasons=[
                    {"code": r.code, "message": r.message} for r in plan.reasons
                ],
            )

        # --- 2) Confirmación de doble factor para todo lo que no sea lectura --- #
        if plan.requires_confirmation and QUERY_SAFE_MODE:
            if confirm_target_name != database:
                raise AppHttpException(
                    message=(
                        "Esta consulta modifica datos o estructura: 'confirm_target_name' "
                        "debe coincidir exactamente con el nombre de la base de datos."
                    ),
                    status_code=422,
                    context={"required": "confirm_target_name == database"},
                )
            if not confirm_token_value:
                raise AppHttpException(
                    message=(
                        "Esta consulta modifica datos o estructura: solicitá el preview y "
                        "enviá su 'confirm_token'."
                    ),
                    status_code=422,
                    context={"required": "confirm_token"},
                )
            confirm_token.verify(
                confirm_token_value,
                _CONSOLE_OP,
                server_id,
                database,
                subject=self._token_subject(plan, credential, connection),
            )

        # --- 3) Auditoría fail-closed ANTES de tocar el motor --- #
        if not read_only:
            audit.record_intent(
                "query_console.execute",
                admin=admin,
                target_type="server_database",
                server_id=server_id,
                touched_engine=True,
                detail=(
                    f"{database} as {credential.username} ({credential.mode}) "
                    f"[{plan.danger}{', dry-run' if dry_run else ''}]: "
                    f"{query_policy.redact_secrets(sql)}"
                )[:500],
            )

        started = datetime.now(timezone.utc)
        outcome = query_runner.run_statements(
            target,
            database=database,
            engine=dialect,
            statements=list(plan.statements),
            credential=credential,
            read_only=read_only,
            dry_run=dry_run,
            max_rows=effective_rows,
            max_cell_chars=QUERY_MAX_CELL_CHARS,
            timeout_ms=effective_timeout,
        )

        duration_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
        first_error = next(
            (s.error for s in outcome.statements if s.error is not None), None
        ) or outcome.connection_error

        execution_id = self._record_history(
            server_id=server_id,
            database=database,
            engine=dialect,
            plan=plan,
            credential=credential,
            admin=admin,
            status=STATUS_SUCCESS if outcome.success else STATUS_ERROR,
            read_only=read_only,
            dry_run=dry_run,
            committed=outcome.committed,
            error_code=first_error.code if first_error else None,
            error_message=first_error.message if first_error else None,
            rows_returned=sum(s.row_count for s in outcome.statements),
            rows_affected=sum(s.rows_affected or 0 for s in outcome.statements),
            duration_ms=duration_ms,
        )

        if not read_only:
            audit.record(
                "query_console.execute",
                status="success" if outcome.success else "error",
                admin=admin,
                target_type="server_database",
                target_id=execution_id,
                server_id=server_id,
                touched_engine=True,
                detail=f"{database}: {plan.danger}, committed={outcome.committed}",
            )

        return QueryExecuteOut(
            server_id=server_id,
            database=database,
            engine=dialect,
            run_as=credential.username,
            connection_mode=credential.mode,
            danger=plan.danger,
            success=outcome.success,
            read_only=read_only,
            dry_run=dry_run,
            committed=outcome.committed,
            rolled_back=outcome.rolled_back,
            ddl_persisted=outcome.ddl_persisted,
            statements=[
                QueryStatementResultOut(
                    seq=s.seq,
                    sql=s.sql,
                    kind=s.kind,
                    danger=s.danger,
                    executed=s.executed,
                    success=s.success,
                    duration_ms=s.duration_ms,
                    columns=s.columns,
                    rows=s.rows,
                    row_count=s.row_count,
                    rows_affected=s.rows_affected,
                    truncated=s.truncated,
                    policy_miss=s.policy_miss,
                    error=(
                        QueryErrorOut(
                            code=s.error.code,
                            sqlstate=s.error.sqlstate,
                            message=s.error.message,
                        )
                        if s.error
                        else None
                    ),
                )
                for s in outcome.statements
            ],
            connection_error=(
                QueryErrorOut(
                    code=outcome.connection_error.code,
                    sqlstate=outcome.connection_error.sqlstate,
                    message=outcome.connection_error.message,
                )
                if outcome.connection_error
                else None
            ),
            warnings=outcome.warnings
            + self._warnings(dialect=dialect, credential=credential, plan=plan),
            execution_id=execution_id,
        )

    # ------------------------------------------------------------------ #
    # Historial                                                           #
    # ------------------------------------------------------------------ #
    def _record_history(
        self,
        *,
        server_id: int,
        database: str,
        engine: str,
        plan: query_policy.QueryPlan,
        credential: QueryCredential,
        admin: dict | None,
        status: str,
        read_only: bool,
        dry_run: bool,
        committed: bool,
        error_code: str | None,
        error_message: str | None,
        rows_returned: int = 0,
        rows_affected: int = 0,
        duration_ms: int = 0,
    ) -> int | None:
        """
        Persiste la fila del historial. **Best-effort**: el rastro fail-closed lo garantiza
        ``audit.record_intent``, así que un fallo al guardar el historial no debe tirar
        abajo una operación que ya se ejecutó en el motor.
        """
        admin = admin or {}
        # Se guarda el SQL COMPLETO del lote (con contraseñas redactadas), recortado al
        # tope: es lo que la UI vuelve a cargar para re-ejecutar.
        sql_text = query_policy.redact_secrets(
            "\n".join(s.sql for s in plan.statements)
        )[:QUERY_HISTORY_SQL_MAX_CHARS]
        try:
            session = self._session()
            try:
                row = QueryExecution(
                    server_id=server_id,
                    database_name=database,
                    engine=engine,
                    admin_id=admin.get("id"),
                    admin_username=admin.get("username"),
                    connection_mode=credential.mode,
                    run_as_username=credential.username,
                    impersonated_role=credential.impersonate_role,
                    sql_text=sql_text,
                    sql_hash=plan.sql_hash,
                    danger_level=plan.danger,
                    statement_count=len(plan.statements),
                    read_only=read_only,
                    dry_run=dry_run,
                    committed=committed,
                    status=status,
                    rows_returned=rows_returned,
                    rows_affected=rows_affected,
                    duration_ms=duration_ms,
                    error_code=(error_code or None),
                    error_message=(error_message or None),
                    request_id=_ctx(current_http_identifier),
                    ip=_ctx(current_request_ip),
                )
                session.add(row)
                session.commit()
                return row.id
            finally:
                session.close()
        except Exception:  # noqa: BLE001 — el historial nunca rompe la operación
            logger.warning(
                "No se pudo registrar el historial de la consola SQL", exc_info=True
            )
            return None

    def list_history(
        self,
        server_id: int,
        *,
        database: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[QueryExecution], int]:
        session = self._session()
        try:
            query = session.query(QueryExecution).filter(
                QueryExecution.server_id == server_id
            )
            if database:
                query = query.filter(QueryExecution.database_name == database)
            total = query.count()
            items = (
                query.order_by(QueryExecution.id.desc()).limit(limit).offset(offset).all()
            )
            return items, total
        finally:
            session.close()


def _ctx(ctxvar) -> str | None:
    try:
        return ctxvar.get() or None
    except LookupError:
        return None
