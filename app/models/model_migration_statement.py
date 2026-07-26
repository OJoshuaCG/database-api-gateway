"""
Modelo ModelMigrationStatement — MANIFIESTO de sentencias de una versión de blueprint.

Es la pieza que faltaba para que un rollback sea correcto tras una aplicación PARCIAL.

**El problema.** ``model_migrations`` guarda ``up_sql`` y ``down_sql`` como dos blobs
independientes. El runner los parte con ``split_sql_statements`` y los ejecuta uno por uno
en AUTOCOMMIT (el DDL de MySQL/MariaDB no es transaccional). Si el ``apply`` de la versión
N muere en la sentencia 3 de 50:

- Alembic NUNCA escribió la versión N en ``_gw_v_{slug}`` (el stamp va al final del
  ``upgrade()``), así que el ledger sigue diciendo "estoy en N-1";
- la BD SÍ tiene, físicamente, las 3 primeras sentencias de N ya commiteadas;
- un ``rollback`` posterior arranca en N-1 y ejecuta el ``down_sql`` de N-1 contra una BD
  contaminada con parte de N — el escenario de corrupción que motivó todo esto.

Y no había forma de arreglarlo con los blobs: el ``down_sql`` es una secuencia con OTRA
cantidad de sentencias (los ítems sin reverso simplemente no aparecen), así que "deshacé
las 3 primeras del up" era ininferible.

**La solución.** Una fila por sentencia, con su reverso EMPAREJADO y su ``seq``
coincidiendo exactamente con el índice que usa el checkpoint
(``migration_statement_progress.last_statement_index``). Con eso:

- ``MigrationRunner`` genera el archivo de revisión Alembic desde el manifiesto en vez de
  re-partir un blob (sin ambigüedad de splitter, incluso con cuerpos ``BEGIN…END``);
- una aplicación parcial se puede RECONCILIAR: ejecutar el reverso de las sentencias
  1..k en orden inverso deja la BD igual a lo que el ledger de Alembic ya afirma (N-1).
  No es un ``downgrade`` de Alembic —la versión nunca se aplicó— sino una compensación.

El manifiesto es OPCIONAL: solo lo escriben los flujos que conocen el emparejamiento
sentencia↔reverso (la adopción de un diff estructural). Una migración escrita a mano
sigue funcionando como antes (todo-o-nada), sin manifiesto. Fail-closed por diseño: el
runner solo confía en el manifiesto si su ``checksum`` y su cantidad de sentencias
coinciden con la migración vigente y con el motor destino.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

_SQL_TEXT = Text().with_variant(LONGTEXT(), "mysql", "mariadb")


class ModelMigrationStatement(Base, TimestampMixin):
    __tablename__ = "model_migration_statements"
    __table_args__ = (
        UniqueConstraint("model_migration_id", "seq", name="uq_mms_migration_seq"),
        {
            "comment": (
                "Manifiesto de sentencias de una versión de blueprint: una fila por "
                "sentencia, con su reverso emparejado (habilita el rollback parcial)"
            )
        },
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la sentencia"
    )

    model_migration_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("model_migrations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Versión de blueprint a la que pertenece esta sentencia",
    )

    seq: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Índice 1-based de la sentencia dentro del up_sql. COINCIDE con "
            "migration_statement_progress.last_statement_index: es el contrato que hace "
            "posible saber qué se aplicó y qué hay que deshacer"
        ),
    )

    engine: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment=(
            "Motor para el que está renderizado este SQL (mysql|mariadb|postgresql). El "
            "manifiesto solo se usa si coincide con el motor de la BD destino: el "
            "up_sql traducido cross-engine puede no partirse en la misma cantidad de "
            "sentencias"
        ),
    )

    up_sql: Mapped[str] = mapped_column(
        _SQL_TEXT, nullable=False, comment="La sentencia tal como se ejecuta en el apply"
    )

    down_sql: Mapped[str | None] = mapped_column(
        _SQL_TEXT,
        nullable=True,
        comment=(
            "Reverso EXACTO de esta sentencia (puede ser multi-sentencia). NULL = esta "
            "sentencia no se puede deshacer automáticamente"
        ),
    )

    down_confirmed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True si el reverso es demostrablemente seguro (no pierde datos)",
    )

    object_type: Mapped[str | None] = mapped_column(
        String(40), nullable=True, comment="Tipo de objeto que toca (para reportes)"
    )

    object_name: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="Nombre del objeto que toca (para reportes)"
    )

    op_group: Mapped[str | None] = mapped_column(
        String(600),
        nullable=True,
        comment=(
            "Grupo atómico del cambio lógico: varias sentencias del mismo grupo se "
            "deshacen juntas o no se deshacen"
        ),
    )

    destructive: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True si la sentencia puede perder datos (informa la reconciliación)",
    )

    def __repr__(self) -> str:
        return (
            f"<ModelMigrationStatement(migration={self.model_migration_id}, "
            f"seq={self.seq}, {self.object_type}:{self.object_name})>"
        )
