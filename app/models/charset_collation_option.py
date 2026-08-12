"""
Modelo CharsetCollationOption — CATÁLOGO GLOBAL de combinaciones charset/collation que el
gateway permite elegir al CREAR una base de datos.

Es una tabla de POLÍTICA (no de presentación): ``create_database`` valida contra ella antes
de tocar el motor, así que una combinación que no esté ``enabled`` no llega nunca al DDL.

Alcance GLOBAL (decisión de diseño, no por servidor):
    - MySQL/MariaDB tienen un catálogo de charsets/collations prácticamente fijo y consultable
      (``SHOW CHARACTER SET`` / ``SHOW COLLATION``), así que una lista global es fiel.
    - PostgreSQL depende del LOCALE del sistema operativo de CADA servidor, así que aquí la
      lista es solo un "menú curado": la creación real sigue validándose contra lo que el motor
      destino soporte (si el locale no existe, PostgreSQL responde "invalid locale name" y ese
      error nativo ya se traduce en ``remote_engine``; no se duplica esa lógica).

``engine_family`` agrupa MySQL y MariaDB bajo ``"mysql"`` porque comparten el mismo catálogo de
charsets/collations; PostgreSQL va aparte. Se guarda como String (no Enum de Python) siguiendo
el patrón de ``Privilege.engine``: el motor de la BD del gateway no necesita un tipo ENUM y el
valor se valida en la capa de servicio.

``collation`` es NOT NULL con centinela ``""`` (cadena vacía) para "el charset sin collation
específica". Es DELIBERADO: si fuera NULLable, la UNIQUE ``(engine_family, charset, collation)``
NO impediría duplicados — tanto MySQL/MariaDB como PostgreSQL tratan cada NULL como distinto en
un índice único —, y esta tabla es una allowlist: un duplicado silencioso ahí es un agujero de
política. La API expone ese centinela como ``null`` (ver ``app/schemas/charset_collation_option.py``).
"""

from sqlalchemy import Boolean, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CharsetCollationOption(Base, TimestampMixin):
    __tablename__ = "charset_collation_options"
    __table_args__ = (
        UniqueConstraint(
            "engine_family",
            "charset",
            "collation",
            name="uq_charset_collation_options_family_charset_collation",
        ),
        {"comment": "Catálogo global de charsets/collations habilitados para crear BDs"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    engine_family: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        index=True,
        comment="Familia de motor: mysql (cubre MySQL y MariaDB) | postgresql",
    )
    charset: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="MySQL/MariaDB: CHARACTER SET. PostgreSQL: ENCODING (p. ej. UTF8)",
    )
    collation: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        default="",
        server_default="",
        comment="MySQL/MariaDB: COLLATE. PostgreSQL: LOCALE. '' = sin collation específica",
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="Si el gateway permite elegir esta combinación al crear una BD",
    )
    is_default: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="Sugerencia por defecto de la familia (a lo sumo una True por engine_family)",
    )

    def __repr__(self) -> str:
        return (
            f"<CharsetCollationOption(family='{self.engine_family}', "
            f"charset='{self.charset}', collation='{self.collation}', "
            f"enabled={self.enabled})>"
        )
