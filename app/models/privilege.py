"""
Modelo Privilege — CATÁLOGO de privilegios soportados por cada motor.

Es una tabla de REFERENCIA/presentación: enumera, por motor, los privilegios que
existen y cuáles la plataforma realmente controla (``is_active``). Sirve para
responder "¿qué permisos activos tiene MySQL/PostgreSQL?" sin exponer los que nunca
se tocan, evitando confusiones o asignaciones indebidas.

IMPORTANTE (seguridad): esta tabla NO es la autoridad anti-inyección. La validación
de qué token es válido y a qué nivel (y la clasificación ALLOW/GATE/DENY) vive en
``app/services/db_admin/privileges.py`` como un set CERRADO en código. Esta tabla se
SIEMBRA desde ese catálogo (ver app/services/privilege_catalog.py) y solo añade la
metadata operativa: descripción, contexto y el flag de activación.
"""

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class Privilege(Base, TimestampMixin):
    __tablename__ = "privileges"
    __table_args__ = (
        UniqueConstraint("engine", "name", name="uq_privileges_engine_name"),
        {"comment": "Catálogo de privilegios soportados por cada motor de BD"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    engine: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
        comment="Motor que admite el privilegio: mysql | mariadb | postgresql",
    )
    name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Token del privilegio, p. ej. SELECT, CREATE VIEW, GRANT OPTION",
    )
    category: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="object",
        server_default="object",
        comment="object = otorgable sobre objetos; admin = global/servidor",
    )
    context: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
        comment="Niveles donde aplica (informativo), p. ej. 'Tables,Columns'",
    )
    description: Mapped[str] = mapped_column(
        String(255), nullable=False, comment="Qué permite el privilegio (breve)"
    )
    is_sensitive: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="Requiere confirmación extra al otorgar (ALL, GRANT OPTION, MAINTAIN)",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        comment="Si la plataforma controla/expone este privilegio",
    )

    def __repr__(self) -> str:
        return (
            f"<Privilege(engine='{self.engine}', name='{self.name}', "
            f"active={self.is_active})>"
        )
