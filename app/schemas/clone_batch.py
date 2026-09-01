"""
Schemas Pydantic del recurso CloneBatch — clonar N bases de un servidor a otro.

**Todos los schemas de ENTRADA declaran ``extra="forbid"``**, por el mismo motivo que los del
clon individual: sin eso, un typo cae al default en silencio y el operador ejecuta algo
distinto de lo que pidió sobre bases de terceros.

A diferencia del clon individual, acá el SPEC viaja en la CREACIÓN y no en un ``preview``
aparte. El motivo es que en el lote no hay preview: el plan de cada base se resuelve cuando le
toca el turno, así que no existe un momento intermedio donde mandar el spec. Lo que se manda al
crear es la intención completa del lote, y lo que se confirma después es ese conjunto.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.schemas.clone import CloneCharsetSpec, CloneDataSpec, CloneStructureSpec
from app.services.db_admin.clone_spec import CopyIntent, DataOnExisting


# --------------------------------------------------------------------------- #
# Entrada                                                                      #
# --------------------------------------------------------------------------- #
class CloneBatchRowIn(BaseModel):
    """
    Una base del lote. Solo lleva lo que puede variar entre filas; el resto sale del perfil
    global. Sin ``clean_mode``: el lote no borra el destino, ni por fila ni globalmente.
    """

    model_config = ConfigDict(extra="forbid")

    source_database_name: str = Field(..., min_length=1, max_length=64)
    source_database_id: int | None = Field(
        None, ge=1, description="Id del inventario si el origen está adoptado; null si es crudo."
    )
    target_database_name: str | None = Field(
        None,
        min_length=1,
        max_length=64,
        description="Nombre en el destino. Si falta, se usa el mismo nombre del origen.",
    )
    target_mode: str = Field(
        "new",
        pattern="^(new|existing)$",
        description=(
            "new = el lote crea la base. existing = ya está creada, y entonces la única "
            "intención admitida es 'data_only' (el lote no borra, así que no puede emitir DDL "
            "sobre objetos que ya existen)."
        ),
    )
    overrides: dict | None = Field(
        None,
        description=(
            "Lo que esta fila le pisa al perfil global: copy_intent, structure, data, "
            "data_on_existing, target_charset, target_collation. Null = usa el perfil tal cual."
        ),
    )


class CloneBatchCreateIn(BaseModel):
    """Arma el plan del lote: los dos servidores, el perfil global y las filas."""

    model_config = ConfigDict(extra="forbid")

    source_server_id: int = Field(..., ge=1)
    target_server_id: int = Field(..., ge=1)

    copy_intent: CopyIntent = Field(
        CopyIntent.structure_and_data,
        description="Qué copiar, por defecto, en todas las filas.",
    )
    data_on_existing: DataOnExisting | None = Field(
        None,
        description=(
            "Qué hacer con las filas que ya estén en el destino. OBLIGATORIO con "
            "copy_intent='data_only'. No existe 'truncate': el lote no vacía tablas."
        ),
    )
    structure: CloneStructureSpec | None = Field(
        None, description="Selección declarativa por defecto. Null = todo el origen."
    )
    data: CloneDataSpec | None = None
    target_charset: CloneCharsetSpec | None = Field(
        None, description="Charset/collation de las bases que el lote CREE."
    )

    rows: list[CloneBatchRowIn] = Field(..., min_length=1)

    @model_validator(mode="after")
    def _targets_are_unique(self) -> "CloneBatchCreateIn":
        """
        Dos filas hacia el mismo destino es, con seguridad, un error de armado — y una
        pisaría a la otra sin que nada fallara. Se comprueba también en el controller (contra
        los nombres ya resueltos, incluido el default "mismo nombre que el origen"); acá se
        atrapa el caso explícito para devolver un 422 de forma antes de tocar nada.
        """
        vistos = set()
        for row in self.rows:
            nombre = (row.target_database_name or row.source_database_name).strip()
            if nombre in vistos:
                raise ValueError(
                    f"Dos bases del lote apuntan al mismo destino '{nombre}'. "
                    f"Cambiá uno de los dos nombres."
                )
            vistos.add(nombre)
        return self


class CloneBatchExecuteIn(BaseModel):
    """
    Confirma y ENCOLA el lote.

    Una sola confirmación para todo el lote, y el gesto es re-tipear el nombre del SERVIDOR
    destino. Con 12 bases, 12 re-tipeos se vuelven copiar y pegar sin leer, y además protegen
    el eje equivocado: en un lote el error catastrófico no es escribir mal un nombre, es que
    la lista entera apunte al servidor que no era. El otro eje lo cierra ``confirm_token``,
    que ata el conjunto exacto de filas.
    """

    model_config = ConfigDict(extra="forbid")

    confirm_server_name: str = Field(
        ..., min_length=1, description="Debe coincidir con el nombre del servidor destino."
    )
    confirm_token: str = Field(..., min_length=1, description="El token del plan del lote.")


# --------------------------------------------------------------------------- #
# Salida                                                                        #
# --------------------------------------------------------------------------- #
class CloneBatchOut(BaseModel):
    """Cabecera + estado del lote. ``counts`` es la respuesta a «¿4 de 12?»."""

    id: int
    source_server_id: int
    target_server_id: int
    copy_intent: str
    data_on_existing: str | None = None
    target_charset: str | None = None
    target_collation: str | None = None
    total: int
    confirm_token: str = Field(
        "",
        description=(
            "Se reenvía tal cual en execute. Ata el CONJUNTO ordenado de pares "
            "origen→destino: agregar, quitar o editar una fila lo invalida."
        ),
    )
    status: str = Field(
        ..., description="pending | running | done | partial | failed | interrupted | canceled"
    )
    cancel_requested: bool = False
    error: str | None = None
    counts: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Filas por estado EFECTIVO (el del job si existe, el del ítem si todavía no), más "
            "'total'. Siempre derivado: refleja el estado vivo de los hijos."
        ),
    )
    created_by_username: str | None = None
    created_at: datetime
    expires_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CloneBatchItemOut(BaseModel):
    """
    Una fila del lote. ``status`` sale del job en cuanto existe; ``error_code`` es el código
    estable del motivo, para que el cliente lo mapee a su propio texto.
    """

    id: int
    batch_id: int
    seq: int
    source_database_name: str
    source_database_id: int | None = None
    target_database_name: str
    target_mode: str
    clone_job_id: int | None = Field(
        None, description="Job que ejecutó la fila. Null = todavía no se materializó."
    )
    status: str | None = None
    phase: str | None = None
    progress: dict | None = None
    error: str | None = None
    error_code: str | None = None
    reason: str | None = Field(
        None,
        description=(
            "Solo en 'needs_manual' del reintento: por qué esta fila no se puede reintentar "
            "automáticamente."
        ),
    )
    started_at: datetime | None = None
    finished_at: datetime | None = None


class CloneBatchRetryOut(BaseModel):
    """
    Las filas no exitosas, partidas por si el destino quedó intacto o no.

    ``needs_manual`` son las que alcanzaron a copiar datos: el destino tiene filas parciales
    (la copia no es reanudable) y el lote no puede limpiarlo porque no admite modos
    destructivos. Se resuelven con el asistente de a una, que sí ofrece el reset con su
    re-tipeo del nombre.
    """

    retryable: list[CloneBatchItemOut] = Field(default_factory=list)
    needs_manual: list[CloneBatchItemOut] = Field(default_factory=list)
