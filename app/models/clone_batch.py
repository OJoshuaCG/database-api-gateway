"""
Modelos ``CloneBatch`` / ``CloneBatchItem`` — clonar VARIAS bases de datos de un servidor a
otro en un solo gesto del operador.

QUÉ PROBLEMA RESUELVE
---------------------
El módulo de clonado resuelve muy bien UNA base. Mover N bases de un servidor a otro era
repetir el asistente N veces: sin estado agregado, sin confirmación única, sin control de
concurrencia y sin forma de reintentar solo lo que falló. El lote es la capa de orquestación
que faltaba; **no reimplementa nada del pipeline**: cada fila termina siendo un ``CloneJob``
real, con su snapshot, su fingerprint, su advisory lock y sus ítems.

POR QUÉ EL ÍTEM DECLARA LA INTENCIÓN Y NO ES YA UN ``CloneJob``
---------------------------------------------------------------
El lote arma el plan de cada base **cuando le toca el turno**, no por adelantado. Las dos
razones son de fondo:

1. ``CloneJob.source_fingerprint`` es NOT NULL y exige un snapshot real del origen. Crear los
   N jobs al planear obligaría a fotografiar N bases antes de que el operador confirme.
2. Un lote de varias bases con datos puede correr durante horas. Un plan congelado al empezar
   describiría el origen de hace seis horas, y además los últimos jobs vencerían por
   ``CLONE_TTL_HOURS``. Materializando cada job justo antes de ejecutarlo, el TTL no llega a
   correr nunca y el DDL se calcula contra el estado real del momento.

Lo que el operador confirma, entonces, es la **intención** —el conjunto exacto de pares
origen→destino y su modo—, no el DDL. A seis horas vista es lo único honestamente confirmable.

UNA SOLA FUENTE DE VERDAD POR FILA
----------------------------------
``CloneBatchItem`` **no espeja** el estado de su job. Mientras ``clone_job_id`` sea NULL, el
estado de la fila es ``outcome``; en cuanto el job se materializa, el estado **es el del job**
y ``outcome`` queda NULL para siempre. La lectura hace ``LEFT JOIN`` y resuelve
``COALESCE(job.status, item.outcome)``. Sin copia no hay desincronización posible — que es el
modo de fallo clásico de un padre que "resume" a sus hijos.

POR QUÉ UNA TABLA DE LOTE Y NO UN ``batch_id`` SUELTO EN ``clone_jobs``
-----------------------------------------------------------------------
Mismo razonamiento que ``CollationConversionBatch``, y las tres razones siguen siendo del
LOTE y no de un job: (1) el conjunto confirmado tiene que sobrevivir para poder rechazar una
ejecución cuyo conjunto cambió; (2) ``ThreadPoolExecutor.submit`` **no propaga ContextVars**,
así que sin autoría persistida la auditoría del worker sale anónima — y este lote autoriza N
operaciones sobre bases de terceros; (3) hace falta una fila donde anclar "¿terminó el lote?"
sin que cada worker tenga que preguntarle a sus hermanos si es el último. Y acá se suma una
cuarta: una fila puede quedar SIN job (bloqueada o nunca arrancada), y eso no tiene dónde
escribirse en ``clone_jobs``.
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base, TimestampMixin

# ---- Estado del LOTE (vocabulario propio, distinto del de un job) ---------------------- #
# No se reusa el de ``CloneJob`` a propósito: un lote no es "succeeded/failed", es un
# contenedor cuyo desenlace se DERIVA de sus filas. Mantenerlos separados evita que alguien
# lea el estado del lote esperando la semántica de un job.
CLONE_BATCH_PENDING = "pending"    # planificado, todavía no confirmado/encolado
CLONE_BATCH_RUNNING = "running"    # el worker está recorriendo las filas
CLONE_BATCH_DONE = "done"          # terminaron todas sin fallos
CLONE_BATCH_PARTIAL = "partial"    # terminaron todas, con al menos una fallida o bloqueada
CLONE_BATCH_FAILED = "failed"      # el lote entero falló antes de poder recorrer las filas
CLONE_BATCH_INTERRUPTED = "interrupted"  # el proceso murió a mitad (barrido de lifespan)
CLONE_BATCH_CANCELED = "canceled"  # cancelado antes de que arrancaran las filas pendientes

CLONE_BATCH_STATUSES: tuple[str, ...] = (
    CLONE_BATCH_PENDING,
    CLONE_BATCH_RUNNING,
    CLONE_BATCH_DONE,
    CLONE_BATCH_PARTIAL,
    CLONE_BATCH_FAILED,
    CLONE_BATCH_INTERRUPTED,
    CLONE_BATCH_CANCELED,
)

# Estados TERMINALES del lote: el polling del cliente se detiene al alcanzar cualquiera.
CLONE_BATCH_TERMINAL: tuple[str, ...] = (
    CLONE_BATCH_DONE,
    CLONE_BATCH_PARTIAL,
    CLONE_BATCH_FAILED,
    CLONE_BATCH_INTERRUPTED,
    CLONE_BATCH_CANCELED,
)

# ---- ``CloneBatchItem.outcome`` — SOLO para filas que nunca llegaron a tener job -------- #
# Si la fila tiene ``clone_job_id``, el estado sale del job y esta columna queda NULL. Por eso
# acá no hay "succeeded" ni "failed": esos desenlaces solo existen si hubo job.
CLONE_BATCH_ITEM_PENDING = "pending"    # todavía no le tocó el turno
CLONE_BATCH_ITEM_BLOCKED = "blocked"    # la validación previa al job la rechazó
CLONE_BATCH_ITEM_SKIPPED = "skipped"    # el lote terminó antes de llegar a ella
CLONE_BATCH_ITEM_CANCELED = "canceled"  # cancelación cooperativa antes de crear el job

CLONE_BATCH_ITEM_OUTCOMES: tuple[str, ...] = (
    CLONE_BATCH_ITEM_PENDING,
    CLONE_BATCH_ITEM_BLOCKED,
    CLONE_BATCH_ITEM_SKIPPED,
    CLONE_BATCH_ITEM_CANCELED,
)


class CloneBatch(Base, TimestampMixin):
    __tablename__ = "clone_batches"
    __table_args__ = (
        {"comment": "Lote de clonaciones de N bases de datos de un servidor a otro"},
    )

    id: Mapped[int] = mapped_column(
        primary_key=True, autoincrement=True, comment="ID único del lote"
    )

    # ---- Los dos lados, a nivel SERVIDOR ----------------------------------------------- #
    # El lote es de servidor a servidor: la base de cada lado vive en el ítem. Se permite el
    # mismo servidor de los dos lados (clonar dentro de un servidor con otro nombre es un caso
    # legítimo); lo que se rechaza, por fila, es el par origen==destino idéntico.
    source_server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor del que salen las bases",
    )
    target_server_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("servers.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Servidor al que se copian",
    )

    # ---- Perfil global (lo que comparten todas las filas) ------------------------------ #
    # Las filas pueden pisarlo con ``CloneBatchItem.overrides``; lo que NO puede ninguna es
    # elegir un ``clean_mode`` destructivo, que el lote rechaza al planear.
    copy_intent: Mapped[str] = mapped_column(
        String(30),
        nullable=False,
        comment="structure_only | structure_and_data | data_only por defecto para las filas",
    )
    data_on_existing: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment="append | upsert. Obligatorio con copy_intent='data_only'; NULL si no aplica",
    )
    structure_spec: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment="JSON del CloneStructureSpec por defecto (NULL = clon completo de cada base)",
    )
    data_spec: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="JSON del CloneDataSpec por defecto"
    )
    target_charset: Mapped[str | None] = mapped_column(
        String(50), nullable=True, comment="Charset de las BDs destino que el lote CREE"
    )
    target_collation: Mapped[str | None] = mapped_column(
        String(100), nullable=True, comment="Collation de las BDs destino que el lote CREE"
    )

    # ---- Alcance ----------------------------------------------------------------------- #
    total: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
        comment=(
            "Cantidad de filas del lote, congelada al planear. Se persiste para que el polling "
            "pueda decir '4 de 12' sin contar filas en cada llamada"
        ),
    )

    # ---- Confirmación ------------------------------------------------------------------ #
    confirm_token: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment=(
            "Hash del CONJUNTO resuelto (servidor destino + la lista ordenada de pares "
            "origen→destino con su modo). Se recomputa server-side al ejecutar: cambiar, "
            "agregar o quitar una sola fila lo invalida"
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
        default=CLONE_BATCH_PENDING,
        server_default=CLONE_BATCH_PENDING,
        index=True,
        comment="pending | running | done | partial | failed | interrupted | canceled",
    )
    cancel_requested: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
        comment="Cancelación COOPERATIVA: el worker la consulta entre filas",
    )
    error: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Motivo acotado del fallo del LOTE (nunca el mensaje crudo del motor)",
    )

    started_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Cuándo arrancó la primera fila"
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True, comment="Cuándo terminó la última fila"
    )

    def __repr__(self) -> str:
        return (
            f"<CloneBatch(id={self.id}, {self.source_server_id}→{self.target_server_id}, "
            f"status='{self.status}', total={self.total})>"
        )


class CloneBatchItem(Base, TimestampMixin):
    """Una base del lote: la INTENCIÓN declarada, y el job que la materializó (si llegó a haberlo)."""

    __tablename__ = "clone_batch_items"
    __table_args__ = ({"comment": "Una base dentro de un lote de clonación"},)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    batch_id: Mapped[int] = mapped_column(
        Integer,
        ForeignKey("clone_batches.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
        comment="Lote al que pertenece",
    )
    seq: Mapped[int] = mapped_column(
        Integer, nullable=False, comment="Orden de ejecución dentro del lote (1..N)"
    )

    # ---- Intención declarada ----------------------------------------------------------- #
    source_database_name: Mapped[str] = mapped_column(
        String(64), nullable=False, comment="Nombre de la BD origen en el servidor de origen"
    )
    source_database_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("managed_databases.id", ondelete="SET NULL"),
        nullable=True,
        comment="BD del inventario, si el origen está adoptado (NULL si es cruda)",
    )
    target_database_name: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="Nombre de la BD destino. Por defecto el del origen; editable por fila",
    )
    target_mode: Mapped[str] = mapped_column(
        String(20), nullable=False, comment="new | existing"
    )
    overrides: Mapped[str | None] = mapped_column(
        Text,
        nullable=True,
        comment=(
            "JSON con lo que esta fila le pisa al perfil global del lote. NULL = usa el perfil "
            "tal cual, que es el caso mayoritario"
        ),
    )

    # ---- Materialización --------------------------------------------------------------- #
    clone_job_id: Mapped[int | None] = mapped_column(
        Integer,
        ForeignKey("clone_jobs.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
        comment=(
            "Job que ejecutó esta fila. Mientras sea NULL el estado de la fila es 'outcome'; "
            "en cuanto se puebla, el estado ES el del job y 'outcome' queda NULL"
        ),
    )
    outcome: Mapped[str | None] = mapped_column(
        String(20),
        nullable=True,
        comment=(
            "Estado de una fila SIN job: pending | blocked | skipped | canceled. NULL cuando "
            "hay job — para que no existan dos versiones del mismo estado"
        ),
    )
    error: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Motivo por el que la fila quedó bloqueada, antes de que hubiera job",
    )
    error_code: Mapped[str | None] = mapped_column(
        String(64),
        nullable=True,
        comment=(
            "Código estable del vocabulario 'clone.*' del motivo de bloqueo, para que el "
            "cliente lo mapee a su texto en vez de matchear prosa"
        ),
    )

    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    def __repr__(self) -> str:
        return (
            f"<CloneBatchItem(id={self.id}, batch_id={self.batch_id}, seq={self.seq}, "
            f"{self.source_database_name}→{self.target_database_name})>"
        )
