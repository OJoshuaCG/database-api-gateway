"""
Controller de LOTES de clonación: copiar N bases de datos de un servidor a otro en un solo
gesto del operador.

Este módulo **no reimplementa el pipeline**. Orquesta: por cada fila del lote crea un
``CloneJob`` real con ``CloneController.create_plan``, le congela el spec con ``preview`` y lo
ejecuta con ``run_job`` — todo dentro del mismo hilo, en serie. Cada base clonada queda con su
snapshot, su fingerprint, su advisory lock, sus ítems y su pantalla de detalle de siempre.

Tres decisiones que gobiernan el resto del archivo
---------------------------------------------------
1. **El plan de cada fila se arma CUANDO LE TOCA EL TURNO**, no al planear el lote. Al planear
   solo se valida lo barato (identificadores, alcance, existencia del destino, coherencia del
   modo), sin fotografiar una sola base. Evita N snapshots antes de que el operador confirme,
   evita que un lote de horas venza por ``CLONE_TTL_HOURS`` a mitad de camino, y evita
   ejecutar un DDL calculado seis horas antes. Lo que el operador confirma es la INTENCIÓN
   —el conjunto exacto de pares origen→destino— que a esa escala de tiempo es lo único
   honestamente confirmable.

2. **Las filas destructivas están prohibidas.** ``clean_mode`` distinto de ``none`` da 422 al
   planear. Borrar y recrear sigue siendo un gesto de a una, con su propio re-tipeo del
   nombre de la base. La consecuencia hay que decirla en la UI: con destino EXISTENTE la única
   intención admitida es ``data_only`` (con ``structure_and_data`` los ``CREATE TABLE``
   chocarían contra las tablas que ya están), y como ``on_existing='truncate'`` todavía no
   existe, no hay "vaciar y recargar" — para un refresco total hay que borrar las bases
   destino aparte y correr el lote con ``target_mode='new'``.

3. **Una sola fuente de verdad por fila.** ``CloneBatchItem`` no espeja el estado de su job:
   mientras ``clone_job_id`` sea NULL manda ``outcome``, y en cuanto hay job manda el job.
   Ver ``_row_status_expr``.

Sobre el desenlace del lote: a diferencia de ``CollationConversionBatch``, acá el estado
terminal lo ESCRIBE el worker en vez de derivarse en cada lectura. La carrera del "¿soy el
último?" que obligó a derivarlo allá no existe acá: un lote lo recorre un único hilo de
principio a fin, así que hay exactamente un escritor. Lo que sí se deriva siempre son los
``counts``, que son la respuesta a "¿4 de 12?" y tienen que reflejar el estado vivo de los
hijos.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

from sqlalchemy import func

from app.controllers.clone_controller import CloneController
from app.controllers.common import build_target, get_server_or_404
from app.core.context import current_http_identifier
from app.core.database import Database
from app.core.environments import (
    CLONE_BATCH_MAX_ROWS,
    CLONE_TTL_HOURS,
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
)
from app.core.logger import get_logger
from app.exceptions import AppHttpException
from app.models.clone_batch import (
    CLONE_BATCH_CANCELED,
    CLONE_BATCH_DONE,
    CLONE_BATCH_INTERRUPTED,
    CLONE_BATCH_ITEM_BLOCKED,
    CLONE_BATCH_ITEM_CANCELED,
    CLONE_BATCH_ITEM_PENDING,
    CLONE_BATCH_ITEM_SKIPPED,
    CLONE_BATCH_PARTIAL,
    CLONE_BATCH_PENDING,
    CLONE_BATCH_RUNNING,
    CloneBatch,
    CloneBatchItem,
)
from app.models.clone_job import (
    CLONE_CLEAN_NONE,
    CLONE_COPY_DATA_ONLY,
    CLONE_COPY_STRUCTURE_ONLY,
    CLONE_ITEM_DATA,
    CLONE_STATUS_PENDING,
    CLONE_STATUS_RUNNING,
    CLONE_STATUS_SUCCEEDED,
    CloneJob,
    CloneJobItem,
)
from app.services import audit
from app.services.db_admin import clone_spec as cspec
from app.services.db_admin.factory import get_adapter

logger = get_logger(__name__)

def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CloneBatchController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Helpers                                                             #
    # ------------------------------------------------------------------ #
    def _batch_or_404(self, session, batch_id: int) -> CloneBatch:
        batch = session.get(CloneBatch, batch_id)
        if batch is None:
            raise AppHttpException(
                message="Lote de clonación no encontrado.",
                status_code=404,
                context={"clone_batch_id": batch_id},
            )
        return batch

    @staticmethod
    def _row_status_expr():
        """
        Estado EFECTIVO de una fila: el del job si existe, el ``outcome`` si no.

        Es la expresión que hace real la regla de "una sola fuente de verdad": el ítem nunca
        copia el estado del job, así que no hay dos versiones del mismo dato que puedan
        divergir. Se usa igual para los ``counts`` y para el listado de filas.
        """
        return func.coalesce(CloneJob.status, CloneBatchItem.outcome)

    @staticmethod
    def batch_token(target_server_id: int, rows: list[dict]) -> str:
        """
        Hash del CONJUNTO resuelto: servidor destino + la lista ORDENADA de pares
        origen→destino con su modo.

        Ata el conjunto entero y no cada fila por separado, que es justo lo que hace falta:
        con firmas por fila, agregar o quitar una base entre planificar y confirmar no
        invalidaría nada mientras las demás siguieran válidas. Por eso entran también la
        cantidad y el orden.

        Es un sha256 PERSISTIDO y recomputado server-side (mismo patrón que
        ``CloneController.clone_execution_token`` y que el lote de collation), no el HMAC
        stateless de ``app/services/confirm_token.py``: acá hay una fila donde anclar el plan
        y su TTL, y ese servicio existe para los casos que no la tienen.
        """
        parts = [str(target_server_id), str(len(rows))]
        for index, row in enumerate(rows):
            parts.append(
                f"{index}:{row['source_database_name']}:{row['target_database_name']}:"
                f"{row['target_mode']}"
            )
        return hashlib.sha256("\x1f".join(parts).encode()).hexdigest()

    # ------------------------------------------------------------------ #
    # Serialización                                                       #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _serialize_batch(batch: CloneBatch, counts: dict[str, int]) -> dict:
        return {
            "id": batch.id,
            "source_server_id": batch.source_server_id,
            "target_server_id": batch.target_server_id,
            "copy_intent": batch.copy_intent,
            "data_on_existing": batch.data_on_existing,
            "target_charset": batch.target_charset,
            "target_collation": batch.target_collation,
            "total": batch.total,
            # El token viaja en la respuesta del PLAN porque es lo que el cliente reenvía al
            # confirmar. No es un secreto: no autoriza nada por sí solo (execute exige además
            # el nombre del servidor destino re-tipeado) y se recomputa server-side.
            "confirm_token": batch.confirm_token or "",
            "status": batch.status,
            "cancel_requested": batch.cancel_requested,
            "error": batch.error,
            "counts": counts,
            "created_by_username": batch.created_by_username,
            "created_at": batch.created_at,
            "expires_at": batch.expires_at,
            "started_at": batch.started_at,
            "finished_at": batch.finished_at,
        }

    @staticmethod
    def _serialize_item(item: CloneBatchItem, job: CloneJob | None) -> dict:
        return {
            "id": item.id,
            "batch_id": item.batch_id,
            "seq": item.seq,
            "source_database_name": item.source_database_name,
            "source_database_id": item.source_database_id,
            "target_database_name": item.target_database_name,
            "target_mode": item.target_mode,
            "clone_job_id": item.clone_job_id,
            # El estado sale del job en cuanto existe; el ``outcome`` solo cubre la vida de la
            # fila ANTES de tener job.
            "status": job.status if job is not None else item.outcome,
            "phase": job.phase if job is not None else None,
            # ``CloneJob.progress`` se persiste como TEXTO JSON, no como dict: pasarlo crudo
            # rompe la validación de la respuesta (lo detectó el test del recorrido completo).
            "progress": (
                json.loads(job.progress) if job is not None and job.progress else None
            ),
            "error": (job.error if job is not None else item.error),
            "error_code": item.error_code,
            "started_at": job.started_at if job is not None else item.started_at,
            "finished_at": job.finished_at if job is not None else item.finished_at,
        }

    def _counts(self, session, batch_id: int) -> dict[str, int]:
        """Conteo por estado efectivo. Siempre derivado: es la respuesta a «¿4 de 12?»."""
        rows = (
            session.query(self._row_status_expr().label("st"), func.count().label("n"))
            .select_from(CloneBatchItem)
            .outerjoin(CloneJob, CloneJob.id == CloneBatchItem.clone_job_id)
            .filter(CloneBatchItem.batch_id == batch_id)
            .group_by("st")
            .all()
        )
        counts = {str(status or CLONE_BATCH_ITEM_PENDING): int(n) for status, n in rows}
        counts["total"] = sum(n for key, n in counts.items() if key != "total")
        return counts

    # ------------------------------------------------------------------ #
    # Crear plan del lote                                                 #
    # ------------------------------------------------------------------ #
    def create_batch_plan(self, data: dict, *, admin: dict | None = None) -> dict:
        """
        Valida lo BARATO y persiste el lote en ``pending`` con su ``confirm_token``.

        No fotografía ninguna base: el snapshot y el DDL de cada fila se calculan cuando le
        toca el turno (ver el docstring del módulo).

        Reparto de responsabilidad entre 422 y "fila bloqueada", que no es arbitrario:

        - **422 del lote entero** para lo que el operador tiene que corregir en el formulario
          antes de que exista nada: lote vacío, tope excedido, nombres destino repetidos, un
          ``clean_mode`` destructivo. Son errores de la petición, no del estado del mundo.
        - **Fila ``blocked``** para lo que depende del estado del servidor y varía por base:
          el destino ya existe (con ``target_mode='new'``), no existe (con ``'existing'``), la
          base es de sistema, el modo pedido no se puede sobre un destino existente. Se marcan
          TODAS de una vez, en vez de rebotar la petición por la primera: si no, corregir un
          lote de 12 bases son 12 viajes.

        Si NINGUNA fila queda ejecutable, sí es un 422: un lote donde no se puede clonar nada
        no tiene por qué llegar a la pantalla de confirmación.
        """
        rows_in = data.get("rows") or []
        if not rows_in:
            raise AppHttpException(
                message="El lote no tiene ninguna base seleccionada.",
                status_code=422,
                public_context={"code": cspec.CODE_BATCH_EMPTY},
            )
        if len(rows_in) > CLONE_BATCH_MAX_ROWS:
            raise AppHttpException(
                message=(
                    f"El lote tiene {len(rows_in)} bases y el tope es {CLONE_BATCH_MAX_ROWS}. "
                    f"Dividilo en lotes más chicos."
                ),
                status_code=422,
                public_context={
                    "code": cspec.CODE_BATCH_TOO_LARGE,
                    "max_rows": CLONE_BATCH_MAX_ROWS,
                    "requested": len(rows_in),
                },
            )

        copy_intent = data.get("copy_intent") or CLONE_COPY_STRUCTURE_ONLY
        on_existing = data.get("data_on_existing")
        if copy_intent == CLONE_COPY_DATA_ONLY and not on_existing:
            raise AppHttpException(
                message=(
                    "Con 'data_only' hay que decir explícitamente qué hacer con las filas que "
                    "ya estén en el destino ('append' o 'upsert')."
                ),
                status_code=422,
                public_context={"code": cspec.CODE_ON_EXISTING_REQUIRED},
            )

        # Ningún camino del lote puede borrar. Se comprueba en el perfil y en cada override.
        self._reject_destructive(data.get("clean_mode"))
        for row in rows_in:
            self._reject_destructive((row.get("overrides") or {}).get("clean_mode"))

        # Nombres destino repetidos: dos filas escribiendo la misma base es, con seguridad, un
        # error de armado — y una de las dos pisaría a la otra sin que nada fallara.
        seen: dict[str, int] = {}
        for index, row in enumerate(rows_in):
            # El default —"el mismo nombre que el origen"— se aplica ANTES de comparar. Sin
            # eso, dos filas que no nombran el destino colisionan las dos en la cadena vacía
            # y el lote se rechaza por un duplicado que no existe. Es la misma resolución que
            # hace ``_resolve_row``: dos lugares que responden lo mismo tienen que responder
            # igual.
            name = (
                row.get("target_database_name") or row.get("source_database_name") or ""
            ).strip()
            if name in seen:
                raise AppHttpException(
                    message=(
                        f"Dos bases del lote apuntan al mismo destino '{name}'. Cambiá uno de "
                        f"los dos nombres."
                    ),
                    status_code=422,
                    public_context={
                        "code": cspec.CODE_BATCH_DUPLICATE_TARGET,
                        "target_database_name": name,
                        "rows": [seen[name], index],
                    },
                )
            seen[name] = index

        session = self._session()
        try:
            source_server = get_server_or_404(session, data["source_server_id"])
            target_server = get_server_or_404(session, data["target_server_id"])
            source_target = build_target(source_server)
            target_target = build_target(target_server)
            source_engine = source_server.engine.value
            target_engine = target_server.engine.value
        finally:
            session.close()

        # UNA sola consulta por servidor, no N: preguntar la lista de bases por cada fila
        # multiplica el ida y vuelta contra el motor sin agregar información.
        live_source = set(get_adapter(source_target).list_databases())
        live_target = set(get_adapter(target_target).list_databases())

        resolved: list[dict] = []
        for index, row in enumerate(rows_in):
            resolved.append(
                self._resolve_row(
                    row,
                    index=index,
                    source_engine=source_engine,
                    target_engine=target_engine,
                    source_target=source_target,
                    target_target=target_target,
                    live_source=live_source,
                    live_target=live_target,
                    batch_intent=copy_intent,
                    same_server=source_server.id == target_server.id,
                )
            )

        if not any(r["outcome"] is None for r in resolved):
            raise AppHttpException(
                message=(
                    "Ninguna de las bases seleccionadas se puede clonar con esta "
                    "configuración. Revisá los motivos por fila."
                ),
                status_code=422,
                public_context={
                    "code": cspec.CODE_BATCH_EMPTY,
                    "blocked": [
                        {
                            "source_database_name": r["source_database_name"],
                            "code": r["error_code"],
                        }
                        for r in resolved
                    ],
                },
            )

        session = self._session()
        try:
            batch = CloneBatch(
                source_server_id=data["source_server_id"],
                target_server_id=data["target_server_id"],
                copy_intent=copy_intent,
                data_on_existing=on_existing,
                structure_spec=json.dumps(data["structure"]) if data.get("structure") else None,
                data_spec=json.dumps(data["data"]) if data.get("data") else None,
                target_charset=data.get("target_charset"),
                target_collation=data.get("target_collation"),
                total=len(resolved),
                # El token ata TODAS las filas, también las bloqueadas: el conjunto que el
                # operador confirma es exactamente el que vio en pantalla.
                confirm_token=self.batch_token(data["target_server_id"], resolved),
                expires_at=_utcnow() + timedelta(hours=CLONE_TTL_HOURS),
                created_by_admin_id=(admin or {}).get("id"),
                created_by_username=(admin or {}).get("username"),
                origin_request_id=current_http_identifier.get(),
                status=CLONE_BATCH_PENDING,
            )
            session.add(batch)
            session.flush()

            for seq, row in enumerate(resolved, start=1):
                session.add(
                    CloneBatchItem(
                        batch_id=batch.id,
                        seq=seq,
                        source_database_name=row["source_database_name"],
                        source_database_id=row["source_database_id"],
                        target_database_name=row["target_database_name"],
                        target_mode=row["target_mode"],
                        overrides=json.dumps(row["overrides"]) if row["overrides"] else None,
                        outcome=row["outcome"] or CLONE_BATCH_ITEM_PENDING,
                        error=row["error"],
                        error_code=row["error_code"],
                    )
                )
            session.commit()
            batch_id = batch.id
        finally:
            session.close()

        audit.record(
            "clone_batch.plan",
            admin=admin,
            target_type="clone_batch",
            target_id=batch_id,
            server_id=data["target_server_id"],
            detail=f"{len(resolved)} bases hacia el servidor {data['target_server_id']}",
            touched_engine=False,
        )
        return self.get_batch(batch_id)

    @staticmethod
    def _reject_destructive(clean_mode: str | None) -> None:
        """
        El lote no admite ``clean_mode`` que borre. Da 422, no una fila bloqueada.

        No es una limitación de la UI: un modo destructivo multiplicado por N bases y
        autorizado con un solo gesto es exactamente la operación que tiene que seguir siendo
        de a una, con el re-tipeo del nombre de cada base. El asistente individual la sigue
        ofreciendo.
        """
        if clean_mode in (None, CLONE_CLEAN_NONE):
            return
        raise AppHttpException(
            message=(
                "Un lote no puede borrar el destino. Para reemplazar una base existente usá "
                "el asistente de a una, que confirma el nombre exacto de esa base."
            ),
            status_code=422,
            public_context={
                "code": cspec.CODE_BATCH_DESTRUCTIVE_NOT_ALLOWED,
                "clean_mode": clean_mode,
            },
        )

    def _resolve_row(
        self,
        row: dict,
        *,
        index: int,
        source_engine: str,
        target_engine: str,
        source_target,
        target_target,
        live_source: set[str],
        live_target: set[str],
        batch_intent: str,
        same_server: bool,
    ) -> dict:
        """
        Valida UNA fila contra el estado actual de los dos servidores, sin tocar el catálogo
        de objetos. Devuelve la fila resuelta; ``outcome=None`` significa "ejecutable".

        Nunca lanza: los motivos por fila se acumulan para que el operador los vea todos
        juntos. Lo que sí lanza es el llamador, si al final no queda ninguna ejecutable.
        """
        source_name = (row.get("source_database_name") or "").strip()
        target_name = (row.get("target_database_name") or "").strip() or source_name
        target_mode = row.get("target_mode") or "new"
        overrides = row.get("overrides") or {}
        intent = overrides.get("copy_intent") or batch_intent

        resolved = {
            "source_database_name": source_name,
            "source_database_id": row.get("source_database_id"),
            "target_database_name": target_name,
            "target_mode": target_mode,
            "overrides": overrides or None,
            "outcome": None,
            "error": None,
            "error_code": None,
        }

        def block(code: str, message: str) -> dict:
            resolved["outcome"] = CLONE_BATCH_ITEM_BLOCKED
            resolved["error_code"] = code
            resolved["error"] = message
            return resolved

        # Alcance: qué bases no se pueden tocar por lo que SON. Se reusa el guard del clon
        # individual —que valida el identificador, descarta las bases reservadas del motor y
        # resuelve host+IP para no dejar tocar la propia BD de metadatos del gateway— en vez
        # de escribir un segundo criterio que se desincronice del primero. Se aplica a los DOS
        # lados: un clon LEE el origen entero.
        try:
            CloneController._validate_scope(source_name, source_engine, source_target)
            CloneController._validate_scope(target_name, target_engine, target_target)
        except AppHttpException as exc:
            return block(
                (exc.public_context or {}).get("code") or cspec.CODE_SCOPE_NOT_ALLOWED,
                exc.message,
            )

        if same_server and source_name == target_name:
            return block(
                cspec.CODE_SAME_DATABASE,
                "El origen y el destino serían la misma base de datos.",
            )
        if source_name not in live_source:
            return block(
                cspec.CODE_SOURCE_NOT_FOUND,
                f"La base origen '{source_name}' ya no existe en el servidor.",
            )

        target_exists = target_name in live_target
        if target_mode == "new" and target_exists:
            return block(
                cspec.CODE_BATCH_TARGET_EXISTS,
                (
                    f"La base '{target_name}' ya existe en el destino. Cambiá el nombre, o "
                    f"elegí 'usar la existente' y copiá solo datos."
                ),
            )
        if target_mode == "existing" and not target_exists:
            return block(
                cspec.CODE_BATCH_TARGET_MISSING,
                f"La base '{target_name}' no existe en el destino.",
            )
        # Con destino existente y sin permiso para limpiar, lo único representable es copiar
        # filas: cualquier intención con estructura emitiría CREATE TABLE contra tablas que
        # ya están. Se dice acá y no en el worker para que se vea antes de confirmar.
        if target_mode == "existing" and intent != CLONE_COPY_DATA_ONLY:
            return block(
                cspec.CODE_BATCH_EXISTING_REQUIRES_DATA_ONLY,
                (
                    f"Sobre la base existente '{target_name}' el lote solo puede copiar datos: "
                    f"un lote no borra el destino, así que la estructura tiene que estar ya "
                    f"creada."
                ),
            )
        return resolved

    # ------------------------------------------------------------------ #
    # Lectura                                                             #
    # ------------------------------------------------------------------ #
    def get_batch(self, batch_id: int) -> dict:
        session = self._session()
        try:
            batch = self._batch_or_404(session, batch_id)
            return self._serialize_batch(batch, self._counts(session, batch_id))
        finally:
            session.close()

    def list_batches(self, *, offset: int, limit: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            query = session.query(CloneBatch).order_by(CloneBatch.id.desc())
            total = query.count()
            batches = query.offset(offset).limit(limit).all()
            # Un conteo por lote: la página es chica (default 20) y la alternativa —una
            # agregación con GROUP BY sobre todos los lotes de la página— complica la consulta
            # para ahorrar unas pocas lecturas indexadas.
            return [self._serialize_batch(b, self._counts(session, b.id)) for b in batches], total
        finally:
            session.close()

    def list_items(self, batch_id: int, *, offset: int, limit: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            self._batch_or_404(session, batch_id)
            query = (
                session.query(CloneBatchItem, CloneJob)
                .outerjoin(CloneJob, CloneJob.id == CloneBatchItem.clone_job_id)
                .filter(CloneBatchItem.batch_id == batch_id)
                .order_by(CloneBatchItem.seq.asc())
            )
            total = query.count()
            rows = query.offset(offset).limit(limit).all()
            return [self._serialize_item(item, job) for item, job in rows], total
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Ejecución                                                           #
    # ------------------------------------------------------------------ #
    def execute_batch(
        self,
        batch_id: int,
        *,
        confirm_server_name: str,
        confirm_token: str,
        admin: dict | None = None,
    ) -> dict:
        """
        Valida la confirmación agregada y ENCOLA el recorrido del lote.

        La confirmación es una sola para todo el lote, y el gesto es re-tipear el nombre del
        SERVIDOR destino. Es deliberado: con 12 bases, 12 re-tipeos se vuelven copiar y pegar
        sin leer, y además protegen el eje equivocado — en un lote el error catastrófico no es
        escribir mal un nombre, es que la lista entera apunte al servidor que no era. Lo que
        cierra el otro eje es el token, que ata el conjunto exacto de filas: agregar, quitar o
        editar una sola invalida la confirmación.
        """
        from app.services import clone_batch_runner

        session = self._session()
        try:
            batch = self._batch_or_404(session, batch_id)

            if batch.status != CLONE_BATCH_PENDING:
                raise AppHttpException(
                    message=f"El lote ya no está pendiente (estado '{batch.status}').",
                    status_code=409,
                    public_context={
                        "code": cspec.CODE_BATCH_NOT_PENDING,
                        "status": batch.status,
                    },
                )
            if batch.expires_at <= _utcnow():
                raise AppHttpException(
                    message="El plan del lote expiró. Volvé a armarlo.",
                    status_code=410,
                    public_context={"code": cspec.CODE_BATCH_EXPIRED},
                )

            target_server = get_server_or_404(session, batch.target_server_id)
            if confirm_server_name.strip() != target_server.name:
                raise AppHttpException(
                    message=(
                        "El nombre del servidor destino no coincide. Escribilo exactamente "
                        "como figura en el inventario."
                    ),
                    status_code=422,
                    public_context={"code": cspec.CODE_BATCH_CONFIRM_SERVER_MISMATCH},
                )

            # El token se RECOMPUTA sobre las filas persistidas, nunca se compara contra lo
            # que mandó el cliente: si alguien editó el lote entre planificar y confirmar, el
            # recomputado no coincide con el guardado y la confirmación deja de valer.
            rows = (
                session.query(CloneBatchItem)
                .filter(CloneBatchItem.batch_id == batch_id)
                .order_by(CloneBatchItem.seq.asc())
                .all()
            )
            recomputed = self.batch_token(
                batch.target_server_id,
                [
                    {
                        "source_database_name": r.source_database_name,
                        "target_database_name": r.target_database_name,
                        "target_mode": r.target_mode,
                    }
                    for r in rows
                ],
            )
            if recomputed != (batch.confirm_token or ""):
                raise AppHttpException(
                    message="El conjunto de bases del lote cambió. Volvé a armarlo.",
                    status_code=409,
                    public_context={"code": cspec.CODE_BATCH_SET_MISMATCH},
                )
            if confirm_token != (batch.confirm_token or ""):
                raise AppHttpException(
                    message="El token de confirmación no corresponde a este lote.",
                    status_code=422,
                    public_context={"code": cspec.CODE_BATCH_TOKEN_MISMATCH},
                )
            target_server_id = batch.target_server_id
            ejecutables = sum(1 for r in rows if r.outcome == CLONE_BATCH_ITEM_PENDING)
        finally:
            session.close()

        # Auditoría de INTENCIÓN antes de encolar, fail-closed: si no se puede dejar rastro,
        # el lote no arranca. Autoriza N operaciones sobre bases de terceros.
        audit.record_intent(
            "clone_batch.execute",
            admin=admin,
            target_type="clone_batch",
            target_id=batch_id,
            server_id=target_server_id,
            detail=f"{ejecutables} bases a clonar",
        )

        # Reclamo ATÓMICO pending → running. El filtro por ``cancel_requested`` es el que hoy
        # solo tiene el lote de collation, y cubre el caso real de cancelar entre el POST de
        # execute y el arranque del worker: sin él, el lote cancelado arrancaba igual.
        session = self._session()
        try:
            claimed = (
                session.query(CloneBatch)
                .filter(
                    CloneBatch.id == batch_id,
                    CloneBatch.status == CLONE_BATCH_PENDING,
                    CloneBatch.cancel_requested.is_(False),
                )
                .update(
                    {CloneBatch.status: CLONE_BATCH_RUNNING, CloneBatch.started_at: _utcnow()},
                    synchronize_session=False,
                )
            )
            session.commit()
        finally:
            session.close()

        if not claimed:
            raise AppHttpException(
                message="El lote ya no está pendiente.",
                status_code=409,
                public_context={"code": cspec.CODE_BATCH_NOT_PENDING},
            )

        clone_batch_runner.enqueue(batch_id)
        return self.get_batch(batch_id)

    def cancel_batch(self, batch_id: int, *, admin: dict | None = None) -> dict:
        """
        Cancelación COOPERATIVA del lote.

        Marca el lote y, además, **propaga la cancelación al job de la fila en curso**. Sin esa
        propagación "cancelar" solo evitaba que arrancaran las filas siguientes, y la base que
        se estaba copiando seguía hasta el final — que en una tabla grande son horas. El job
        tiene su propio chequeo cooperativo entre tablas y entre lotes de filas.
        """
        session = self._session()
        try:
            batch = self._batch_or_404(session, batch_id)
            if batch.status not in (CLONE_BATCH_PENDING, CLONE_BATCH_RUNNING):
                raise AppHttpException(
                    message=f"El lote ya terminó (estado '{batch.status}').",
                    status_code=409,
                    public_context={
                        "code": cspec.CODE_BATCH_NOT_PENDING,
                        "status": batch.status,
                    },
                )
            batch.cancel_requested = True
            en_curso = (
                session.query(CloneBatchItem.clone_job_id)
                .join(CloneJob, CloneJob.id == CloneBatchItem.clone_job_id)
                .filter(
                    CloneBatchItem.batch_id == batch_id,
                    CloneJob.status.in_([CLONE_STATUS_PENDING, CLONE_STATUS_RUNNING]),
                )
                .all()
            )
            session.commit()
        finally:
            session.close()

        for (job_id,) in en_curso:
            CloneController().request_cancel(job_id)

        audit.record(
            "clone_batch.cancel",
            admin=admin,
            target_type="clone_batch",
            target_id=batch_id,
            detail=f"{len(en_curso)} jobs en curso marcados para cancelar",
            touched_engine=False,
        )
        return self.get_batch(batch_id)

    # ------------------------------------------------------------------ #
    # Worker: el recorrido en serie                                       #
    # ------------------------------------------------------------------ #
    def run_batch(self, batch_id: int) -> None:
        """
        Recorre las filas del lote EN SERIE. Nunca lanza: cada fila registra su desenlace y el
        recorrido sigue con la siguiente.

        Por cada fila, en este mismo hilo:
          1. ¿pidieron cancelar? → el resto queda ``canceled`` y se corta.
          2. Se re-valida lo barato contra el estado ACTUAL del destino (pasaron horas desde
             el plan: la base pudo aparecer o desaparecer).
          3. ``create_plan`` → job real, con snapshot y fingerprint frescos.
          4. ``preview`` → congela el spec en el job y emite el plan. Si trae
             ``blocking_issues``, la fila queda bloqueada, el job se cierra y el lote SIGUE.
          5. ``run_job`` **sincrónicamente**, no encolado: es lo que garantiza la serie y lo
             que evita consumir el pool de los clones sueltos.
        """
        from app.services import clone_batch_runner

        session = self._session()
        try:
            batch = session.get(CloneBatch, batch_id)
            if batch is None:
                return
            target_server_id = batch.target_server_id
            source_server_id = batch.source_server_id
            profile = {
                "copy_intent": batch.copy_intent,
                "data_on_existing": batch.data_on_existing,
                "structure": json.loads(batch.structure_spec) if batch.structure_spec else None,
                "data": json.loads(batch.data_spec) if batch.data_spec else None,
                "target_charset": batch.target_charset,
                "target_collation": batch.target_collation,
            }
            author = {
                "id": batch.created_by_admin_id,
                "username": batch.created_by_username,
            }
            item_ids = [
                row_id
                for (row_id,) in session.query(CloneBatchItem.id)
                .filter(
                    CloneBatchItem.batch_id == batch_id,
                    CloneBatchItem.outcome == CLONE_BATCH_ITEM_PENDING,
                )
                .order_by(CloneBatchItem.seq.asc())
                .all()
            ]
        finally:
            session.close()

        guard = clone_batch_runner.server_guard(target_server_id)
        with guard:
            for item_id in item_ids:
                if self._cancel_requested(batch_id):
                    self._close_remaining(batch_id, CLONE_BATCH_ITEM_CANCELED)
                    break
                try:
                    self._run_item(
                        item_id,
                        batch_id=batch_id,
                        source_server_id=source_server_id,
                        target_server_id=target_server_id,
                        profile=profile,
                        author=author,
                    )
                except Exception:  # noqa: BLE001 — una fila no puede tumbar el lote
                    logger.error(
                        "Fila %s del lote de clonación %s falló de forma inesperada",
                        item_id,
                        batch_id,
                        exc_info=True,
                    )
                    self._block_item(
                        item_id,
                        code=cspec.CODE_BATCH_ROW_BLOCKED,
                        reason="Error inesperado al preparar esta base.",
                    )

        self._finish_batch(batch_id)

    def _run_item(
        self,
        item_id: int,
        *,
        batch_id: int,
        source_server_id: int,
        target_server_id: int,
        profile: dict,
        author: dict,
    ) -> None:
        session = self._session()
        try:
            item = session.get(CloneBatchItem, item_id)
            if item is None or item.outcome != CLONE_BATCH_ITEM_PENDING:
                return
            item.started_at = _utcnow()
            source_name = item.source_database_name
            source_id = item.source_database_id
            target_name = item.target_database_name
            target_mode = item.target_mode
            overrides = json.loads(item.overrides) if item.overrides else {}
            session.commit()
        finally:
            session.close()

        controller = CloneController()

        # 1) El job, con el snapshot del origen tomado AHORA.
        create_payload = {
            "source_database_id": source_id,
            "source_server_id": None if source_id is not None else source_server_id,
            "source_database_name": None if source_id is not None else source_name,
            "target_server_id": target_server_id,
            "target_database_name": target_name,
            "target_mode": target_mode,
            # El lote no borra: está garantizado en el plan y se vuelve a fijar acá, para que
            # un override malformado no pueda colar un modo destructivo por la puerta de atrás.
            "clean_mode": CLONE_CLEAN_NONE,
            # La adopción del destino queda fuera del lote: exige un clon completo desde un
            # origen con blueprint y un owner del servidor destino por base. Es su propio ítem.
            "adopt_target": False,
            "selection": None,
        }
        try:
            summary = controller.create_plan(create_payload, admin=author)
        except AppHttpException as exc:
            self._block_item(
                item_id,
                code=(exc.public_context or {}).get("code") or cspec.CODE_BATCH_ROW_BLOCKED,
                reason=exc.message,
            )
            return

        job_id = int(summary["id"])
        # En cuanto la fila tiene job, su estado ES el del job: ``outcome`` pasa a NULL para
        # que no queden dos versiones del mismo dato.
        session = self._session()
        try:
            item = session.get(CloneBatchItem, item_id)
            if item is not None:
                item.clone_job_id = job_id
                item.outcome = None
                item.error = None
                item.error_code = None
            session.commit()
        finally:
            session.close()

        # 2) Congelar el spec en el job.
        spec, sent = self._spec_for(profile, overrides)
        try:
            preview = controller.preview(job_id, spec=spec, sent=sent)
        except AppHttpException as exc:
            controller.abort_pending_job(job_id, reason=exc.message)
            self._record_item_code(
                item_id, (exc.public_context or {}).get("code") or cspec.CODE_BATCH_ROW_BLOCKED
            )
            return

        if preview.get("blocking_issues"):
            # El esquema del destino no admite estas filas. Es información del PREVIEW, no un
            # fallo del motor: se cierra el job sin ejecutarlo y el lote sigue con la próxima.
            motivos = ", ".join(
                f"{i.get('table')}: {i.get('reason')}" for i in preview["blocking_issues"][:3]
            )
            controller.abort_pending_job(
                job_id,
                reason=f"El esquema del destino no admite la copia de datos ({motivos}).",
            )
            self._record_item_code(item_id, cspec.CODE_TARGET_SCHEMA_INCOMPATIBLE)
            return

        # 3) Ejecutar. ``run_job`` reclama el job, sostiene el advisory lock del motor durante
        #    todas las fases y nunca lanza: deja el desenlace escrito en el propio job.
        controller.run_job(job_id)
        self._stamp_item_finished(item_id)

    @staticmethod
    def _spec_for(profile: dict, overrides: dict) -> tuple[dict, set[str]]:
        """
        Spec que se le manda a ``preview``, mezclando el perfil del lote con el override de la
        fila. Devuelve también el conjunto de claves ENVIADAS, que es lo que ``preview`` usa
        para distinguir "no lo mandó" de "lo mandó en null".

        La selección de datos se deriva de la intención en vez de dejarla al cliente: con
        ``structure_and_data`` el backend exige que los datos sean un subconjunto de la
        estructura, y con ``data_only`` exige ``on_existing`` explícito. Derivarlo acá evita
        que el formulario tenga que reimplementar esas reglas y divergir.
        """
        intent = overrides.get("copy_intent") or profile["copy_intent"]
        structure = overrides.get("structure", profile["structure"])
        data = overrides.get("data", profile["data"])
        on_existing = overrides.get("data_on_existing", profile["data_on_existing"])

        spec: dict = {"copy_intent": intent}
        sent = {"copy_intent"}

        if intent != CLONE_COPY_DATA_ONLY:
            spec["structure"] = structure or {
                "mode": "all",
                "types": [],
                "names": [],
                "include_patterns": [],
                "exclude_patterns": [],
            }
            sent.add("structure")

        if intent == CLONE_COPY_STRUCTURE_ONLY:
            spec["data"] = {
                "mode": "none",
                "names": [],
                "include_patterns": [],
                "exclude_patterns": [],
                "on_existing": None,
            }
        else:
            base = data or {}
            spec["data"] = {
                "mode": base.get("mode", "all"),
                "names": base.get("names", []),
                "include_patterns": base.get("include_patterns", []),
                "exclude_patterns": base.get("exclude_patterns", []),
                # Solo tiene sentido —y solo se admite— en ``data_only``.
                "on_existing": on_existing if intent == CLONE_COPY_DATA_ONLY else None,
            }
        sent.add("data")

        charset = overrides.get("target_charset", profile["target_charset"])
        collation = overrides.get("target_collation", profile["target_collation"])
        if charset or collation:
            spec["target_charset"] = {
                "mode": "override",
                "charset": charset,
                "collation": collation,
            }
            sent.add("target_charset")
        return spec, sent

    # ------------------------------------------------------------------ #
    # Helpers de estado del worker                                        #
    # ------------------------------------------------------------------ #
    def _cancel_requested(self, batch_id: int) -> bool:
        session = self._session()
        try:
            value = (
                session.query(CloneBatch.cancel_requested)
                .filter(CloneBatch.id == batch_id)
                .scalar()
            )
            return bool(value)
        finally:
            session.close()

    def _block_item(self, item_id: int, *, code: str, reason: str) -> None:
        """Cierra una fila que nunca llegó a tener job."""
        session = self._session()
        try:
            item = session.get(CloneBatchItem, item_id)
            if item is not None:
                item.outcome = CLONE_BATCH_ITEM_BLOCKED
                item.error_code = code
                item.error = reason[:500]
                item.finished_at = _utcnow()
            session.commit()
        finally:
            session.close()

    def _record_item_code(self, item_id: int, code: str) -> None:
        """
        Anota el CÓDIGO del motivo en una fila que sí tiene job.

        No se toca ``outcome``: la fila ya tiene job, así que su estado sale del job (que
        quedó ``failed``). Lo único que se guarda acá es el código estable, que el job no
        tiene dónde llevar y que el cliente necesita para mapear el motivo a su propio texto.
        """
        session = self._session()
        try:
            item = session.get(CloneBatchItem, item_id)
            if item is not None:
                item.error_code = code
                item.finished_at = _utcnow()
            session.commit()
        finally:
            session.close()

    def _stamp_item_finished(self, item_id: int) -> None:
        session = self._session()
        try:
            item = session.get(CloneBatchItem, item_id)
            if item is not None:
                item.finished_at = _utcnow()
            session.commit()
        finally:
            session.close()

    def _close_remaining(self, batch_id: int, outcome: str) -> None:
        """Cierra las filas que nunca llegaron a arrancar (cancelación o corte del lote)."""
        session = self._session()
        try:
            session.query(CloneBatchItem).filter(
                CloneBatchItem.batch_id == batch_id,
                CloneBatchItem.outcome == CLONE_BATCH_ITEM_PENDING,
            ).update(
                {CloneBatchItem.outcome: outcome, CloneBatchItem.finished_at: _utcnow()},
                synchronize_session=False,
            )
            session.commit()
        finally:
            session.close()

    def _finish_batch(self, batch_id: int) -> None:
        """
        Escribe el desenlace del lote a partir de los estados vivos de sus filas.

        Acá el estado terminal se ESCRIBE (a diferencia del lote de collation, que lo deriva
        en cada lectura) porque la carrera que allá lo obligaba no existe: un lote lo recorre
        un único hilo de principio a fin, así que hay exactamente un escritor. Los ``counts``,
        en cambio, se siguen derivando siempre — son el estado vivo, no el desenlace.
        """
        session = self._session()
        try:
            batch = session.get(CloneBatch, batch_id)
            if batch is None:
                return
            counts = self._counts(session, batch_id)
            exitosas = counts.get(CLONE_STATUS_SUCCEEDED, 0)
            total = counts.get("total", 0)
            if batch.cancel_requested:
                batch.status = CLONE_BATCH_CANCELED
            elif exitosas == total and total > 0:
                batch.status = CLONE_BATCH_DONE
            else:
                batch.status = CLONE_BATCH_PARTIAL
            batch.finished_at = _utcnow()
            session.commit()
            estado, autor, admin_id = batch.status, batch.created_by_username, batch.created_by_admin_id
        finally:
            session.close()

        # El worker corre fuera del ciclo de request, así que el admin viene de la fila del
        # lote (que lo persistió justamente para esto) y no de los ContextVars.
        audit.record(
            "clone_batch.finish",
            admin={"id": admin_id, "username": autor} if admin_id or autor else None,
            target_type="clone_batch",
            target_id=batch_id,
            detail=f"desenlace '{estado}'",
            touched_engine=True,
        )

    # ------------------------------------------------------------------ #
    # Reintento                                                           #
    # ------------------------------------------------------------------ #
    def retry_candidates(self, batch_id: int) -> dict:
        """
        Parte las filas no exitosas en las que se pueden reintentar y las que NO.

        La regla es el DESTINO, no el estado del job: solo es reintentable la fila cuyo destino
        quedó **intacto**, porque el lote no puede limpiar nada (los modos destructivos están
        prohibidos) y por lo tanto solo sabe escribir sobre algo virgen.

        - **Reintentables**: las que nunca llegaron a tener job —bloqueadas, salteadas,
          canceladas antes de arrancar—, que es el caso que importa después de un reinicio o
          una cancelación; y las que fallaron tan temprano que no dejaron rastro en el destino.
        - **Requieren intervención manual**, por dos motivos distintos que se informan por
          separado:
          (a) **datos parciales**: la fila alcanzó a copiar filas. La copia hace commit por
              lote en AUTOCOMMIT y no es reanudable, así que reintentar agregaría encima y
              duplicaría datos en silencio.
          (b) **base creada a medias**: la fila creaba el destino (``target_mode='new'``) y el
              intento anterior alcanzó a crearlo antes de fallar. Un reintento con el mismo
              modo se bloquearía por "el destino ya existe", así que prometerlo como
              reintentable sería mentir.

        El chequeo (b) consulta el estado ACTUAL del servidor destino: sin eso, este endpoint
        y ``create_batch_plan`` contestarían distinto a la misma pregunta — el primero diría
        "se puede reintentar" y el segundo bloquearía la fila.
        """
        session = self._session()
        try:
            self._batch_or_404(session, batch_id)
            rows = (
                session.query(CloneBatchItem, CloneJob)
                .outerjoin(CloneJob, CloneJob.id == CloneBatchItem.clone_job_id)
                .filter(CloneBatchItem.batch_id == batch_id)
                .order_by(CloneBatchItem.seq.asc())
                .all()
            )
            # Jobs que llegaron a escribir datos: una sola consulta agregada, no una por fila.
            job_ids = [item.clone_job_id for item, _ in rows if item.clone_job_id is not None]
            con_datos: set[int] = set()
            if job_ids:
                con_datos = {
                    job_id
                    for (job_id,) in session.query(CloneJobItem.job_id)
                    .filter(
                        CloneJobItem.job_id.in_(job_ids),
                        CloneJobItem.kind == CLONE_ITEM_DATA,
                        CloneJobItem.rows_copied.isnot(None),
                        CloneJobItem.rows_copied > 0,
                    )
                    .distinct()
                    .all()
                }

            # Estado vivo del servidor destino: una sola consulta para todas las filas.
            batch = session.get(CloneBatch, batch_id)
            live_target: set[str] = set()
            if any(item.clone_job_id is not None for item, _ in rows):
                target_server = get_server_or_404(session, batch.target_server_id)
                live_target = set(get_adapter(build_target(target_server)).list_databases())

            retryable: list[dict] = []
            manual: list[dict] = []
            for item, job in rows:
                estado = job.status if job is not None else item.outcome
                if estado == CLONE_STATUS_SUCCEEDED:
                    continue
                payload = self._serialize_item(item, job)
                if job is None:
                    # Nunca arrancó: el destino está como estaba.
                    retryable.append(payload)
                elif job.id in con_datos:
                    payload["reason"] = (
                        "El destino quedó con datos parciales: la copia no es reanudable y el "
                        "lote no puede limpiarlo. Resolvelo con el asistente de a una, que sí "
                        "puede recrear la base."
                    )
                    manual.append(payload)
                elif item.target_mode == "new" and item.target_database_name in live_target:
                    payload["reason"] = (
                        "El intento anterior alcanzó a crear la base en el destino antes de "
                        "fallar. Borrala, o resolvelo con el asistente de a una."
                    )
                    manual.append(payload)
                else:
                    retryable.append(payload)
            return {"retryable": retryable, "needs_manual": manual}
        finally:
            session.close()

    def retry_failed(self, batch_id: int, *, admin: dict | None = None) -> dict:
        """
        Arma un lote NUEVO con las filas reintentables. Nunca reanuda el viejo.

        Un lote nuevo vuelve a pasar por la confirmación agregada, que es lo correcto: el
        estado de los servidores cambió desde el plan original, y reintentar N operaciones
        sobre bases de terceros no debería ser un click sin confirmar.
        """
        candidatos = self.retry_candidates(batch_id)
        if not candidatos["retryable"]:
            raise AppHttpException(
                message=(
                    "No hay filas reintentables en este lote. Las que fallaron dejaron datos "
                    "parciales en el destino y hay que resolverlas de a una."
                ),
                status_code=422,
                public_context={
                    "code": cspec.CODE_BATCH_RETRY_NOT_ELIGIBLE,
                    "needs_manual": [r["target_database_name"] for r in candidatos["needs_manual"]],
                },
            )

        session = self._session()
        try:
            batch = self._batch_or_404(session, batch_id)
            payload = {
                "source_server_id": batch.source_server_id,
                "target_server_id": batch.target_server_id,
                "copy_intent": batch.copy_intent,
                "data_on_existing": batch.data_on_existing,
                "structure": json.loads(batch.structure_spec) if batch.structure_spec else None,
                "data": json.loads(batch.data_spec) if batch.data_spec else None,
                "target_charset": batch.target_charset,
                "target_collation": batch.target_collation,
                "rows": [
                    {
                        "source_database_name": r["source_database_name"],
                        "source_database_id": r["source_database_id"],
                        "target_database_name": r["target_database_name"],
                        "target_mode": r["target_mode"],
                    }
                    for r in candidatos["retryable"]
                ],
            }
        finally:
            session.close()
        return self.create_batch_plan(payload, admin=admin)

    # ------------------------------------------------------------------ #
    # Barrido de arranque                                                 #
    # ------------------------------------------------------------------ #
    def sweep_interrupted(self) -> int:
        """
        Cierra los lotes que un reinicio dejó ``running`` y sus filas sin arrancar.

        **Corre DESPUÉS de ``clone_runner.sweep_interrupted``**, que es el que pasa los
        ``CloneJob`` colgados a ``interrupted``: el estado de una fila se lee del job, así que
        al revés el lote se cerraría contando filas que todavía figuran ``running``.

        **No se re-encola nada**, y es deliberado — mismo criterio que el lote de collation: el
        fingerprint del origen pudo cambiar y el token autorizaba otro conjunto. Reanudar solo
        una parte de un lote sin que un humano lo confirme es exactamente el default
        equivocado para una operación sobre bases de terceros.
        """
        session = self._session()
        try:
            colgados = [
                batch_id
                for (batch_id,) in session.query(CloneBatch.id)
                .filter(CloneBatch.status == CLONE_BATCH_RUNNING)
                .all()
            ]
            if not colgados:
                return 0
            session.query(CloneBatchItem).filter(
                CloneBatchItem.batch_id.in_(colgados),
                CloneBatchItem.outcome == CLONE_BATCH_ITEM_PENDING,
            ).update(
                {
                    CloneBatchItem.outcome: CLONE_BATCH_ITEM_SKIPPED,
                    CloneBatchItem.finished_at: _utcnow(),
                },
                synchronize_session=False,
            )
            session.query(CloneBatch).filter(CloneBatch.id.in_(colgados)).update(
                {
                    CloneBatch.status: CLONE_BATCH_INTERRUPTED,
                    CloneBatch.error: (
                        "El proceso se reinició mientras el lote estaba en ejecución."
                    ),
                    CloneBatch.finished_at: _utcnow(),
                },
                synchronize_session=False,
            )
            session.commit()
            logger.warning("Lotes de clonación marcados 'interrupted': %s", colgados)
            return len(colgados)
        finally:
            session.close()
