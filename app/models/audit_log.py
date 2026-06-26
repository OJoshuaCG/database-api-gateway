"""
Modelo AuditLog — registro de auditoría de operaciones sensibles del gateway.

Toda operación que MUTA el inventario o que TOCA un motor destino (DDL/DCL) deja
una entrada. NUNCA almacena credenciales ni datos de negocio: solo qué acción, sobre
qué objeto, por quién, desde qué request/IP y con qué resultado.
"""

from sqlalchemy import Boolean, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AuditLog(Base, TimestampMixin):
    __tablename__ = "audit_log"
    __table_args__ = ({"comment": "Auditoría de operaciones sensibles del gateway"},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    request_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True, comment="Request ID de la operación"
    )
    admin_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="ID del admin que ejecutó la acción"
    )
    admin_username: Mapped[str | None] = mapped_column(String(128), nullable=True)

    action: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        index=True,
        comment="Acción, p. ej. 'managed_database.create'",
    )
    target_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    server_id: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)

    touched_engine: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="True si la operación ejecutó DDL/DCL en un motor destino",
    )
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="attempt | success | error"
    )
    detail: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Resumen corto SIN credenciales"
    )
    ip: Mapped[str | None] = mapped_column(String(64), nullable=True)

    # ---- Campos granulares de DCL (Plan 07 — GRANT/REVOKE) -------------------- #
    # Solo se llenan en operaciones de permisos; NULL en el resto de las acciones.
    grantee: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Beneficiario del grant (usuario@host)"
    )
    privilege: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="Privilegio(s) afectado(s), separados por coma"
    )
    object_level: Mapped[str | None] = mapped_column(
        String(32), nullable=True, comment="Nivel del objeto: database|table|column|..."
    )
    object_name: Mapped[str | None] = mapped_column(
        String(512), nullable=True, comment="Objeto destino, p. ej. 'db.tabla'"
    )
    with_grant_option: Mapped[bool | None] = mapped_column(
        Boolean, nullable=True, comment="True si el GRANT incluyó WITH GRANT OPTION"
    )
    grantor: Mapped[str | None] = mapped_column(
        String(255), nullable=True, comment="Credencial del gateway que ejecutó el DCL"
    )

    def __repr__(self) -> str:
        return f"<AuditLog(id={self.id}, action='{self.action}', status='{self.status}')>"
