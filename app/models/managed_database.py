"""
Modelo ManagedDatabase — base de datos real creada/gestionada en un servidor.

Reglas de negocio:
- Pertenece a EXACTAMENTE un usuario del motor (``owner_id``), del MISMO servidor.
- Puede replicar un ``DatabaseModel`` (blueprint), opcional.
- Nombre único por servidor.

El campo ``status`` refleja la consistencia entre el inventario y el motor:
``pending`` → ``active`` | ``error`` (ver ``ProvisionStatus``).
"""

from sqlalchemy import Boolean
from sqlalchemy import Enum as SQLAEnum
from sqlalchemy import ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin
from app.models.enums import ProvisionStatus


class ManagedDatabase(Base, TimestampMixin):
    __tablename__ = "managed_databases"
    __table_args__ = (
        UniqueConstraint(
            "server_id", "name", name="uq_managed_databases_server_name"
        ),
        {"comment": "Bases de datos reales gestionadas por el gateway en cada servidor"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la BD gestionada"
    )

    name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Nombre de la base de datos en el motor"
    )

    server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor donde vive la base de datos",
    )

    owner_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("server_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Usuario del motor propietario (único). RESTRICT: reasignar antes de borrar",
    )

    model_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("database_models.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment="Blueprint que replica esta BD (opcional)",
    )

    model_version: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Versión del blueprint implementada"
    )

    charset: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Charset (MySQL/MariaDB); p. ej. utf8mb4"
    )

    collation: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="Collation (MySQL/MariaDB)"
    )

    status: Mapped[ProvisionStatus] = mapped_column(
        SQLAEnum(ProvisionStatus, native_enum=False, length=20),
        nullable=False,
        default=ProvisionStatus.pending,
        server_default=ProvisionStatus.pending.value,
        comment="Estado de consistencia inventario↔motor",
    )

    notes: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Notas / detalle de error de aprovisionamiento"
    )

    # RESTRICT y no SET NULL, a diferencia de ``model_id``. Las dos columnas son FKs
    # nullable, pero apuntan a cosas distintas: ``model_id`` es un puntero de CAPACIDAD
    # (perderlo significa "esta BD no replica ningún blueprint", que es benigno), y este es
    # un puntero de POLÍTICA. Con SET NULL, borrar una fila de ``environments`` convertiría N
    # BDs de producción en BDs sin guard, y el guard estaría de acuerdo con que eso está
    # bien: la política desaparecería sin que nada falle. El criterio correcto es el de
    # ``owner_id`` (arriba): reasignar antes de borrar. La vía de retiro de un entorno que
    # todavía tiene BDs es ``is_active=false``, no el borrado.
    environment_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("environments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
        comment="Entorno de despliegue que clasifica esta BD (opcional). RESTRICT: reasignar antes de borrar",
    )

    origin: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="provisioned",
        server_default="provisioned",
        comment="Origen del registro: 'provisioned' (creada por el gateway) | 'adopted' (preexistente, adoptada — Plan 09)",
    )

    # EL EJE QUE DECIDE EL ALCANCE ES ESTE OPT-IN, no el veto de abajo. Con solo un opt-out,
    # habilitar un entorno dejaría legibles TODAS sus bases de golpe —incluidas las que nadie
    # revisó y las que se creen después— y "activar una" obligaría a ir a bloquear N a mano,
    # invirtiendo el trabajo y dejando el default del lado permisivo. El default-deny existiría
    # una sola vez, al nivel del entorno, y de ahí en adelante el sistema sería default-allow.
    agent_access_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="Opt-in POR BASE para agentes (MCP). Nace en false: cada activación es explícita",
    )

    # Veto de emergencia: el bloqueo gana sobre el permiso. **No tiene override**: ni `force`
    # —que es override de cuarentena y nada más, como documenta CLAUDE.md— ni nada. Un agente no
    # tiene manera de elevar.
    agent_access_blocked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="Veto de emergencia para agentes. Gana sobre agent_access_allowed. Sin override",
    )

    def __repr__(self) -> str:
        return (
            f"<ManagedDatabase(id={self.id}, name='{self.name}', "
            f"server_id={self.server_id}, status='{self.status}')>"
        )
