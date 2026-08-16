"""
Endpoints de exportación de bases de datos (módulo 10).

- GET  /servers/{sid}/databases/{db}/export-capabilities — qué admite ESTE motor + matriz.
- POST /servers/{sid}/databases/{db}/database-exports    — crea un PLAN (snapshotea, persiste).
- GET  /database-exports/{id}/objects                    — catálogo paginado y filtrable.
- POST /database-exports/{id}/resolve-selection          — cierre de dependencias (sin congelar).
- POST /database-exports/{id}/preview                    — congela la selección + confirm_token.
- POST /database-exports/{id}/execute                    — confirma y ENCOLA la generación.
- GET  /database-exports/{id}                            — polling del estado.
- GET  /database-exports/{id}/items                      — reporte de incidencias por objeto.
- POST /database-exports/{id}/cancel                     — cancelación cooperativa.
- GET  /database-exports/{id}/manifest                   — inventario verificable (sin abrir el archivo).
- GET  /database-exports/{id}/download                   — descarga del artefacto.
- GET  /database-exports/{id}/content                    — entrega en línea (texto plano).

La creación cuelga de ``/servers/...`` porque la BD se identifica por IDENTIDAD FÍSICA
(``server_id`` + nombre), funcione o no adoptada en el inventario — mismo patrón que
collation-conversion. Una vez que existe el job, el resto cuelga de él.

Todo detrás de ``AdminDep``. Las capacidades son una lectura barata y muy repetida por el
formulario → 30/min; la planificación toca el motor (catálogo y snapshot en solo lectura) →
10/min, igual que el clon; la ejecución y las dos entregas → **3/min**, porque son lo caro
(una lectura completa del origen) y lo sensible (la divulgación de los datos en claro).

``download`` y ``content`` son las dos ÚNICAS rutas del módulo que no devuelven
``ApiResponse``: una descarga no se anida en un envoltorio JSON (el precedente es el
``export`` de schema-comparisons) y la entrega en línea se copia al portapapeles tal cual.
"""

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, PlainTextResponse
from starlette.background import BackgroundTask

