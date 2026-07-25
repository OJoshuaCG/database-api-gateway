"""
Checkpoint de sentencias SQL dentro de UNA migración — permite retomar un ``apply``/
``rollback`` parcialmente fallido desde la última sentencia exitosa, sin reintentar las
que ya commitearon (DDL en AUTOCOMMIT, ``migrations.py::_prepared``).

Deliberadamente CONSERVADOR (fail-closed, revisado con ``gateway-senior-python`` y
``gateway-db-dialects`` antes de implementar): el checkpoint solo se activa para SQL de
esquema "plano" sin estado de sesión. Cada exclusión de ``is_resumable`` responde a un
riesgo de CORRUPCIÓN SILENCIOSA detectado en esa revisión, no es arbitraria:

- ``kind='data'``: la sintaxis upsert (``ON DUPLICATE KEY UPDATE`` / ``ON CONFLICT``) y
  el ``down_sql`` (DELETE por PK) no valen el riesgo de indexar mal.
- ``has_non_portable`` (rutinas/triggers/events MySQL/MariaDB): se mantiene excluido por
  CONSERVADURISMO, no por una limitación del splitter. ``split_sql_statements`` **sí**
  entiende hoy los bloques ``BEGIN...END`` (además del dollar-quoting de PostgreSQL y de
  la directiva ``DELIMITER``), así que el índice de sentencia ya no sería basura; pero un
  cuerpo procedural sigue siendo el caso donde un resume mal indexado costaría más caro,
  y no hay demanda de reanudarlo. Si alguna vez se relaja, hace falta verificarlo contra
  motor real antes.
- Sentencias que dependen de ESTADO DE SESIÓN (``SET``, ``PREPARE``, ``CREATE TEMPORARY``,
  ``LOCK TABLES``, ``USE``, transacciones explícitas): un reintento abre una conexión
  NUEVA (``database_connection`` en ``_prepared``) — ese estado se pierde. Un resume que
  arranca "desde la sentencia 4" perdería, por ejemplo, el ``SET FOREIGN_KEY_CHECKS=0``
  de la sentencia 1, cambiando el comportamiento de las sentencias restantes sin avisar.

Si CUALQUIER sentencia de una migración no es segura, la migración COMPLETA se trata
como no resumible: comportamiento todo-o-nada (el actual, sin checkpoint), no un resume
parcialmente confiable.

El checkpoint vive en la BD de METADATOS del gateway (una conexión totalmente distinta
a la del motor destino que ejecuta el DDL) — cada función abre su propia sesión corta,
nunca reutiliza la sesión de un request en curso.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.models.migration_statement_progress import MigrationStatementProgress

# Prefijos (case-insensitive, tras strip) que delatan dependencia de estado de SESIÓN de
# la conexión — no sobreviven a una conexión nueva en un resume.
_SESSION_STATEMENT_PREFIXES = (
    "set ", "set@",
    "prepare ", "execute ", "deallocate ",
    "create temporary", "create temp ",
    "lock tables", "unlock tables",
    "use ",
    "start transaction", "begin", "savepoint", "release savepoint",
    "declare ", "commit", "rollback",
)

# Indicios de bloque procedural. El splitter ya los parte bien (ver arriba); esto excluye
# la migración del checkpoint por prudencia, no porque el split sea incorrecto.
_PROCEDURAL_HINT = re.compile(
    r"\b(create|alter)\s+(procedure|function|trigger|event)\b", re.IGNORECASE
)


def is_resumable(
    raw_sql: str, statements: list[str], *, kind: str, has_non_portable: bool
) -> bool:
    """
    Decide si una migración puede reanudarse sentencia-por-sentencia. Fail-closed: ante
    cualquier duda, False (todo-o-nada, comportamiento actual sin checkpoint).
    """
    if kind == "data" or has_non_portable or not statements:
        return False
    if _PROCEDURAL_HINT.search(raw_sql):
        return False
    for stmt in statements:
        s = stmt.strip().lower()
        if s and s.startswith(_SESSION_STATEMENT_PREFIXES):
            return False
    return True


@dataclass(frozen=True)
class ProgressState:
    last_statement_index: int
    total_statements: int
    migration_checksum: str


def _session():
    return Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT).get_declarative_base_session()


def get_progress(
    managed_database_id: int, model_migration_id: int, direction: str
) -> ProgressState | None:
    session = _session()
    try:
        row = (
            session.query(MigrationStatementProgress)
            .filter(
                MigrationStatementProgress.managed_database_id == managed_database_id,
                MigrationStatementProgress.model_migration_id == model_migration_id,
                MigrationStatementProgress.direction == direction,
            )
            .first()
        )
        if row is None:
            return None
        return ProgressState(
            last_statement_index=row.last_statement_index,
            total_statements=row.total_statements,
            migration_checksum=row.migration_checksum,
        )
    finally:
        session.close()


def record_statement(
    managed_database_id: int,
    model_migration_id: int,
    direction: str,
    index: int,
    total: int,
    migration_checksum: str,
) -> None:
    """Upsert: se llama DESPUÉS de que la sentencia ``index`` (1-based) ejecutó con éxito.

    Sesión corta y dedicada (no la del request en curso, no la conexión al motor
    destino): este write es completamente independiente del DDL que se está aplicando.
    """
    session = _session()
    try:
        row = (
            session.query(MigrationStatementProgress)
            .filter(
                MigrationStatementProgress.managed_database_id == managed_database_id,
                MigrationStatementProgress.model_migration_id == model_migration_id,
                MigrationStatementProgress.direction == direction,
            )
            .first()
        )
        if row is None:
            row = MigrationStatementProgress(
                managed_database_id=managed_database_id,
                model_migration_id=model_migration_id,
                direction=direction,
            )
            session.add(row)
        row.last_statement_index = index
        row.total_statements = total
        row.migration_checksum = migration_checksum
        session.commit()
    finally:
        session.close()


def clear_progress(
    managed_database_id: int, model_migration_id: int, direction: str
) -> None:
    """Borra el checkpoint de UNA migración/dirección — se llama tras completarla con éxito."""
    session = _session()
    try:
        session.query(MigrationStatementProgress).filter(
            MigrationStatementProgress.managed_database_id == managed_database_id,
            MigrationStatementProgress.model_migration_id == model_migration_id,
            MigrationStatementProgress.direction == direction,
        ).delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def clear_progress_for_database(managed_database_id: int, direction: str | None = None) -> None:
    """Borra TODOS los checkpoints de una BD (usado por ``stamp(force=true)``)."""
    session = _session()
    try:
        q = session.query(MigrationStatementProgress).filter(
            MigrationStatementProgress.managed_database_id == managed_database_id,
        )
        if direction is not None:
            q = q.filter(MigrationStatementProgress.direction == direction)
        q.delete(synchronize_session=False)
        session.commit()
    finally:
        session.close()


def incomplete_progress_for_migration(
    model_migration_id: int, direction: str = "up"
) -> list[dict]:
    """
    Filas con progreso INCOMPLETO (0 < last < total) de esta migración, en CUALQUIER BD.

    Usado para bloquear la edición de SQL (``ModelMigrationController.update_migration``)
    mientras haya un resume en curso. Deliberadamente NO filtra por checksum: el punto es
    "hay algo a mitad de camino en esta migración", sin importar si el SQL que se quiere
    escribir coincidiría o no con lo ya aplicado — cualquier edición mientras hay progreso
    parcial es insegura (ver docstring del módulo).
    """
    session = _session()
    try:
        rows = (
            session.query(MigrationStatementProgress)
            .filter(
                MigrationStatementProgress.model_migration_id == model_migration_id,
                MigrationStatementProgress.direction == direction,
            )
            .all()
        )
        return [
            {
                "managed_database_id": r.managed_database_id,
                "last_statement_index": r.last_statement_index,
                "total_statements": r.total_statements,
            }
            for r in rows
            if 0 < r.last_statement_index < r.total_statements
        ]
    finally:
        session.close()


def incomplete_progress_for_database(
    managed_database_id: int, direction: str = "up"
) -> list[dict]:
    """Igual que ``incomplete_progress_for_migration`` pero acotado a UNA BD (guard de ``stamp``)."""
    session = _session()
    try:
        rows = (
            session.query(MigrationStatementProgress)
            .filter(
                MigrationStatementProgress.managed_database_id == managed_database_id,
                MigrationStatementProgress.direction == direction,
            )
            .all()
        )
        return [
            {
                "model_migration_id": r.model_migration_id,
                "last_statement_index": r.last_statement_index,
                "total_statements": r.total_statements,
            }
            for r in rows
            if 0 < r.last_statement_index < r.total_statements
        ]
    finally:
        session.close()
