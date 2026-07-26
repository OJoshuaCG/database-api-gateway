"""
Modelo SchemaComparisonItem — una sentencia DDL derivada de una comparación.

DECISIÓN DE MODELADO (documentada): se persiste **una fila por sentencia
renderizada**, no una por objeto del diff. ``ServerAdapter.render_diff()`` ya
aplana un ``SchemaDiff`` a una lista de ``RenderedStatement`` (cada una con su
propio ``sql``, ``down_sql`` y flags de riesgo), así que un cambio de un solo
objeto que requiere varias sentencias (p. ej. en PostgreSQL una columna
modificada emite ``ALTER … TYPE`` + ``SET DEFAULT`` + ``SET NOT NULL``) produce
N filas con el mismo ``object_type``/``object_name`` pero ``sql`` distinto.

Ventajas de esta granularidad:
- La ejecución (Opción B) corre sentencia por sentencia y guarda el resultado por
  fila (``execution_status``/``execution_error``/``executed_at``).
- El ``down_sql`` por sentencia lo provee el renderer (invierte con precisión).
- Todas las filas de un mismo objeto comparten ``risk_flags`` (los renderers
  propagan ``item.risk``), así que el filtro "excluir destructivo" nunca parte un
  cambio lógico por la mitad (o entran todas, o ninguna).

``seq`` es el orden GLOBAL de aplicación (tal como lo emite ``render_diff``, ya
ordenado por fase 1..9). Adopt/execute ordenan e iteran por ``seq``.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# El DDL renderizado puede ser grande (un CREATE TABLE con muchas columnas o un
# cuerpo procedural). LONGTEXT en MySQL/MariaDB; TEXT ya es ilimitado en PG/SQLite.
_SQL_TEXT = Text().with_variant(LONGTEXT(), "mysql", "mariadb")


class SchemaComparisonItem(Base, TimestampMixin):
    __tablename__ = "schema_comparison_items"
    __table_args__ = (
        {"comment": "Sentencia DDL derivada de una comparación (una fila por sentencia)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la sentencia"
    )

    comparison_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("schema_comparisons.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Comparación a la que pertenece esta sentencia",
    )

    seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Orden GLOBAL de ejecución. Es la ÚNICA fuente de verdad del orden: lo calcula "
            "el ordenador topológico de schema_diff, que cruza fases cuando la dependencia "
            "lo exige (una FK se crea después de la PK que necesita, aunque sea fase 3 vs 4)"
        ),
    )

    object_type: Mapped[str] = mapped_column(
        String(40),
        nullable=False,
        comment="table|column|index|foreign_key|view|routine|trigger|sequence|enum_type|…",
    )

    object_name: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
        comment="Nombre del objeto (cualificado con su tabla padre donde aplica)",
    )

    change_type: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="new | modified | dropped"
    )

    phase: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Etiqueta gruesa del pipeline (1..9), INFORMATIVA. NO ordena la ejecución: "
            "para eso está 'seq'. Ordenar por 'phase' produce un orden que el motor "
            "rechaza (una FK de fase 3 puede depender de una PK de fase 4)"
        ),
    )

    # ---- Atomicidad y dependencias (calculadas por el motor de diff) ----------- #
    op_group: Mapped[str | None] = mapped_column(
        String(600),
        nullable=True,
        index=True,
        comment=(
            "Grupo ATÓMICO ('object_type|object_name|change_type'): las varias sentencias "
            "de un mismo cambio lógico (DROP+CREATE de un índice redefinido, DROP+ADD de "
            "un PK) lo comparten. La selección se valida por grupo, no por sentencia"
        ),
    )

    depends_on: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "JSON: lista de op_group que deben ejecutarse ANTES que esta sentencia "
            "(tabla antes que su vista, columna antes que su CHECK, UNIQUE antes que la "
            "FK que la referencia). Base del cierre de dependencias de una selección "
            "parcial y del linter de orden del plan"
        ),
    )

    sql: Mapped[str] = mapped_column(
        _SQL_TEXT, nullable=False, comment="Sentencia DDL renderizada para el motor del target"
    )

    risk_flags: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment=(
            "JSON de RiskFlags (destructive/lock_heavy/data_conversion/needs_review/"
            "requires_individual_review/cross_flavor_warning/possible_rename_of)"
        ),
    )

    down_sql: Mapped[str | None] = mapped_column(
        _SQL_TEXT,
        nullable=True,
        comment="Reverso de esta sentencia (sugerido por el renderer); NULL si no aplica",
    )

    down_confirmed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True si el reverso es claramente seguro (aditivo) — auto-confirmable",
    )

    # ---- Resultado de ejecución (solo se llena vía Opción B ad-hoc) ----------- #
    execution_status: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Resultado de la ejecución ad-hoc: applied | failed | skipped (NULL = no ejecutada)",
    )

    execution_error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Error del motor si la sentencia falló (limpio, sin secretos)"
    )

    executed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Momento de ejecución de esta sentencia"
    )

    def __repr__(self) -> str:
        return (
            f"<SchemaComparisonItem(id={self.id}, comparison={self.comparison_id}, "
            f"seq={self.seq}, {self.object_type}:{self.change_type})>"
        )
