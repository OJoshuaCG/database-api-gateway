"""
Modelo DatabaseMigrationHistory — log de aplicación de migraciones por BD gestionada.

Es el ESPEJO de auditoría del gateway: registra cada intento de aplicar o revertir
una migración de blueprint sobre una BD gestionada (cuándo, resultado, duración,
error). Permite responder "¿qué BDs están atrasadas / fallaron?" sin abrir N
conexiones a los motores destino.

NO es la fuente de verdad de la versión actual de una BD: esa la mantiene Alembic en
la tabla ``_gw_v_{slug}`` DENTRO de cada BD gestionada. Aquí solo se acumula el
historial de desenlaces.
"""

from datetime import datetime

from sqlalchemy import DateTime
from sqlalchemy import Enum as SQLAEnum
from sqlalchemy import ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import MigrationStatus


class DatabaseMigrationHistory(Base, TimestampMixin):
    __tablename__ = "database_migration_history"
    __table_args__ = (
        # Índice compuesto para el patrón real de consulta del historial de una BD:
        # WHERE managed_database_id = ? ORDER BY applied_at DESC (cubre filtro + orden).
        Index(
            "ix_dmh_managed_db_applied_at",
            "managed_database_id",
            "applied_at",
        ),
        {"comment": "Historial de aplicación/rollback de migraciones por BD gestionada"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del registro de historial"
    )

    managed_database_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="CASCADE"),
        nullable=False,
        # El índice compuesto (managed_database_id, applied_at) de __table_args__
        # cubre los filtros por managed_database_id por su prefijo izquierdo.
        comment="BD gestionada sobre la que se aplicó la migración",
    )

    # ``SET NULL`` y no ``CASCADE``, y el cambio no es cosmético: ``delete_migration`` hace
    # ``session.delete(m)``, así que con CASCADE **borrar una versión de blueprint borraba su
    # historial de aplicación en las N bases**. La evidencia desaparecía con una operación de
    # mantenimiento rutinaria. Con SET NULL el evento sobrevive a su definición, y por eso
    # ``applied_version``/``applied_checksum`` de abajo importan: con la FK en NULL son lo
    # ÚNICO que queda para saber qué corrió.
    model_migration_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("model_migrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "Migración aplicada/revertida. NULL si la versión se borró del blueprint: el "
            "evento histórico sobrevive en applied_version/applied_checksum"
        ),
    )

    # La DIRECCIÓN faltaba, y su ausencia es la causa raíz de todo un módulo: sin ella una
    # fila ``applied`` de un rollback es indistinguible de un apply, así que el guard del
    # freeze no puede usar el historial como criterio y tiene que abrir conexión a CADA motor
    # para leer la versión en vivo (ver ``migration_freeze_catalog``).
    direction: Mapped[str | None] = mapped_column(
        String(4),
        nullable=True,
        comment="'up' (apply) | 'down' (rollback). NULL en filas previas a esta columna",
    )

    # Copia CONGELADA del número. ``history()`` resuelve la versión por la FK, o sea que
    # devuelve la ACTUAL: tras un renumerado, un evento de hace seis meses pasa a mostrar un
    # número que nunca tuvo. Un log cuyo contenido cambia retroactivamente no es un log.
    applied_version: Mapped[str | None] = mapped_column(
        String(10),
        nullable=True,
        comment="Versión al momento del intento (congelada: el renumerado no la mueve)",
    )

    # El ``checksum`` de ``model_migrations`` es de la DEFINICIÓN y se recalcula en cada
    # edición, así que no puede responder "¿qué texto corrió en ESTA base?". El repo ya
    # documenta que editar una versión aplicada deja una divergencia irreversible y que lo
    # único evitable es que quede en silencio; esto la vuelve detectable después, no solo
    # anunciable en el momento de editar.
    applied_checksum: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Checksum del SQL que REALMENTE corrió, no el vigente de la definición",
    )

    # Actor DESNORMALIZADO, no una FK a ``audit_log``: ``audit.record`` es best-effort (si
    # falla solo loguea), así que una FK apuntaría a una fila que puede no existir justo
    # cuando más se necesita. ``request_id`` es el que más rinde: une con ``audit_log`` y con
    # los logs HTTP sin depender de que ninguna de las dos escrituras haya sobrevivido.
    actor_type: Mapped[str | None] = mapped_column(
        String(16),
        nullable=True,
        comment="'admin' | 'api_token'. NULL en filas previas a esta columna",
    )

    actor_id: Mapped[int | None] = mapped_column(
        Integer,
        nullable=True,
        comment="ID del admin o del token que ejecutó (sin FK: el actor puede borrarse)",
    )

    actor_username: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Nombre del actor al momento del intento (desnormalizado a propósito)",
    )

    request_id: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Request ID: une con audit_log y con los logs HTTP sin depender de FKs",
    )

    applied_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="Momento en que se ejecutó el intento"
    )

    status: Mapped[MigrationStatus] = mapped_column(
        SQLAEnum(MigrationStatus, native_enum=False, length=20),
        nullable=False,
        comment="Desenlace del intento (applied | failed)",
    )

    error: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Detalle del error si status=failed (sin secretos)"
    )

    execution_ms: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Duración de la ejecución en milisegundos"
    )

    def __repr__(self) -> str:
        return (
            f"<DatabaseMigrationHistory(id={self.id}, "
            f"managed_database_id={self.managed_database_id}, status='{self.status}')>"
        )
