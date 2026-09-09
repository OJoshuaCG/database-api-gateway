"""
Modelo ApiToken — credencial de un agente (servidor MCP).

POR QUÉ HMAC-SHA256 Y NO ARGON2, AUNQUE EL RESTO DEL REPO USE ARGON2
--------------------------------------------------------------------
Argon2 saltea por hash, así que **no es indexable**: verificar un token sería O(N) verificaciones
Argon2 **por request**, y eso es a la vez lento y un vector de DoS de CPU con bearers basura.

Y no hace falta: Argon2 existe para estirar entropía baja, y un secreto de 256 bits de
``secrets.token_urlsafe`` no la tiene. La verificación es un lookup por ``token_id`` indexado más
un ``hmac.compare_digest``.

La clave del HMAC es un **pepper** derivado de ``SECRET_KEY`` con HKDF, no la DEK: rotar la DEK es
una operación de rutina (``POST /admin/crypto/rotate``) y que invalidara todos los tokens de
agente sería una caída sorpresa. Con el pepper, un dump de esta tabla **por sí solo** no alcanza
para verificar un token adivinado offline.

``project_id`` ES NOT NULL, Y NO ES UN DETALLE
----------------------------------------------
Un token sin proyecto no tiene ninguna base alcanzable, así que lo único que un ``NULL`` podría
significar es "token global" — precisamente el radio de explosión que este diseño existe para no
tener. La tabla es nueva, no hay filas que invalidar, así que nace NOT NULL y **el caso no existe
nunca**.

``RESTRICT`` y no ``CASCADE`` sobre el proyecto: borrar un proyecto no puede destruir en silencio
la evidencia de qué tokens lo alcanzaban.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin


class ApiToken(Base, TimestampMixin):
    __tablename__ = "api_tokens"
    __table_args__ = ({"comment": "Tokens de agente (servidor MCP), acotados a un proyecto"},)

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del token"
    )

    # 24 caracteres URL-safe. El número tiene que coincidir con el que emite el controller: si
    # divergen, el lookup por prefijo falla en runtime y no al escribirlo.
    token_id: Mapped[str] = mapped_column(
        String(24),
        nullable=False,
        unique=True,
        index=True,
        comment="Identificador público del token: la parte indexable de 'dbgw.<id>.<secreto>'",
    )

    secret_hmac: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="HMAC-SHA256(pepper, secreto) en hex. El secreto NUNCA se guarda",
    )

    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Para qué máquina o repo es. La revocación granular depende de que sea uno por uno",
    )

    scopes: Mapped[str] = mapped_column(
        String(255),
        nullable=False,
        comment="Vocabulario cerrado separado por comas. En v1 solo 'blueprints.read'",
    )

    project_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
        comment="Proyecto que el token alcanza. NOT NULL: un token sin proyecto sería global",
    )

    expires_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment=(
            "NOT NULL: no hay tokens perpetuos. Un token de agente vive en un .mcp.json del "
            "repo de otra gente, o sea es la credencial con más chance de terminar commiteada"
        ),
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="Último uso (UTC), con escritura amortiguada: un UPDATE por request es gratis de más",
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True, comment="Cuándo se revocó (UTC). NULL = vivo"
    )

    created_by_admin_id: Mapped[int | None] = mapped_column(
        Integer,
        # Sin FK a propósito, igual que `ExportJob.created_by_admin_id`: si el usuario
        # desaparece hay que poder renderizar "usuario eliminado (#id)" y no reventar en un
        # join ausente.
        nullable=True,
        comment="Quién lo emitió. Sin FK: la fila sobrevive al usuario",
    )

    note: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Nota libre del operador"
    )

    def __repr__(self) -> str:
        return f"<ApiToken(token_id='{self.token_id}', project_id={self.project_id})>"
