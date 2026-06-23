"""
Genera la representación SQL (de REFERENCIA) del esquema de metadatos del gateway.

Compila el DDL directamente desde los modelos ORM (SQLAlchemy) para el dialecto
MySQL/MariaDB —el motor de la BD de metadatos— de modo que coincide EXACTAMENTE con
lo que define la app y las migraciones, sin transcripción a mano ni drift. NO se
conecta a ninguna base de datos.

Alembic sigue siendo la fuente de verdad de los CAMBIOS de esquema; esta carpeta es
una FOTO legible para entender la BD a nivel SQL (tablas, índices, constraints) y un
lugar natural para versionar vistas/procedimientos/triggers cuando existan.

Uso:
    uv run python database/generate_schema.py
"""

from pathlib import Path

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import Base
from app.services.db_admin.privilege_seed import privilege_seed_rows

ROOT = Path(__file__).resolve().parent / "gateway"
TABLES_DIR = ROOT / "tables"
SEEDS_DIR = ROOT / "seeds"

_DIALECT = mysql.dialect()


def _esc(value: str) -> str:
    return value.replace("'", "''")


def _header(title: str) -> str:
    return (
        f"-- {title}\n"
        "-- Generado por database/generate_schema.py desde los modelos ORM.\n"
        "-- NO editar a mano: Alembic es la fuente de verdad del esquema.\n\n"
    )


def write_tables() -> list[str]:
    TABLES_DIR.mkdir(parents=True, exist_ok=True)
    written = []
    for i, table in enumerate(Base.metadata.sorted_tables, start=1):
        ddl = str(CreateTable(table).compile(dialect=_DIALECT)).strip() + ";\n"
        for ix in sorted(table.indexes, key=lambda x: x.name or ""):
            ddl += str(CreateIndex(ix).compile(dialect=_DIALECT)).strip() + ";\n"
        path = TABLES_DIR / f"{i:02d}_{table.name}.sql"
        path.write_text(_header(f"Tabla: {table.name}") + ddl, encoding="utf-8")
        written.append(table.name)
    return written


def write_full_schema(tables: list[str]) -> None:
    parts = [_header("Esquema completo de la BD de metadatos del gateway")]
    for i, table in enumerate(Base.metadata.sorted_tables, start=1):
        parts.append(str(CreateTable(table).compile(dialect=_DIALECT)).strip() + ";\n")
        for ix in sorted(table.indexes, key=lambda x: x.name or ""):
            parts.append(str(CreateIndex(ix).compile(dialect=_DIALECT)).strip() + ";")
        parts.append("")
    (ROOT / "schema.sql").write_text("\n".join(parts) + "\n", encoding="utf-8")


def write_privilege_seed() -> int:
    SEEDS_DIR.mkdir(parents=True, exist_ok=True)
    lines = [
        _header("Llenado del catálogo `privileges`"),
        "-- El seeding REAL en runtime lo hace app/services/privilege_catalog.py",
        "-- (idempotente y preservando is_active). Esto es la referencia SQL.\n",
    ]
    cols = "`engine`,`name`,`category`,`context`,`description`,`is_sensitive`,`is_active`"
    rows = privilege_seed_rows()
    for r in rows:
        ctx = "NULL" if r["context"] is None else f"'{_esc(r['context'])}'"
        lines.append(
            f"INSERT INTO `privileges` ({cols}) VALUES "
            f"('{r['engine']}','{_esc(r['name'])}','{r['category']}',{ctx},"
            f"'{_esc(r['description'])}',{int(r['is_sensitive'])},{int(r['is_active'])});"
        )
    (SEEDS_DIR / "privileges.sql").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return len(rows)


def main() -> None:
    tables = write_tables()
    write_full_schema(tables)
    n = write_privilege_seed()
    print(f"OK: {len(tables)} tablas -> database/gateway/tables/")
    print(f"OK: esquema completo -> database/gateway/schema.sql")
    print(f"OK: {n} filas de catálogo -> database/gateway/seeds/privileges.sql")


if __name__ == "__main__":
    main()
