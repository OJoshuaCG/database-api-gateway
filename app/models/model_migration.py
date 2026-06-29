"""
Modelo ModelMigration — una versión (delta) del esquema de un blueprint.

Cada ``DatabaseModel`` (blueprint) tiene una secuencia ordenada de migraciones
``0001``, ``0002``… Cada migración es un delta SQL (no el esquema completo) que el
gateway aplica sobre las BDs gestionadas que replican el blueprint, vía Alembic.

FUENTE DE VERDAD del SQL: esta tabla, en la BD de metadatos del gateway. Los
archivos de revisión de Alembic se generan a partir de aquí en tiempo de aplicación
(efímeros, reconstituibles). La versión REAL aplicada a cada BD gestionada vive en
la tabla ``_gw_v_{slug}`` que Alembic mantiene DENTRO de cada BD destino.

Cross-engine:
- ``up_sql`` es el SQL base que sube el admin (dialecto de referencia: MySQL).
- ``up_sql_mysql`` / ``up_sql_postgresql`` son OVERRIDES manuales opcionales; si no
  existen, el runner auto-traduce ``up_sql`` con sqlglot al motor destino.

Rollback:
- ``down_sql_suggested`` lo genera el gateway automáticamente (solo ops aditivas).
- ``down_sql`` es el rollback CONFIRMADO por el admin (vía PATCH); mientras sea
  ``None`` el endpoint de rollback responde 409.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ModelMigration(Base, TimestampMixin):
    __tablename__ = "model_migrations"
    __table_args__ = (
        UniqueConstraint(
            "model_id", "version", name="uq_model_migrations_model_version"
        ),
        {"comment": "Migraciones versionadas (deltas SQL) de cada blueprint"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la migración"
    )

    model_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("database_models.id", ondelete="CASCADE"),
        nullable=False,
        # Sin index propio: el UniqueConstraint(model_id, version) ya sirve los
        # filtros por model_id (prefijo izquierdo) en los 3 motores.
        comment="Blueprint al que pertenece esta migración",
    )

    version: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        comment="Versión secuencial con padding ('0001', '0002'…); se ordena NUMÉRICAMENTE",
    )

    name: Mapped[str] = mapped_column(
        String(200), nullable=False, comment="Descripción corta de la migración"
    )

    up_sql: Mapped[str] = mapped_column(
        Text, nullable=False, comment="SQL base del delta (dialecto de referencia: MySQL)"
    )

    up_sql_mysql: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Override manual para MySQL/MariaDB (opcional)"
    )

    up_sql_postgresql: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Override manual para PostgreSQL (opcional)"
    )

    down_sql_suggested: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Rollback auto-generado (solo sugerencia, no se aplica)"
    )

    down_sql: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="Rollback CONFIRMADO por el admin; si es NULL no hay rollback disponible",
    )

    checksum: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="SHA256(up_sql + variantes + down_sql + version) — detecta alteración",
    )

    # ---- Plan 09: trazabilidad de migraciones generadas por SNAPSHOT ---------- #
    source_engine: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="Motor de origen si proviene de un snapshot ('mysql'|'mariadb'|'postgresql'); NULL = portable",
    )

    is_baseline: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True si es el baseline inicial generado por snapshot (Plan 09)",
    )

    has_non_portable: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True si incluye objetos procedurales (rutinas/triggers/events) no traducibles cross-engine",
    )

    def __repr__(self) -> str:
        return (
            f"<ModelMigration(id={self.id}, model_id={self.model_id}, "
            f"version='{self.version}')>"
        )
