"""
Schemas de la CONSOLA SQL: ejecutar queries ad-hoc contra una BD de un servidor destino,
con el usuario del motor que se elija, en modo seguro.

El ciclo es ``preview`` → ``execute``: el preview clasifica el SQL, estima el impacto y
emite el ``confirm_token``; el execute lo canjea. Un ``SELECT`` puro no necesita preview.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class QueryConnectionIn(BaseModel):
    """
    Con qué credencial del MOTOR ejecutar. El propósito de la consola es probar permisos,
    así que la elección del usuario es parte del request, no una configuración.

    - ``admin``: credencial pseudo-root del servidor. Es la más peligrosa y la que la UI
      debe marcar visiblemente.
    - ``stored``: usuario del inventario cuya contraseña fijó el gateway (cifrada con
      Fernet). Requiere ``username`` (y ``host`` en MySQL/MariaDB).
    - ``provided``: contraseña enviada en este request. NO se persiste. Es el modo normal
      para un usuario que el gateway no creó.
    - ``impersonate``: **solo PostgreSQL**. Conecta como pseudo-root y emite ``SET ROLE``,
      que permite adoptar cualquier rol sin conocer su contraseña. MySQL/MariaDB no tienen
      equivalente y devuelven 422.
    """

    mode: Literal["admin", "stored", "provided", "impersonate"] = Field(
        default="admin", description="Modo de conexión al motor."
    )
    username: str | None = Field(
        default=None, description="Usuario del motor (modos stored/provided)."
    )
    host: str | None = Field(
        default=None,
        description="Host de la identidad 'user'@'host' (MySQL/MariaDB, modo stored).",
    )
    password: str | None = Field(
        default=None,
        description="Contraseña para el modo provided. Nunca se persiste ni se audita.",
    )
    role: str | None = Field(
        default=None, description="Rol a adoptar con SET ROLE (modo impersonate)."
    )


class QueryReasonOut(BaseModel):
    code: str = Field(description="Código estable del motivo (para la UI).")
    message: str


class QueryStatementPlanOut(BaseModel):
    seq: int
    sql: str = Field(
        description=(
            "SQL realmente ejecutado. Puede diferir del enviado: a una consulta de lectura "
            "se le empuja un LIMIT al motor para acotar la transferencia."
        )
    )
    kind: str = Field(description="select | insert | update | delete | create | …")
    danger: str = Field(description="read | write | ddl | blocked")
    reasons: list[QueryReasonOut] = Field(default_factory=list)
    estimated_rows: int | None = Field(
        default=None,
        description=(
            "Filas que afectaría un UPDATE/DELETE, contadas con la MISMA credencial de la "
            "ejecución. null = no se pudo contar de forma exacta (varias tablas, USING/"
            "JOIN, o el usuario no puede leer la tabla); la confirmación se exige igual."
        ),
    )


class QueryPreviewIn(BaseModel):
    database: str = Field(..., min_length=1, max_length=128)
    sql: str = Field(..., min_length=1)
    connection: QueryConnectionIn = Field(default_factory=QueryConnectionIn)
    estimate_impact: bool = Field(
        default=True,
        description=(
            "Ejecuta un SELECT COUNT(*) derivado de cada UPDATE/DELETE para saber cuántas "
            "filas se verían afectadas ANTES de confirmar."
        ),
    )


class QueryPreviewOut(BaseModel):
    server_id: int
    database: str
    engine: str
    run_as: str = Field(description="Usuario del motor con el que se ejecutaría.")
    connection_mode: str
    danger: str = Field(description="Nivel del LOTE: el máximo de sus sentencias.")
    requires_confirmation: bool
    blocked: bool = Field(
        description="True = prohibido incluso confirmando; execute devolverá 403."
    )
    statements: list[QueryStatementPlanOut] = Field(default_factory=list)
    reasons: list[QueryReasonOut] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    confirm_token: str | None = Field(
        default=None,
        description="Token firmado (TTL 2 min) atado al SQL, la BD y el usuario elegidos.",
    )
    expires_at: datetime | None = None


class QueryExecuteIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    database: str = Field(..., min_length=1, max_length=128)
    sql: str = Field(..., min_length=1)
    connection: QueryConnectionIn = Field(default_factory=QueryConnectionIn)
    confirm_token: str | None = Field(
        default=None,
        description="Token del preview. Obligatorio si el lote no es de solo lectura.",
    )
    confirm_target_name: str | None = Field(
        default=None,
        description=(
            "Debe coincidir EXACTO con el nombre de la base de datos. Obligatorio si el "
            "lote no es de solo lectura: obliga a identificar CUÁL base se está tocando."
        ),
    )
    dry_run: bool = Field(
        default=False,
        description=(
            "Ejecuta y revierte para medir el impacto real sin persistir. Atención: el DDL "
            "de MySQL/MariaDB hace COMMIT implícito y NO se revierte."
        ),
    )
    max_rows: int | None = Field(
        default=None, ge=1, description="Tope de filas por sentencia (acotado por QUERY_MAX_ROWS)."
    )
    timeout_ms: int | None = Field(
        default=None, ge=100, description="Timeout por sentencia (acotado por QUERY_MAX_TIMEOUT_MS)."
    )


class QueryErrorOut(BaseModel):
    code: str | None = Field(default=None, description="errno de MySQL/MariaDB o SQLSTATE.")
    sqlstate: str | None = None
    message: str


class QueryStatementResultOut(BaseModel):
    seq: int
    sql: str
    kind: str
    danger: str
    executed: bool = Field(description="False = no llegó a ejecutarse (el lote se detuvo antes).")
    success: bool
    duration_ms: int
    columns: list[str] = Field(default_factory=list)
    rows: list[list[Any]] = Field(default_factory=list)
    row_count: int = 0
    rows_affected: int | None = None
    truncated: bool = Field(
        default=False, description="True = el resultado se recortó al tope de filas."
    )
    policy_miss: bool = Field(
        default=False,
        description=(
            "True = el motor rechazó la sentencia por la transacción de SOLO LECTURA, es "
            "decir, la política la clasificó como lectura y no lo era. Es un fallo de "
            "clasificación del gateway, no del usuario: reportalo."
        ),
    )
    error: QueryErrorOut | None = None


class QueryExecuteOut(BaseModel):
    server_id: int
    database: str
    engine: str
    run_as: str
    connection_mode: str
    danger: str
    success: bool = Field(
        description=(
            "False cuando el MOTOR rechazó alguna sentencia. Un rechazo por permisos es un "
            "resultado válido de la prueba, no un error de la API: la respuesta sigue "
            "siendo 200."
        )
    )
    read_only: bool
    dry_run: bool
    committed: bool
    rolled_back: bool
    ddl_persisted: bool = Field(
        default=False,
        description=(
            "True = quedaron cambios de ESQUEMA aplicados pese al rollback. MySQL/MariaDB "
            "hacen COMMIT implícito en cada sentencia DDL, así que 'rolled_back' no "
            "alcanza para describir el estado real."
        ),
    )
    statements: list[QueryStatementResultOut] = Field(default_factory=list)
    connection_error: QueryErrorOut | None = Field(
        default=None,
        description="Fallo al autenticarse o al abrir la BD con la credencial elegida.",
    )
    warnings: list[str] = Field(default_factory=list)
    execution_id: int | None = Field(default=None, description="Fila del historial.")


class QueryHistoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    server_id: int
    database_name: str
    engine: str
    admin_username: str | None = None
    connection_mode: str
    run_as_username: str
    impersonated_role: str | None = None
    sql_text: str
    danger_level: str
    statement_count: int
    status: str
    read_only: bool
    dry_run: bool
    committed: bool
    rows_returned: int
    rows_affected: int
    duration_ms: int
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime
