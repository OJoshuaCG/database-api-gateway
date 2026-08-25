"""
Endpoints de conversión de charset/collation EN LOTE, sobre todas las BDs de un blueprint.

- POST /database-models/{model_id}/collation-conversions
                                                  — planifica: un job por BD elegible, ya
                                                    previsualizado, + el batch_token.
- POST /database-models/{model_id}/collation-conversions/{batch_id}/execute
                                                  — confirma y ENCOLA el lote.
- GET  /database-models/{model_id}/collation-conversions/{batch_id}
                                                  — polling: cabecera + un job por BD.
- POST /database-models/{model_id}/collation-conversions/{batch_id}/cancel
                                                  — frena lo que todavía no arrancó.

POR QUÉ ESTO NO ES UNA MIGRACIÓN DE BLUEPRINT
---------------------------------------------
La respuesta intuitiva sería materializar la conversión como una versión y repartirla con
``apply``. No sirve, y el motivo es de fondo: el SQL de una conversión promete un resultado que
depende del estado de CADA destino. Una versión estática no puede recrear los objetos con la
collation congelada de la hermana —necesita su cuerpo, sus grants y su DEFINER, no los del
origen—, así que aplicarla dejaría las tablas convertidas y las rutinas/vistas en la collation
vieja: exactamente el ``Illegal mix of collations`` que este módulo existe para evitar, ahora
sobre una BD cuyo operador nunca vio el asistente.

Por eso el lote son N conversiones REALES, cada una leyendo su propio inventario.

CONFIRMACIÓN: un lote se lleva N re-tipeos y hay que reponerlos
--------------------------------------------------------------
``TODO.md`` declara por escrito que lo que protege este módulo es *"su propio doble factor
(re-tipeo + confirm_token)"*. El ``batch_token`` lo genera el servidor, así que aporta
FRESCURA, no INTENCIÓN — es el mismo argumento con el que este repo eliminó el consentimiento
por corrida de la captura de SELECT. De ahí que ``execute`` exija además el slug del blueprint,
el conjunto de BDs echado de vuelta (422 fail-closed ante cualquier diferencia) y el re-tipeo
del nombre de cada BD cuyo entorno bloquee migraciones destructivas.

Rate limits, con el criterio de la familia: planificar toca el motor N veces (lee el inventario
de cada BD) → 10/min; ejecutar es la operación más sensible del módulo → 3/min; el polling es
lectura de la BD del gateway pero se consulta seguido → 30/min.

Los jobs corren EN SERIE (``COLLATION_CONVERSION_MAX_WORKERS`` default 1) y un lote grande puede
tardar horas: por eso las respuestas llevan ``runs_serially`` y el polling trae ``batch_seq``,
o la UI no puede distinguir "en cola" de "colgado".
"""

from fastapi import APIRouter, Request

from app.controllers.collation_conversion_controller import CollationConversionController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.schemas.collation_conversion import (
    CollationBatchCreate,
    CollationBatchExecuteIn,
    CollationBatchExecuteOut,
    CollationBatchPlanOut,
    CollationBatchStatusOut,
)
from app.utils.response import ApiResponse, success

router = APIRouter(prefix="/database-models", tags=["Collation Batches"])


@router.post(
    "/{model_id}/collation-conversions",
    response_model=ApiResponse[CollationBatchPlanOut],
    status_code=201,
)
@limiter.limit("10/minute")
def create_collation_batch(
    request: Request, admin: AdminDep, model_id: int, payload: CollationBatchCreate
):
    """
    🔌 Planifica el lote: crea y previsualiza un job por cada BD **activa** del blueprint.

    Solo entran las ``status=active``: una ``pending`` no existe en el motor, una ``error`` está
    en cuarentena y una ``archived`` fue retirada de uso. Una BD a la que el objetivo no aplica
    (p. ej. ``target_charset`` contra PostgreSQL) sale como ítem con ``ok=false`` y su
    ``error_code``, **sin abortar el lote**.

    Devuelve el ``batch_token`` que ``/execute`` exige, y ``capped=true`` si el tope dejó BDs
    elegibles afuera — el recorte se reporta, nunca se silencia.
    """
    data = CollationConversionController().create_batch_plan(
        model_id,
        target_charset=payload.target_charset,
        target_collation=payload.target_collation,
        scope=payload.scope,
        tables=payload.tables,
        objects=payload.objects,
        include_database_default=payload.include_database_default,
        environment_id=payload.environment_id,
        max_databases=payload.max_databases,
        admin=admin,
    )
    return success(data=data, message="Lote de conversión planificado.")


@router.post(
    "/{model_id}/collation-conversions/{batch_id}/execute",
    response_model=ApiResponse[CollationBatchExecuteOut],
)
@limiter.limit("3/minute")
def execute_collation_batch(
    request: Request,
    admin: AdminDep,
    model_id: int,
    batch_id: int,
    payload: CollationBatchExecuteIn,
):
    """
    🔌 Confirma y encola el lote. Cada BD pasa por el MISMO camino de validación que una
    conversión suelta (nombre, token contra el plan recalculado, fingerprint anti-TOCTOU,
    cuarentena) — no una versión resumida.

    Responde **200** aunque alguna BD se rechace: el desenlace por base viaja en
    ``results[].ok`` + ``error_code``. Un rechazo del LOTE (slug, conjunto de BDs, re-tipeo
    faltante o token) es 422/409 y no encola nada.
    """
    data = CollationConversionController().execute_batch(
        model_id,
        batch_id,
        confirm_model_slug=payload.confirm_model_slug,
        confirm_token=payload.confirm_token,
        database_ids=payload.database_ids,
        confirmations=payload.confirmations,
        force=payload.force,
        admin=admin,
    )
    msg = (
        f"Lote encolado: {data['enqueued']} conversión(es). Corren EN SERIE."
        if data["enqueued"]
        else "Ninguna base de datos del lote pudo encolarse."
    )
    return success(data=data, message=msg)


@router.get(
    "/{model_id}/collation-conversions/{batch_id}",
    response_model=ApiResponse[CollationBatchStatusOut],
)
@limiter.limit("30/minute")
def get_collation_batch(request: Request, admin: AdminDep, model_id: int, batch_id: int):
    """
    Estado del lote + un job por BD, para el polling.

    Cada job trae ``batch_seq`` (los jobs corren en serie: es lo único que permite decir "la 4
    de 12") y ``tables_total``/``objects_total``, que son el denominador que ``progress`` nunca
    trae. El agregado va en ``batch.counts`` para no recorrer N filas en cada tick.
    """
    return success(data=CollationConversionController().list_batch(model_id, batch_id))


@router.post(
    "/{model_id}/collation-conversions/{batch_id}/cancel",
    response_model=ApiResponse[CollationBatchStatusOut],
)
@limiter.limit("10/minute")
def cancel_collation_batch(
    request: Request, admin: AdminDep, model_id: int, batch_id: int
):
    """
    Cancelación COOPERATIVA del lote.

    Las BDs que todavía están en cola no llegan a tocar el motor. La que está convirtiendo
    termina su paso en curso y corta en el próximo punto seguro: matar un ``ALTER TABLE`` a
    mitad dejaría la tabla a medio reescribir, que es peor que dejarlo terminar.
    """
    return success(
        data=CollationConversionController().cancel_batch(model_id, batch_id, admin=admin),
        message="Cancelación del lote solicitada.",
    )
