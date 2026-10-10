"""
Modelos de los tokens de integración: la credencial bearer con la que el proyecto web de un
usuario invoca un conjunto cerrado de operaciones de la API REST.

POR QUÉ NO REUSA ``api_tokens``
-------------------------------
Un token de agente (MCP) está atado a un ``project_id`` y su techo excluye todo lo que mute. Un
token de integración es lo contrario en las dos cosas: está atado a un USUARIO emisor (ejerce su
rol real, recortado por la lista de scopes) y existe para mutar. Mezclar los dos en una tabla
obligaría a que cada lector distinga "cuál de los dos es" antes de decidir qué puede hacer, y un
olvido en ese ``if`` es una escalada de privilegios. Tablas separadas, pepper separado
(``integration_token_pepper``) y prefijo separado (``datumint``) hacen que una credencial de un
sistema no pueda validar en el otro.

VERIFICACIÓN: HMAC-SHA256 CON PEPPER, NO ARGON2
-----------------------------------------------
Mismo razonamiento que ``ApiToken``: un secreto de 256 bits no necesita estirarse, Argon2 no es
indexable (O(N) por request, y un vector de DoS de CPU) y la verificación es un lookup por
``token_id`` más ``hmac.compare_digest``.

LAS LISTAS DE PERMITIDOS
------------------------
``integration_token_servers`` e ``integration_token_blueprints`` acotan el DESTINO del token. Un
token solo opera sobre los servidores de su lista (obligatoria para scopes de operación) y, si
su lista de blueprints no está vacía, solo asigna o aplica esos. Se guardan en tablas propias
con FK ``CASCADE`` y PK compuesta —no como texto separado por comas— para que borrar un servidor
o un blueprint limpie las listas y la base haga cumplir la integridad.
"""

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

#: Largo de ``token_id``: tiene que coincidir con ``INTEGRATION_PUBLIC_ID_LENGTH`` de
#: ``app.core.integration_token_format`` (``token_urlsafe(18)`` = 24 caracteres) o el INSERT falla en
#: runtime y no al escribirlo.
TOKEN_PUBLIC_ID_COLUMN_LENGTH = 24
#: Largo del HMAC-SHA256 en hexadecimal.
SECRET_HMAC_HEX_LENGTH = 64
#: Los 10 scopes del vocabulario unidos por comas miden menos de 200; el margen cubre el
#: vocabulario ampliado de la fase de operaciones destructivas sin otra migración.
SCOPES_COLUMN_LENGTH = 512


class IntegrationToken(Base, TimestampMixin):
    __tablename__ = "integration_tokens"
    __table_args__ = (
        {
            "comment": (
                "Tokens bearer de la API de integración. Atados a un usuario emisor: ejercen su "
                "rol real recortado por los scopes del token"
            )
        },
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del token de integración"
    )

    token_id: Mapped[str] = mapped_column(
        String(TOKEN_PUBLIC_ID_COLUMN_LENGTH),
        nullable=False,
        unique=True,
        index=True,
        comment="Identificador público del token: la parte indexable de 'datumint.<id>.<secreto>'",
    )

    secret_hmac: Mapped[str] = mapped_column(
        String(SECRET_HMAC_HEX_LENGTH),
        nullable=False,
        comment="HMAC-SHA256(pepper de integración, secreto) en hex. El secreto NUNCA se guarda",
    )

    name: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
        comment="Para qué proyecto o pipeline es. La revocación granular depende de que sea uno por uno",
    )

    scopes: Mapped[str] = mapped_column(
        String(SCOPES_COLUMN_LENGTH),
        nullable=False,
        comment=(
            "Scopes de integración separados por comas (vocabulario cerrado de "
            "integration_scope_catalog). Lo efectivo es esta lista recortada por el rol actual "
            "del emisor"
        ),
    )

    created_by_admin_id: Mapped[int] = mapped_column(
        Integer,
        # Sin FK a propósito, igual que `ApiToken.created_by_admin_id`: si el usuario desaparece
        # hay que poder renderizar "usuario eliminado (#id)" y no perder la evidencia. Indexado
        # porque el listado de "mis tokens" filtra por esta columna.
        nullable=False,
        index=True,
        comment="Usuario del gateway que emitió el token y cuyo rol ejerce. Sin FK: la fila sobrevive al usuario",
    )

    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment=(
            "UTC. NULL = token sin expiración: solo se emite si INTEGRATION_ALLOW_NON_EXPIRING_TOKENS "
            "está encendido y nunca con scopes destructivos"
        ),
    )

    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
        comment="Último uso (UTC), con escritura amortiguada a una por minuto como máximo",
    )

    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, index=True, comment="Cuándo se revocó (UTC). NULL = vivo"
    )

    revoked_by_admin_id: Mapped[int | None] = mapped_column(
        Integer,
        # Sin FK por la misma razón que `created_by_admin_id`.
        nullable=True,
        comment="Usuario del gateway que revocó el token. NULL = no revocado. Sin FK",
    )

    note: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="Nota libre del operador"
    )

    def __repr__(self) -> str:
        return f"<IntegrationToken(token_id='{self.token_id}', created_by={self.created_by_admin_id})>"


class IntegrationTokenServer(Base):
    __tablename__ = "integration_token_servers"
    __table_args__ = (
        {"comment": "Servidores sobre los que un token de integración puede operar (lista de permitidos)"},
    )

    token_pk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("integration_tokens.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Token de integración al que pertenece la entrada",
    )
    server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Servidor permitido. Si el servidor se borra, la entrada se borra",
    )


class IntegrationTokenBlueprint(Base):
    __tablename__ = "integration_token_blueprints"
    __table_args__ = (
        {
            "comment": (
                "Blueprints que un token de integración puede asignar o aplicar. Vacía = sin "
                "restricción, salvo en los tokens con scopes destructivos"
            )
        },
    )

    token_pk: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("integration_tokens.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Token de integración al que pertenece la entrada",
    )
    model_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("database_models.id", ondelete="CASCADE"),
        primary_key=True,
        comment="Blueprint (database_models.id) permitido. Si el blueprint se borra, la entrada se borra",
    )
