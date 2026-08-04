"""
Modelo QueryExecution — historial de la CONSOLA SQL.

POR QUÉ UNA TABLA PROPIA Y NO ``audit_log.detail``
--------------------------------------------------
``audit_log`` es el rastro de SEGURIDAD: filas cortas, homogéneas, con retención larga y
sin datos de negocio. El historial de la consola es otra cosa — se pagina, se filtra por
base/usuario, se vuelve a ejecutar desde la UI y guarda el SQL completo. Meterlo en
``detail`` mezclaría dos ciclos de vida y dos políticas de retención. La consola escribe
en AMBAS: ``audit_log`` (fail-closed, antes de tocar el motor) y esta tabla (resultado).

QUÉ NO SE GUARDA
----------------
- **Las filas devueltas**: son datos del usuario final; el gateway no es su custodio.
  Solo se guardan los CONTEOS.
- **Contraseñas**: ``sql_text`` pasa por ``query_policy.redact_secrets`` antes de
  persistirse, así que un ``ALTER USER … IDENTIFIED BY 'x'`` queda con ``'***'``. Se
  guarda incluso cuando el intento fue BLOQUEADO: un intento rechazado también es
  historial.
- **La credencial usada**: se guarda el NOMBRE del usuario del motor, nunca su clave.
"""

from sqlalchemy import Boolean, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

_SQL_TEXT = Text().with_variant(LONGTEXT(), "mysql", "mariadb")

# Estados posibles de una ejecución.
STATUS_SUCCESS = "success"   # todas las sentencias corrieron sin error
STATUS_ERROR = "error"       # el motor rechazó alguna sentencia (incluye falta de permisos)
STATUS_BLOCKED = "blocked"   # la política lo rechazó: nunca se tocó el motor
STATUS_PREVIEW = "preview"   # solo se analizó/estimó impacto, sin ejecutar


class QueryExecution(Base, TimestampMixin):
    __tablename__ = "query_executions"
    __table_args__ = ({"comment": "Historial de ejecuciones de la consola SQL"},)

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la ejecución"
    )

    server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor destino sobre el que se ejecutó",
    )

    database_name: Mapped[str] = mapped_column(
        String(128), nullable=False, comment="Base de datos sobre la que se ejecutó"
    )

    engine: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="Motor: mysql | mariadb | postgresql"
    )

    # --- quién ---
    admin_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="ID del admin del gateway que lanzó la consulta"
    )
    admin_username: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Usuario admin del gateway (desnormalizado)"
    )
    connection_mode: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="admin | stored | provided | impersonate",
    )
    run_as_username: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Usuario del MOTOR con el que se conectó (nunca su contraseña)",
    )
    impersonated_role: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Rol adoptado con SET ROLE (solo PostgreSQL, modo impersonate)",
    )

    # --- qué ---
    sql_text: Mapped[str] = mapped_column(
        _SQL_TEXT,
        nullable=False,
        comment="SQL enviado, con literales de contraseña redactados y recortado al tope",
    )
    sql_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, index=True, comment="SHA-256 del SQL original"
    )
    danger_level: Mapped[str] = mapped_column(
        String(10), nullable=False, comment="read | write | ddl | blocked"
    )
    statement_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Sentencias del lote"
    )

    # --- cómo ---
    read_only: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Se ejecutó dentro de una transacción de solo lectura",
    )
    dry_run: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        comment="Se ejecutó y se revirtió para medir el impacto sin persistir",
    )
    committed: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, comment="La transacción se confirmó"
    )

    # --- resultado ---
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="success | error | blocked | preview"
    )
    rows_returned: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Filas devueltas (suma del lote)"
    )
    rows_affected: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Filas afectadas (suma del lote)"
    )
    duration_ms: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="Duración total en milisegundos"
    )
    error_code: Mapped[str | None] = mapped_column(
        String(20), nullable=True, comment="errno/SQLSTATE nativo del motor, si falló"
    )
    error_message: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Mensaje del motor o motivo del bloqueo"
    )

    # --- trazabilidad de la request ---
    request_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, comment="Request ID que originó la ejecución"
    )
    ip: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="IP del cliente que lanzó la consulta"
    )

    def __repr__(self) -> str:
        return (
            f"<QueryExecution(id={self.id}, server_id={self.server_id}, "
            f"database='{self.database_name}', status='{self.status}')>"
        )
