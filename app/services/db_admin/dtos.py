"""
DTOs de retorno de los adaptadores. Son Pydantic models para serializarse
directamente en las respuestas de la API (`ApiResponse[T]`). NUNCA contienen
datos de filas de las tablas gestionadas: solo estructura/metadatos.
"""

from pydantic import BaseModel


class ConnectionInfo(BaseModel):
    ok: bool
    dialect: str
    server_version: str | None = None


class EngineUserInfo(BaseModel):
    username: str
    host: str | None = None  # solo MySQL/MariaDB


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
