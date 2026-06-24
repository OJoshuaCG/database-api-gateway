"""
DTOs de retorno de los adaptadores. Son Pydantic models para serializarse
directamente en las respuestas de la API (`ApiResponse[T]`). NUNCA contienen
datos de filas de las tablas gestionadas: solo estructura/metadatos.
"""

import enum

from pydantic import BaseModel, ConfigDict, Field


class GrantLevel(str, enum.Enum):
    """
    Nivel de entidad sobre el que se otorga/revoca un privilegio.

    Fase 1 (object-level): DATABASE, SCHEMA (solo PG), TABLE, COLUMN,
    SEQUENCE (solo PG), ROUTINE. GLOBAL y los niveles raros (TYPE, LANGUAGE,
    FDW, ...) y la membresía de roles se incorporan en fases posteriores
    (ver docs/plans/07-gestion-granular-de-permisos.md).
    """

    GLOBAL = "global"
    DATABASE = "database"
    SCHEMA = "schema"
    TABLE = "table"
    COLUMN = "column"
    SEQUENCE = "sequence"
    ROUTINE = "routine"


class ConnectionInfo(BaseModel):
    ok: bool
    dialect: str
    server_version: str | None = None


class EngineUserInfo(BaseModel):
    username: str
    host: str | None = None  # solo MySQL/MariaDB


class RoutineRef(BaseModel):
    """Identidad de una rutina para grants de EXECUTE/ALTER ROUTINE."""

    kind: str  # FUNCTION | PROCEDURE
    name: str


class ObjectRef(BaseModel):
    """
    Objeto destino de un GRANT/REVOKE. Los campos relevantes dependen del nivel:
    DATABASE→database; SCHEMA(PG)→database+schema; TABLE/COLUMN→database[+schema]+table
    (+columns); SEQUENCE(PG)→database+schema+sequence; ROUTINE→database[+schema]+routine.
    `schema` solo aplica a PostgreSQL (default 'public').
    """

    model_config = ConfigDict(populate_by_name=True)

    database: str | None = None
    db_schema: str | None = Field(default=None, alias="schema")
    table: str | None = None
    columns: list[str] = Field(default_factory=list)
    sequence: str | None = None
    routine: RoutineRef | None = None


class ColumnInfo(BaseModel):
    name: str
    type: str
    nullable: bool
    default: str | None = None
    primary_key: bool = False
    autoincrement: bool = False
    comment: str | None = None


class ForeignKeyInfo(BaseModel):
    name: str | None = None
    columns: list[str]
    referred_table: str
    referred_columns: list[str]


class IndexInfo(BaseModel):
    name: str | None
    columns: list[str]
    unique: bool


class TableSchema(BaseModel):
    database: str
    table: str
    columns: list[ColumnInfo]
    primary_key: list[str]
    foreign_keys: list[ForeignKeyInfo]
    indexes: list[IndexInfo]
