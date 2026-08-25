"""
Modelo ``CollationConversionBatch`` — conversión de charset/collation de TODAS las BDs de un
blueprint, en un solo gesto del operador.

QUÉ PROBLEMA RESUELVE
---------------------
Un blueprint (``DatabaseModel``) es un esquema que N bases de datos reales replican. Convertir
la collation de UNA de ellas no deja rastro en el blueprint y las otras N-1 quedan como
estaban: el módulo de conversión, hasta ahora, no miraba ``model_id`` ni una sola vez.

La tentación es materializar la conversión como una **versión de blueprint** y repartirla con
``apply``. **No sirve, y el motivo es de fondo**: el SQL de una conversión promete un resultado
que depende del estado de CADA destino. Concretamente, una versión estática no puede recrear
los objetos con la collation congelada de la hermana (necesita el cuerpo, los grants y el
DEFINER **de esa** BD, no los del origen), así que aplicarla dejaría las tablas convertidas y
las rutinas/vistas en la collation vieja — que es EXACTAMENTE el ``Illegal mix of collations``
que este módulo entero existe para evitar.

Por eso el lote **no es una migración**: es N jobs de conversión reales, uno por BD, cada uno
leyendo su propio inventario. Lo que sí puede existir después es una versión de
**contabilidad**, que se crea y se **stampea** (nunca se aplica) — ver la Fase C.

POR QUÉ HACE FALTA UNA TABLA Y NO ALCANZA UN ``batch_id`` SUELTO
----------------------------------------------------------------
Se evaluó agrupar los jobs con un simple UUID en ``collation_conversions``. No alcanza para
tres cosas, y las tres son del LOTE, no de un job:

1. **``capped``**: si el tope ``max_databases`` recortó el conjunto, eso hay que poder
   reportarlo en el polling. Calculado al planear y no persistido, el endpoint de estado no
   puede decirlo, y el operador cree que se convirtió todo.
2. **Autoría**: ``ThreadPoolExecutor.submit`` **no propaga ContextVars**, así que el worker
   corre sin ``admin`` ni Request ID. Sin persistirlos, las filas de auditoría del lote salen
   anónimas — y este lote autoriza N operaciones irreversibles sobre bases de terceros.
3. **"¿terminó el lote?"**: sin una fila donde anotarlo, cada worker tendría que consultar a
   sus hermanos y decidir si es el último. Eso es una carrera en cuanto
   ``COLLATION_CONVERSION_MAX_WORKERS`` sea > 1 (y su default de 1 es configuración, no
   invariante).

Autoría: se copia el molde de ``ExportJob.created_by_admin_id`` — ``Integer`` **sin FK** al
admin, más el username desnormalizado, para que borrar un admin no arrastre ni bloquee el
historial del lote.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# ---- Estado del LOTE (distinto del estado de cada job) --------------------------------- #
# No se reusa el vocabulario de job a propósito: un lote no es "succeeded/failed", es un
# contenedor cuyo desenlace se deriva de sus hijos. Mantenerlos separados evita que alguien
# lea el estado del lote esperando la semántica de un job.
BATCH_STATUS_PENDING = "pending"    # planificado, todavía no confirmado/encolado
BATCH_STATUS_RUNNING = "running"    # al menos un job encolado o corriendo
BATCH_STATUS_DONE = "done"          # todos los jobs terminaron sin fallos
BATCH_STATUS_FAILED = "failed"      # terminaron todos, con al menos un job fallido
BATCH_STATUS_CANCELED = "canceled"  # cancelado antes de que arrancaran los pendientes

BATCH_STATUSES: tuple[str, ...] = (
    BATCH_STATUS_PENDING,
    BATCH_STATUS_RUNNING,
    BATCH_STATUS_DONE,
    BATCH_STATUS_FAILED,
    BATCH_STATUS_CANCELED,
)


class CollationConversionBatch(Base, TimestampMixin):
    __tablename__ = "collation_conversion_batches"
    __table_args__ = (
        {"comment": "Lote de conversiones de collation sobre las BDs de un blueprint"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del lote"
    )

    model_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("database_models.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Blueprint cuyas BDs se convierten",
    )

    # ---- Objetivo del lote ------------------------------------------------------------- #
    # Mismo par para TODAS las BDs: el sentido del lote es dejarlas parejas. Se guarda para
    # poder reconstruir qué se pidió aunque los jobs se borren.
    target_charset: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment="Charset objetivo (NULL en PostgreSQL: no tiene charset por tabla)",
    )
    target_collation: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="Collation objetivo del lote"
    )

    # ---- Alcance y recorte ------------------------------------------------------------- #
    total: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
        comment="Cantidad de BDs efectivamente incluidas en el lote",
    )
    max_databases: Mapped[int] = mapped_column(
        Integer, nullable=False, default=10, server_default="10",
        comment="Tope pedido al planear",
    )
    capped: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment=(
            "True si el tope dejó BDs elegibles fuera. Se PERSISTE (y no solo se devuelve al "
            "planear) porque si no el polling no puede reportar el recorte y el operador cree "
            "que se convirtió todo el blueprint"
        ),
    )

    # ---- Confirmación ------------------------------------------------------------------ #
    confirm_token: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment=(
            "Hash del lote RESUELTO (conjunto de BDs + objetivo + planes por BD). Se recomputa "
            "server-side al ejecutar: cualquier cambio de inventario, de objetivo o del "
            "conjunto de BDs lo invalida"
        ),
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="Vencimiento del plan del lote (TTL)"
    )

    # ---- Autoría (el worker NO hereda el contexto de la request) ----------------------- #
    # ``ThreadPoolExecutor.submit`` no propaga ContextVars: sin persistir esto, las filas de
    # auditoría que escribe el worker salen con admin/IP/Request ID en NULL. Molde:
    # ``ExportJob.created_by_admin_id`` (Integer SIN FK, para no atar el historial al admin).
    created_by_admin_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="Admin que creó el lote (sin FK: historial desacoplado)"
    )
    created_by_username: Mapped[str | None] = mapped_column(
        String(128), nullable=True, comment="Username del admin (mismo ancho que audit_log)"
    )
    origin_request_id: Mapped[str | None] = mapped_column(
        String(32),
        nullable=True,
        comment="Request ID que autorizó el lote, para correlacionar la auditoría del worker",
    )

    # ---- Estado ------------------------------------------------------------------------ #
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default=BATCH_STATUS_PENDING,
        server_default=BATCH_STATUS_PENDING,
        comment="pending | running | done | failed | canceled",
    )
    error: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Motivo acotado del fallo del lote (nunca el mensaje crudo del motor)",
    )

    # ---- Versión de contabilidad (Fase C) ---------------------------------------------- #
    blueprint_version_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("model_migrations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "Versión de blueprint creada DESPUÉS del lote como contabilidad de la conversión. "
            "Se stampea en las N BDs, nunca se aplica. SET NULL: borrar la versión no debe "
            "borrar el historial del lote"
        ),
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Cuándo arrancó el primer job del lote"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Cuándo terminó el último job del lote"
    )

    def __repr__(self) -> str:
        return (
            f"<CollationConversionBatch(id={self.id}, model_id={self.model_id}, "
            f"status='{self.status}', total={self.total})>"
        )