from app.controllers.export_controller import ExportController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.export import (
    ExportCapabilitiesOut,
    ExportCatalogOut,
    ExportClosureOut,
    ExportCreate,
    ExportExecuteIn,
    ExportItemOut,
    ExportManifestOut,
    ExportPreviewIn,
    ExportPreviewOut,
    ExportResolveIn,
    ExportSummaryOut,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, paginated, success

router = APIRouter(tags=["Database Exports"])


@router.get(
    "/servers/{server_id}/databases/{database}/export-capabilities",
    response_model=ApiResponse[ExportCapabilitiesOut],
)
@limiter.limit("30/minute")
def export_capabilities(request: Request, admin: AdminDep, server_id: int, database: str):
    """
    Qué puede exportarse de esta BD y con qué opciones, para ESTE motor.

    La matriz de compatibilidad que se devuelve es la misma que el servidor hace cumplir:
    el cliente puede deshabilitar controles con la certeza de que el rechazo va a coincidir.
    """
    return success(data=ExportController().capabilities(server_id, database))


@router.post(
    "/servers/{server_id}/databases/{database}/database-exports",
    response_model=ApiResponse[ExportSummaryOut],
    status_code=201,
)
@limiter.limit("10/minute")
def create_export_plan(
    request: Request,
    admin: AdminDep,
    server_id: int,
    database: str,
    payload: ExportCreate,
):
    """
    Crea el PLAN. El cuerpo es el ``ExportSpec`` completo y se persiste íntegro.

    Reenviar la misma ``idempotency_key`` con el mismo spec devuelve el plan ya creado en
    vez de disparar una segunda lectura del catálogo; con un spec distinto es 409.
    """
    result = ExportController().create_plan(
        server_id, database, payload.model_dump(mode="json"), admin=admin
    )
    return success(data=result, message="Plan de exportación creado.")


@router.get(
    "/database-exports/{job_id}/objects",
    response_model=ApiResponse[ExportCatalogOut],
)
@limiter.limit("10/minute")
def list_export_objects(
    request: Request,
    admin: AdminDep,
    job_id: int,
    pagination: PaginationDep,
    object_type: str | None = Query(
        None, description="Filtra por tipo (table, view, routine, trigger, ...)."
    ),
    name_like: str | None = Query(
        None,
        max_length=128,
        description="Subcadena del nombre, sin distinguir mayúsculas. Se filtra en memoria.",
    ),
):
    """
    Catálogo en vivo de la BD con los metadatos que informan la selección.

    No usa el envelope paginado estándar porque la respuesta lleva metadatos de catálogo
    (``scope_note``, tipos presentes, conteo por tipo) que una lista plana no transporta;
    la paginación viaja dentro del objeto.
    """
    data = ExportController().list_objects(
        job_id,
        object_type=object_type,
        name_like=name_like,
        limit=pagination.size,
        offset=pagination.offset,
    )
    return success(data=data)


@router.post(
    "/database-exports/{job_id}/resolve-selection",
    response_model=ApiResponse[ExportClosureOut],
)
@limiter.limit("10/minute")
def resolve_export_selection(
    request: Request, admin: AdminDep, job_id: int, payload: ExportResolveIn
):
    """
    Resuelve las dos selecciones y su cierre de dependencias SIN congelar nada.

    Con una selección explícita que deja fuera una dependencia responde 422 y la selección
    sugerida (nunca se recorta en silencio); con una selección automática poda y lo informa.
    """
    data = ExportController().resolve_selection(
        job_id,
        selection=(
            payload.selection.model_dump(mode="json") if payload.selection else None
        ),
        data=payload.data.model_dump(mode="json") if payload.data else None,
        auto_resolve_dependencies=payload.auto_resolve_dependencies,
    )
    return success(data=data)


@router.post(
    "/database-exports/{job_id}/preview",
    response_model=ApiResponse[ExportPreviewOut],
)
@limiter.limit("10/minute")
def preview_export(
    request: Request, admin: AdminDep, job_id: int, payload: ExportPreviewIn
):
    """
    Valida el spec entero, CONGELA la selección y emite el ``confirm_token``.

    Con ``dry_run_only`` valida y reporta sin congelar ni emitir token: es el modo "solo
    advertencias" para que el formulario muestre las consecuencias mientras se elige.
    """
    data = ExportController().preview(
        job_id,
        spec_payload=payload.spec.model_dump(mode="json") if payload.spec else None,
        auto_resolve_dependencies=payload.auto_resolve_dependencies,
        dry_run_only=payload.dry_run_only,
        include_sample=payload.include_sample,
    )
    return success(data=data)


@router.post(
    "/database-exports/{job_id}/execute",
    response_model=ApiResponse[ExportSummaryOut],
)
@limiter.limit("3/minute")
def execute_export(
    request: Request, admin: AdminDep, job_id: int, payload: ExportExecuteIn
):
    """
    Confirma el plan congelado y ENCOLA la generación del artefacto.

    Doble factor: ``confirm_target_name`` (el nombre real de la base, re-tecleado) +
    ``confirm_token`` (el hash del plan que devolvió el preview). Además se re-lee el
    catálogo y se compara el fingerprint: si el esquema cambió desde la previsualización,
    responde 409 y hay que volver a previsualizar.

    Devuelve de inmediato con el job en ``pending``/``running``: el progreso se sigue por
    ``GET /database-exports/{id}``.
    """
    data = ExportController().execute(
        job_id,
        confirm_target_name=payload.confirm_target_name,
        confirm_token=payload.confirm_token,
        admin=admin,
    )
    return success(data=data, message="Exportación encolada.")


@router.get(
    "/database-exports/{job_id}",
    response_model=ApiResponse[ExportSummaryOut],
)
def get_export(admin: AdminDep, job_id: int):
    """
    Estado del job (**polling**).

    Sin límite de tasa a propósito: es la ruta que el frontend consulta cada pocos segundos
    mientras dura la exportación, y limitarla rompería justamente el caso de uso para el que
    existe. No toca el motor ni el artefacto: solo lee la BD de metadatos.
    """
    return success(data=ExportController().get_job(job_id))


@router.get(
    "/database-exports/{job_id}/items",
    response_model=ApiResponse[list[ExportItemOut]],
)
def list_export_items(admin: AdminDep, job_id: int, pagination: PaginationDep):
    """
    Reporte de incidencias por objeto (§14): qué se exportó, qué se omitió y por qué.

    ``reason`` es siempre un motivo de vocabulario cerrado, nunca el mensaje del driver: el
    error de un motor puede incrustar valores de filas y este reporte se conserva.
    """
    items, total = ExportController().list_items(
        job_id, limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)


@router.post(
    "/database-exports/{job_id}/cancel",
    response_model=ApiResponse[ExportSummaryOut],
)
def cancel_export(admin: AdminDep, job_id: int):
    """
    Pide la cancelación COOPERATIVA: el worker corta en el próximo punto seguro, cierra la
    transacción contra el origen y descarta el artefacto parcial.

    Sin límite de tasa: detener una exportación que está degradando el origen no puede
    quedar bloqueado por una cuota.
    """
    return success(
        data=ExportController().cancel(job_id, admin=admin),
        message="Cancelación solicitada.",
    )


@router.get(
    "/database-exports/{job_id}/manifest",
    response_model=ApiResponse[ExportManifestOut],
)
def export_manifest(admin: AdminDep, job_id: int):
    """
    Inventario verificable del artefacto: checksum, tamaño, objetos, filas y ``complete``.

    Permite comprobar integridad y auditar **sin abrir el archivo** — mirar el contenido para
    saber qué se llevó sería una segunda divulgación.
    """
    return success(data=ExportController().manifest(job_id))


def _range_covers_whole_file(
    range_header: str | None,
    if_range_header: str | None,
    byte_size: int,
    sha256: str,
) -> bool:
    """
    ¿La respuesta va a entregar el archivo ENTERO, aun habiendo cabecera ``Range``?

    Función pura y aparte para poder testearla sin levantar la descarga. Devuelve ``True``
    cuando no hay ``Range``, cuando el ``If-Range`` no coincide con el ETag —en ese caso
    Starlette ignora el rango y responde 200 con todo el cuerpo— o cuando el rango pedido
    abarca de punta a punta (``bytes=0-``, ``bytes=0-<size-1>`` o un sufijo
    ``bytes=-<n>`` con ``n >= size``).

    **Fail-closed hacia NO consumir**: cualquier forma que no se entienda (multi-rango,
    unidad distinta de ``bytes``, sintaxis rara) se considera parcial. Equivocarse hacia
    "no borrar" deja un artefacto vivo hasta su TTL; equivocarse hacia "borrar" le corta
    la reanudación a un cliente legítimo y el trabajo hay que rehacerlo entero.
    """
    if not range_header:
        return True
    # ``If-Range`` que no valida ⇒ el servidor ignora el rango y manda el recurso completo.
    if if_range_header is not None and if_range_header.strip().strip("W/").strip() not in (
        f'"{sha256}"',
        sha256,
    ):
        return True

    unit, _, spec = range_header.partition("=")
    if unit.strip().lower() != "bytes" or "," in spec:
        return False
    start_raw, sep, end_raw = spec.strip().partition("-")
    if not sep:
        return False
    try:
        if not start_raw:  # sufijo: los últimos N bytes
            return int(end_raw) >= byte_size
        if int(start_raw) != 0:
            return False
        return not end_raw or int(end_raw) >= byte_size - 1
    except ValueError:
        return False


@router.get("/database-exports/{job_id}/download")
@limiter.limit("3/minute")
def download_export(request: Request, admin: AdminDep, job_id: int):
    """
    Descarga el artefacto. **No usa ``ApiResponse``**: es un archivo, no un recurso JSON
    (mismo criterio que el ``export`` de schema-comparisons).

    Se usa ``FileResponse`` y no ``StreamingResponse`` porque la versión de Starlette del
    repositorio (0.50) implementa ``Range`` en ``FileResponse`` —descarga reanudable,
    ``Accept-Ranges``, ``416`` y ``If-Range``— y reimplementarlo a mano sería peor. Lo que el
    diseño pedía de la respuesta se conserva: entrega por trozos desde disco (nunca en
    memoria), ``Content-Disposition``, ``Content-Length`` y ``ETag`` = el sha256 del
    artefacto (que también es lo que valida ``If-Range``).

    ``X-Export-Complete`` es la marca del §14 en la capa de transporte: ``false`` cuando el
    job no terminó bien. El artefacto además lleva el banner de incompleto al final, para que
    la marca sobreviva a un cliente que ignore las cabeceras.

    **Un solo uso**: al terminar la entrega el archivo se borra (``EXPORT_SINGLE_USE_DOWNLOAD``).
    Una descarga GENUINAMENTE parcial NO lo consume: borrarlo ahí rompería justamente la
    reanudación que el ``Range`` habilita. Lo que decide es si el rango CUBRE el archivo
    entero, no la mera presencia de la cabecera: con esa lectura ingenua, un
    ``Range: bytes=0-`` bajaba el artefacto completo y lo dejaba disponible para la próxima
    — el "un solo uso" se anulaba con una cabecera. El contador de descargas se incrementa
    en los dos casos (ver ``finish_delivery``).
    """
    controller = ExportController()
    info = controller.prepare_download(job_id, admin=admin, inline=False)
    whole = _range_covers_whole_file(
        request.headers.get("range"),
        request.headers.get("if-range"),
        info["byte_size"],
        info["sha256"],
    )
    finish = BackgroundTask(
        controller.finish_delivery, job_id, admin=admin, consume=whole
    )
    return FileResponse(
        path=info["path"],
        media_type=info["media_type"],
        filename=info["filename"],
        headers={
            "ETag": f'"{info["sha256"]}"',
            "X-Export-Complete": "true" if info["complete"] else "false",
            "X-Export-Sha256": info["sha256"],
        },
        background=finish,
    )


@router.get("/database-exports/{job_id}/content")
@limiter.limit("3/minute")
def export_content(request: Request, admin: AdminDep, job_id: int):
    """
    Entrega EN LÍNEA: el artefacto como ``text/plain`` **sin envolver**, para copiarlo al
    portapapeles tal cual.

    Si supera ``EXPORT_INLINE_MAX_BYTES`` responde **409 accionable** (con el tamaño real y
    la indicación de usar la descarga como archivo). **Nunca se trunca en silencio**: un
    script cortado que alguien pega y ejecuta es peor que un fallo. El preview ya publica
    ``inline_delivery_viable`` para que el cliente lo sepa antes de lanzar el job.
    """
    info = ExportController().read_inline(job_id, admin=admin)
    return PlainTextResponse(
        content=info["text"],
        headers={
            "ETag": f'"{info["sha256"]}"',
            "X-Export-Complete": "true" if info["complete"] else "false",
            "X-Export-Sha256": info["sha256"],
        },
    )
