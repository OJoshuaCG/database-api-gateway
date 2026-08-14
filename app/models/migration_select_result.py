"""
Modelo MigrationSelectResult — resultados de las sentencias ``SELECT`` que se ejecutaron
dentro de una migración de blueprint, por BD gestionada / versión / dirección.

**Por qué existe.** Una migración puede necesitar *verificar* algo en la BD destino
(cuántas filas quedaron sin backfill, qué valores viola una constraint que se va a crear,
qué duplicados bloquean un UNIQUE). Hoy ese ``SELECT`` se ejecuta y su resultado se
descarta: Alembic no devuelve nada y el gateway solo reporta "aplicada / falló". El
diagnóstico obliga a ir a la BD a mano, y si la migración falló A MITAD el estado que se
quería mirar ya cambió.

**La excepción deliberada a la regla del audit log.** ``app/models/audit_log.py`` declara
que el gateway NUNCA almacena datos de negocio. Esta tabla es la PRIMERA excepción y por
eso lleva todas las salvaguardas juntas: es **opt-in por migración**
(``model_migrations.capture_selects``), exige **confirmación explícita** en el ``apply``
(``allow_result_capture``), exige **revisión previa** (``reviewed``), el payload va
**CIFRADO con la DEK** (envelope encryption — no es legible por SQL directo contra la BD
del gateway, todo acceso pasa por el endpoint auditado), la lectura se **audita
fail-closed** y las filas **expiran** (``MIGRATION_CAPTURE_TTL_HOURS``).

**Qué NO es.** No es la fuente de verdad de nada: no participa del checkpoint
(``migration_statement_progress``), no altera el conteo de sentencias, y su ausencia o su
pérdida no cambia el resultado de un ``apply``/``rollback``. Es un informe best-effort
para un humano. Ver ``app/services/db_admin/migration_results.py``.

**Durabilidad por motor** (``durability``): en MySQL/MariaDB el DDL va en AUTOCOMMIT, así
que cada captura se escribe de inmediato y nace ``committed``. En PostgreSQL la migración
corre dentro de UNA transacción: las capturas se acumulan en memoria y se vuelcan cuando
se sabe si esa transacción confirmó (``committed``) o se revirtió (``rolled_back``). Una
fila ``rolled_back`` describe datos que el motor deshizo — es información válida de
diagnóstico, pero NO refleja el estado final de la BD, y el endpoint de lectura lo avisa.
"""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.dialects.mysql import LONGTEXT
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# El payload cifrado de un resultado puede pasar los 64 KB del TEXT de MySQL (el tope real
# lo pone MIGRATION_CAPTURE_MAX_BYTES, pero el cifrado Fernet + base64 infla ~1.4×).
_BIG_TEXT = Text().with_variant(LONGTEXT(), "mysql", "mariadb")


