"""
Modelos del ACCESO al gateway: ``AccessGrant`` y ``UserGlobalCapability``.

OJO CON EL PLANO. Esto es el plano de CONTROL — qué puede hacer un usuario **del gateway**.
El plano GESTIONADO —qué puede hacer un usuario **del motor**— son ``PermissionProfile`` y
``Privilege``, y usan otras palabras a propósito. Ver el docstring de
``app/services/capability_catalog.py``.

POR QUÉ DOS TABLAS Y NO UNA
---------------------------
``AccessGrant`` guarda la cadena TOTALMENTE ORDENADA (``viewer ⊆ operator ⊆ owner``) con su
alcance. ``UserGlobalCapability`` guarda las ORTOGONALES (``access_admin``,
``security_officer``).

Meterlas en la misma columna ``role`` daría cinco valores de los cuales tres forman cadena y
dos no son comparables con ninguno — y ahí ``max({owner@dev, access_admin@global})`` **no
tiene valor**. La resolución de alcance del actor toma el máximo sobre los alcances, así que
sin monotonía ese máximo deja de estar definido y con él todo el modelo. Son conceptos
distintos y van en tablas distintas.

POR QUÉ ``scope_id`` ES NOT NULL CON SENTINELA 0
------------------------------------------------
En MySQL, MariaDB y PostgreSQL un índice ``UNIQUE`` **no considera dos ``NULL`` iguales**, así
que con ``scope_id`` nullable un usuario podría acumular N filas ``('global', NULL)`` con
roles distintos: el máximo operaría sobre un multiconjunto arbitrario y un ``DELETE`` de
revocación borraría filas que nadie sabía que existían.

Los índices únicos parciales no son la salida y el repo ya lo investigó: el docstring de
``app/models/environment.py`` explica por qué (MySQL 8 no los tiene y el truco funcional no
existe en MariaDB 11). El sentinela ``0`` con un ``CHECK`` es la única forma portable.
"""

from sqlalchemy import CheckConstraint, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class AccessGrant(Base, TimestampMixin):
    """
    Un rol de la cadena, con su alcance. Un usuario puede tener varios.

    El alcance por ENTORNO existe porque el caso real es "operador en desarrollo, lector en
    producción". El alcance por SERVIDOR existe porque ``environment_id`` vive solo en
    ``managed_databases``: los 33 endpoints de ``servers.py`` —incluidos ``DROP DATABASE``,
    ``DROP USER``, la consola SQL y revelar contraseñas— no tienen entorno al que anclarse, y
    sin ese eje "lector en producción" sería una promesa incumplible sobre la mitad más
    destructiva de la superficie.
    """

    __tablename__ = "access_grants"
    __table_args__ = (
        UniqueConstraint("user_id", "scope_type", "scope_id", name="uq_access_grants_scope"),
        # Sin esto, `scope_type='global'` con un `scope_id` cualquiera crearía filas que el
        # resolvedor no sabe interpretar.
        CheckConstraint(
            "(scope_type <> 'global') OR (scope_id = 0)",
            name="ck_access_grants_global_scope_id",
        ),
        {"comment": "Roles del gateway por usuario y alcance (plano de CONTROL)"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del grant"
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        # CASCADE: un alcance sin usuario es basura, no evidencia. La evidencia de qué hizo
        # esa persona vive en `audit_log`, que desnormaliza el username a propósito.
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Usuario del gateway al que se le otorga",
    )

    role: Mapped[str] = mapped_column(
        String(16), nullable=False, comment="Rol de la cadena: viewer | operator | owner"
    )

    scope_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        comment="Eje del alcance: global | environment | server",
    )

    scope_id: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        server_default="0",
        comment="Id del entorno o servidor; 0 cuando scope_type='global' (ver docstring)",
    )

    def __repr__(self) -> str:
        return (
            f"<AccessGrant(user_id={self.user_id}, role='{self.role}', "
            f"scope={self.scope_type}:{self.scope_id})>"
        )


class UserGlobalCapability(Base, TimestampMixin):
    """
    Una capacidad global y ortogonal (``access_admin`` | ``security_officer``).

    PK compuesta y no ``id`` sintético, con el criterio de ``ProjectDatabaseModel``: acá el par
    ES la identidad de la fila, y con PK compuesta un doble otorgamiento es imposible incluso
    ante un bug del controller.
    """

    __tablename__ = "user_global_capabilities"
    __table_args__ = (
        {"comment": "Capacidades globales ortogonales a la cadena de roles"},
    )

    user_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Usuario del gateway",
    )

    capability: Mapped[str] = mapped_column(
        String(32),
        primary_key=True,
        comment="access_admin | security_officer",
    )

    def __repr__(self) -> str:
        return f"<UserGlobalCapability(user_id={self.user_id}, capability='{self.capability}')>"
