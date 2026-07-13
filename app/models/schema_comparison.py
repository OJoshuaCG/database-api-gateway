"""
Modelo SchemaComparison — cabecera de una comparación estructural entre dos BDs
gestionadas (Plan diff, Fase 4).

Persistir la comparación (en vez de recalcularla o confiar en un token del cliente)
es una decisión de seguridad: el servidor es la ÚNICA fuente de verdad del SQL que
se ejecutará con la credencial pseudo-root. Guardamos:

- La DIRECCIÓN explícita: el par SOURCE (estado deseado/referencia) y TARGET (la BD
  que se modificaría). Todo el DDL derivado es "qué correr sobre TARGET para que quede
  como SOURCE". Cada lado se identifica SIEMPRE por su BD física —
  ``*_server_id`` + ``*_database_name`` (NOT NULL)— y, ADEMÁS, por su
  ``*_database_id`` (``managed_database_id``) si esa BD está registrada en el
  inventario, o ``NULL`` si es una BD cruda no gestionada. Esto permite comparar (y con
  Opción B, ejecutar sobre) cualquier BD de un servidor dado de alta, no solo las
  adoptadas/provisionadas. Una referencia cruda a una BD que YA está en el inventario
  se auto-resuelve a su ``managed_database_id`` al crear la comparación (mismo lock,
  misma cuarentena) para que nunca se trate distinto de pasar el id directamente.
- Los ``*_fingerprint``: hash estable del snapshot normalizado de cada lado al
  momento de comparar. Antes de adoptar/ejecutar se re-snapshotea y se recompara
  (anti-TOCTOU): si el esquema cambió, se rechaza (409) — sin ``force``.
- ``expires_at``: TTL. Una comparación vieja describe un estado que probablemente
  ya no exista; expira para forzar el recálculo.

Las sentencias renderizadas viven en ``SchemaComparisonItem`` (una fila por
sentencia). Cascada ON DELETE: borrar la BD gestionada limpia sus comparaciones.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class SchemaComparison(Base, TimestampMixin):
    __tablename__ = "schema_comparisons"
    __table_args__ = (
        {"comment": "Cabecera de una comparación estructural entre dos BDs gestionadas"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la comparación"
    )

    source_server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor de la BD source (siempre poblado, aun si la BD no está en inventario)",
    )

    target_server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor de la BD target (siempre poblado, aun si la BD no está en inventario)",
    )

    source_database_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Nombre de la BD source en el motor (siempre poblado)",
    )

    target_database_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Nombre de la BD target en el motor (siempre poblado)",
    )

    source_database_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="managed_database_id del source si está en el inventario; NULL si es una BD cruda no registrada",
    )

    target_database_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
        comment="managed_database_id del target si está en el inventario; NULL si es una BD cruda no registrada",
    )

    source_engine: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Motor del source al comparar ('mysql'|'mariadb'|'postgresql')",
    )

    target_engine: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="Motor del target (el DDL se renderiza para este dialecto)",
    )

    source_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA256 del snapshot normalizado del source al comparar (anti-TOCTOU)",
    )

    target_fingerprint: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA256 del snapshot normalizado del target al comparar (anti-TOCTOU)",
    )

    cross_flavor_warning: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True si es una comparación MySQL↔MariaDB (posible ruido de dialecto)",
    )

    scope_note: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Aviso de alcance (p. ej. PostgreSQL: solo schema 'public')",
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment="Vencimiento (TTL): tras expirar, adopt/execute exigen recalcular (410)",
    )

    def __repr__(self) -> str:
        return (
            f"<SchemaComparison(id={self.id}, "
            f"source={self.source_server_id}/{self.source_database_name}, "
            f"target={self.target_server_id}/{self.target_database_name})>"
        )
