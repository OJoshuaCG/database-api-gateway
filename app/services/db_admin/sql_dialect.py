"""
Utilidades de dialecto SQL para las migraciones de blueprints.

Tres piezas, todas sin estado y sin tocar ningún motor:

- ``split_sql_statements``: separa un script multi-sentencia en sentencias
  individuales, respetando comillas, comentarios y dollar-quoting de PostgreSQL.
  Necesario porque PyMySQL ejecuta UNA sentencia por ``execute()`` y porque cada
  ``op.execute`` de Alembic debe recibir una sola sentencia.

- ``SqlTranslator``: auto-traduce el ``up_sql`` base (dialecto de referencia: MySQL)
  al motor destino con sqlglot. Devuelve ``None`` si sqlglot no puede transpilar
  (el llamador cae al SQL base / override manual).

- ``RollbackGenerator``: infiere el ``down_sql`` para operaciones ADITIVAS simples
  (CREATE TABLE/INDEX/VIEW, ADD COLUMN). Devuelve ``None`` si alguna sentencia es
  destructiva o no invertible — nunca adivina un rollback que pueda perder datos.

LIMITACIÓN documentada: el splitter no interpreta bloques ``BEGIN…END`` de rutinas
MySQL con ``;`` internos (esas deben subirse con cuidado, una por migración). El
dollar-quoting de PostgreSQL (``$$…$$``) sí se respeta, por lo que las funciones PG
se separan correctamente.
"""

from __future__ import annotations

import sqlglot
from sqlglot import exp

from app.models.enums import EngineType

# Dialecto de negocio -> dialecto de sqlglot.
_SQLGLOT_DIALECT = {
    EngineType.mysql: "mysql",
    EngineType.mariadb: "mysql",
    EngineType.postgresql: "postgres",
}

# El ``up_sql`` base se escribe en estilo MySQL (dialecto de referencia).
_REFERENCE_DIALECT = "mysql"


