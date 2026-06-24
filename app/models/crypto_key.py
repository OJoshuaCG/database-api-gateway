"""
Modelo CryptoKey — clave de datos (DEK) envuelta por la KEK derivada de SECRET_KEY.

Habilita la rotación de cifrado (envelope encryption): los datos se cifran con la DEK
activa; rotar genera una DEK nueva, re-cifra los datos y marca la anterior inactiva,
todo SIN cambiar SECRET_KEY. La DEK SIEMPRE se almacena envuelta (cifrada por la KEK);
nunca en claro.
"""

from sqlalchemy import Boolean, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class CryptoKey(Base, TimestampMixin):
    __tablename__ = "crypto_keys"
    __table_args__ = (
        {"comment": "Claves de datos (DEK) envueltas por la KEK derivada de SECRET_KEY"},
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    dek_wrapped: Mapped[str] = mapped_column(
        Text, nullable=False, comment="DEK cifrada (envuelta) por la KEK. Nunca en claro."
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=True,
        server_default="1",
        index=True,
        comment="DEK activa actual (solo una activa a la vez)",
    )

    def __repr__(self) -> str:
        return f"<CryptoKey(id={self.id}, active={self.is_active})>"