class MigrationSelectResult(Base, TimestampMixin):
    __tablename__ = "migration_select_results"
    __table_args__ = (
        # Una fila por (BD, versión, dirección, sentencia). El upsert por esta clave es lo
        # que hace que un RESUME no duplique: la sentencia ya ejecutada no se re-ejecuta
        # (el archivo de revisión no la incluye) y su resultado anterior se conserva.
        UniqueConstraint(
            "managed_database_id",
            "model_migration_id",
            "direction",
            "statement_index",
            name="uq_msr_db_migration_direction_index",
        ),
        # Lectura CRUZADA (P1, todavía sin endpoint): "esta sentencia de esta versión, en
        # todas las BDs del blueprint". El índice se deja ahora para no tener que migrar la
        # tabla después solo por eso.
        Index(
            "ix_msr_migration_direction_index",
            "model_migration_id",
            "direction",
            "statement_index",
        ),
        {
            "comment": (
                "Resultados capturados de sentencias SELECT ejecutadas dentro de una "
                "migración de blueprint (payload CIFRADO con la DEK; opt-in + TTL)"
            )
        },
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único de la captura"
    )

    managed_database_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="CASCADE"),
        nullable=False,
        # Sin índice propio: el UniqueConstraint ya sirve el lookup por su prefijo izquierdo.
        comment="BD gestionada sobre la que se ejecutó la sentencia",
    )

    model_migration_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("model_migrations.id", ondelete="CASCADE"),
        nullable=False,
        comment="Versión de blueprint que contenía la sentencia",
    )

    direction: Mapped[str] = mapped_column(
        String(4), nullable=False, comment="'up' (apply) | 'down' (rollback)"
    )

    statement_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment=(
            "Índice 1-based ABSOLUTO de la sentencia dentro de la dirección — el MISMO que "
            "usa el checkpoint (migration_statement_progress.last_statement_index), así "
            "que un resume no desalinea las capturas"
        ),
    )

    migration_checksum: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment=(
            "Checksum de ModelMigration al capturar. Si no coincide con el actual, la "
            "captura describe un SQL que ya no existe: se reporta 'stale' (el PATCH que "
            "cambia el SQL además purga estas filas)"
        ),
    )

    sql_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="SHA256 de la sentencia capturada"
    )

    sql_text: Mapped[str] = mapped_column(
        _BIG_TEXT,
        nullable=False,
        comment=(
            "Sentencia capturada, recortada a MIGRATION_CAPTURE_SQL_MAX_CHARS y con "
            "contraseñas redactadas"
        ),
    )

    status: Mapped[str] = mapped_column(
        String(10),
        nullable=False,
        default="ok",
        server_default="ok",
        comment=(
            "'ok' | 'error'. 'error' = la sentencia SÍ se ejecutó pero su resultado no se "
            "pudo capturar (tipo no serializable, encoding roto): la migración siguió su "
            "curso normal — capturar es best-effort y nunca la aborta"
        ),
    )

    durability: Mapped[str] = mapped_column(
        String(12),
        nullable=False,
        default="unknown",
        server_default="unknown",
        comment=(
            "'committed' (MySQL/MariaDB: AUTOCOMMIT, o PostgreSQL con la transacción "
            "confirmada) | 'rolled_back' (PostgreSQL deshizo la migración: el dato es "
            "diagnóstico, NO el estado final) | 'unknown' (desenlace indeterminado)"
        ),
    )

    columns_json: Mapped[str] = mapped_column(
        _BIG_TEXT,
        nullable=False,
        comment="JSON CIFRADO (DEK) de list[str] con los nombres de columna, en orden",
    )

    rows_json: Mapped[str] = mapped_column(
        _BIG_TEXT,
        nullable=False,
        comment=(
            "JSON CIFRADO (DEK) de list[list] con las filas. Listas, NO dicts: una "
            "consulta puede devolver columnas con el mismo nombre y un dict las perdería"
        ),
    )

    row_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Filas efectivamente capturadas (tras aplicar los topes)",
    )

    truncated: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment=(
            "True si el resultado real tenía más filas/bytes que los topes. El recorte es "
            "solo de la CAPTURA: el SQL que se ejecutó nunca se reescribe"
        ),
    )

    payload_bytes: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment="Tamaño del JSON en claro antes de cifrar (control de topes)",
    )

    error_message: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "Motivo acotado si status='error'. NUNCA str(exc) del motor (podría filtrar "
            "datos o detalles internos): el detalle va al log con el Request ID"
        ),
    )

    captured_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        comment=(
            "Cuándo corrió la sentencia en el motor destino. Distinto de created_at, que "
            "es cuándo se escribió la fila en la BD del gateway (en PostgreSQL las "
            "capturas se vuelcan al terminar la transacción, no al ejecutarse)"
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<MigrationSelectResult(managed_database_id={self.managed_database_id}, "
            f"model_migration_id={self.model_migration_id}, "
            f"direction='{self.direction}', index={self.statement_index}, "
            f"rows={self.row_count})>"
        )