def split_sql_statements(sql: str) -> list[str]:
    """
    Divide un script SQL en sentencias por ``;`` de nivel superior.

    Respeta: literales ``'…'`` y ``"…"``, identificadores ``` `…` ```, dollar-quoting
    ``$tag$…$tag$`` (PostgreSQL), comentarios de línea ``--`` y ``#`` y de bloque
    ``/* … */``. Descarta sentencias vacías.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql)
    while i < n:
        ch = sql[i]
        nxt = sql[i + 1] if i + 1 < n else ""

        # Comentario de línea: -- ... \n  o  # ... \n
        if (ch == "-" and nxt == "-") or ch == "#":
            j = sql.find("\n", i)
            if j == -1:
                buf.append(sql[i:])
                i = n
            else:
                buf.append(sql[i : j + 1])
                i = j + 1
            continue

        # Comentario de bloque: /* ... */
        if ch == "/" and nxt == "*":
            j = sql.find("*/", i + 2)
            if j == -1:
                buf.append(sql[i:])
                i = n
            else:
                buf.append(sql[i : j + 2])
                i = j + 2
            continue

        # Literal/identificador delimitado por ' " `
        if ch in ("'", '"', "`"):
            buf.append(ch)
            i += 1
            while i < n:
                buf.append(sql[i])
                if sql[i] == ch:
                    # Comilla duplicada => escape, sigue dentro del literal.
                    if i + 1 < n and sql[i + 1] == ch:
                        buf.append(sql[i + 1])
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue

        # Dollar-quoting de PostgreSQL: $tag$ ... $tag$
        if ch == "$":
            tag_end = sql.find("$", i + 1)
            if tag_end != -1 and sql[i + 1 : tag_end].replace("_", "").isalnum() or (
                tag_end == i + 1
            ):
                if tag_end != -1:
                    tag = sql[i : tag_end + 1]  # p.ej. "$$" o "$body$"
                    close = sql.find(tag, tag_end + 1)
                    if close != -1:
                        buf.append(sql[i : close + len(tag)])
                        i = close + len(tag)
                        continue

        # Fin de sentencia.
        if ch == ";":
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue

        buf.append(ch)
        i += 1

    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements


class SqlTranslator:
    """Auto-traduce el ``up_sql`` base (MySQL) al motor destino con sqlglot."""

    def translate(self, sql: str, to_engine: EngineType) -> str | None:
        """
        Devuelve el SQL transpilado al motor destino, o ``None`` si sqlglot falla.

        Para MySQL/MariaDB devuelve el SQL base sin tocar (es el dialecto de
        referencia): así se preserva exactamente lo que escribió el admin.
        """
        if to_engine in (EngineType.mysql, EngineType.mariadb):
            return sql
        write = _SQLGLOT_DIALECT.get(to_engine)
        if write is None:
            return None
        try:
            parts = sqlglot.transpile(sql, read=_REFERENCE_DIALECT, write=write)
        except sqlglot.errors.SqlglotError:
            return None
        if not parts:
            return None
        return ";\n".join(p.strip() for p in parts if p.strip())

    def translate_all(self, sql: str) -> dict[str, str]:
        """
        Traduce a todos los motores soportados para mostrar en la API.
        Solo incluye las traducciones que sqlglot pudo producir.
        """
        out: dict[str, str] = {}
        for engine in (EngineType.mysql, EngineType.postgresql):
            translated = self.translate(sql, engine)
            if translated is not None:
                out[engine.value] = translated
        return out


class RollbackGenerator:
    """Infiere ``down_sql`` para operaciones aditivas; ``None`` si no es seguro."""

    def generate(self, up_sql: str) -> str | None:
        """
        Genera el rollback (en estilo MySQL de referencia) invirtiendo cada sentencia
        en ORDEN INVERSO. Si CUALQUIER sentencia no es invertible de forma segura,
        devuelve ``None`` (no se arriesga un rollback parcial o destructivo).
        """
        statements = split_sql_statements(up_sql)
        if not statements:
            return None

        reversed_stmts: list[str] = []
        for stmt in statements:
            try:
                parsed = sqlglot.parse_one(stmt, read=_REFERENCE_DIALECT)
            except sqlglot.errors.SqlglotError:
                return None
            inverse = self._invert(parsed)
            if inverse is None:
                return None
            reversed_stmts.append(inverse)

        reversed_stmts.reverse()
        return ";\n".join(reversed_stmts) + ";"

    def _invert(self, node: exp.Expression) -> str | None:
        if isinstance(node, exp.Create):
            return self._invert_create(node)
        if isinstance(node, exp.Alter):
            return self._invert_alter(node)
        # DROP / INSERT / UPDATE / DELETE / TRUNCATE / etc.: no invertible sin pérdida.
        return None

    def _invert_create(self, node: exp.Create) -> str | None:
        kind = (node.args.get("kind") or "").upper()
        this = node.this

        if kind == "TABLE":
            name = self._object_name(this)
            return f"DROP TABLE IF EXISTS {name}" if name else None

        if kind == "VIEW":
            name = self._object_name(this)
            return f"DROP VIEW IF EXISTS {name}" if name else None

        if kind == "INDEX":
            # this es un exp.Index con nombre y tabla.
            idx_name = None
            table_name = None
            if isinstance(this, exp.Index):
                ident = this.this
                idx_name = ident.name if ident is not None else None
                table = this.args.get("table")
                if table is not None:
                    table_name = table.name
            if not idx_name:
                return None
            # Estilo MySQL: DROP INDEX name ON table (la traducción a PG quita el ON).
            if table_name:
                return f"DROP INDEX {idx_name} ON {table_name}"
            return f"DROP INDEX {idx_name}"

        if kind in ("PROCEDURE", "FUNCTION"):
            name = self._object_name(this)
            return f"DROP {kind} IF EXISTS {name}" if name else None

        return None

    def _invert_alter(self, node: exp.Alter) -> str | None:
        kind = (node.args.get("kind") or "TABLE").upper()
        if kind != "TABLE":
            return None
        table = node.this.name if node.this is not None else None
        if not table:
            return None
        actions = node.args.get("actions") or []
        if not actions:
            return None

        inverses: list[str] = []
        for action in actions:
            # ADD COLUMN -> DROP COLUMN (única acción que invertimos con seguridad).
            if isinstance(action, exp.ColumnDef):
                inverses.append(
                    f"ALTER TABLE {table} DROP COLUMN {action.name}"
                )
            else:
                # ADD CONSTRAINT, MODIFY, DROP, RENAME, etc.: no invertible con certeza.
                return None
        return ";\n".join(inverses)

    @staticmethod
    def _object_name(this: exp.Expression | None) -> str | None:
        if this is None:
            return None
        if isinstance(this, exp.Schema):  # CREATE TABLE -> Schema(this=Table)
            this = this.this
        if isinstance(this, exp.Table):
            return this.name
        name = getattr(this, "name", None)
        return name or None
