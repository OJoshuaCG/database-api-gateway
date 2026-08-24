"""
Endpoints de ManagedDatabases (bases de datos gestionadas).

Crea/borra BDs reales en el motor destino. Flags y rutas que tocan el motor:
- ``?provision=true`` en POST → CREATE DATABASE (**sin GRANT**: crear una BD no otorga ningún
  privilegio al propietario; se asignan aparte vía ``POST /server-users/{id}/grants``).
- ``POST /{id}/provision`` → CREATE DATABASE sobre una fila YA registrada que quedó ``pending``
  o ``error``, sin tener que borrarla y recrearla.
- ``?drop_remote=true`` en DELETE → DROP DATABASE.
- ``?provision=true`` en reassign-owner → re-grant / ALTER OWNER en el motor.
"""

from fastapi import APIRouter, Path as FPath, Query, Request

from app.controllers.managed_database_controller import ManagedDatabaseController
from app.controllers.managed_migration_controller import ManagedMigrationController
from app.core.auth import AdminDep
from app.core.limiter import limiter
from app.models.enums import EngineType, ProvisionStatus
from app.schemas.managed_database import (
    AdoptDatabaseIn,
    ManagedDatabaseCreate,
    ManagedDatabaseOut,
    ManagedDatabaseProvisionOut,
    ManagedDatabaseUpdate,
    ReassignOwnerIn,
)
from app.schemas.model_migration import (
    MigrationApplyOut,
    MigrationHistoryOut,
    MigrationReconcilePartialOut,
    MigrationRollbackOut,
    MigrationSelectResultsOut,
    MigrationStatusOut,
)
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/managed-databases", tags=["Managed Databases"])


@router.get("", response_model=ApiResponse[list[ManagedDatabaseOut]])
def list_databases(
    admin: AdminDep,
    pagination: PaginationDep,
    server_id: int | None = Query(None, ge=1),
    owner_id: int | None = Query(None, ge=1),
    model_id: int | None = Query(None, ge=1),
    environment_id: int | None = Query(
        None, ge=1, description="Filtra por entorno de despliegue."
    ),
    only_unassigned: bool = Query(
        False,
        description=(
            "Solo las BDs SIN entorno asignado. Hace falta un flag propio porque "
            "'environment_id' vacío ya significa 'sin filtro'. Mandar los dos devuelve 422 "
            "(environment.filter_conflict) en vez de una lista vacía en silencio."
        ),
    ),
    status: ProvisionStatus | None = Query(None),
    engine: EngineType | None = Query(
        None, description="Filtra por motor del servidor (join a Server.engine)."
    ),
):
    items, total = ManagedDatabaseController().list_databases(
        server_id=server_id,
        owner_id=owner_id,
        model_id=model_id,
        environment_id=environment_id,
        only_unassigned=only_unassigned,
        status=status,
        engine=engine,
        limit=pagination.size,
        offset=pagination.offset,
    )
    return paginated(items, total=total, pagination=pagination)


@router.post("", response_model=ApiResponse[ManagedDatabaseOut], status_code=201)
def create_database(
    admin: AdminDep, payload: ManagedDatabaseCreate, provision: bool = Query(False)
):
    created = ManagedDatabaseController().create_database(
        payload.model_dump(), provision=provision, admin=admin
    )
    msg = "Base de datos registrada en el inventario."
    if provision:
        msg = "Base de datos creada y aprovisionada en el motor."
    return success(data=created, message=msg)


@router.post("/adopt", response_model=ApiResponse[ManagedDatabaseOut], status_code=201)
def adopt_database(admin: AdminDep, payload: AdoptDatabaseIn):
    """
    Adopta una BD que YA existe en el motor (Plan 09): registra metadata sin ejecutar
    CREATE DATABASE. 404 si la BD no existe; 409 si ya está en el inventario.
    """
    created = ManagedDatabaseController().adopt_database(payload.model_dump(), admin=admin)
    return success(data=created, message="Base de datos existente adoptada al inventario.")


@router.get("/{db_id}", response_model=ApiResponse[ManagedDatabaseOut])
def get_database(admin: AdminDep, db_id: int):
    return success(data=ManagedDatabaseController().get_database(db_id))


@router.patch("/{db_id}", response_model=ApiResponse[ManagedDatabaseOut])
def update_database(admin: AdminDep, db_id: int, payload: ManagedDatabaseUpdate):
    updated = ManagedDatabaseController().update_database(
        db_id, payload.model_dump(exclude_unset=True), admin=admin
    )
    return success(data=updated, message="Base de datos actualizada.")


@router.delete("/{db_id}", response_model=ApiResponse[None])
def delete_database(
    admin: AdminDep,
    db_id: int,
    drop_remote: bool = Query(False),
    confirm_name: str | None = Query(
        None,
        description="Obligatorio si drop_remote=true: repetir el nombre exacto de la BD para confirmar el DROP en el motor.",
    ),
):
    ManagedDatabaseController().delete_database(
        db_id, drop_remote=drop_remote, confirm_name=confirm_name, admin=admin
    )
    return empty("Base de datos eliminada.")


@router.post(
    "/{db_id}/reassign-owner", response_model=ApiResponse[ManagedDatabaseOut]
)
def reassign_owner(
    admin: AdminDep,
    db_id: int,
    payload: ReassignOwnerIn,
    provision: bool = Query(False),
):
    updated = ManagedDatabaseController().reassign_owner(
        db_id, payload.owner_id, provision=provision, admin=admin
    )
    return success(data=updated, message="Propietario reasignado.")


@router.post(
    "/{db_id}/provision", response_model=ApiResponse[ManagedDatabaseProvisionOut]
)
@limiter.limit("10/minute")
def provision_database(
    request: Request,
    admin: AdminDep,
    db_id: int,
    allow_recreate: bool = Query(
        False,
        description=(
            "Permite aprovisionar una fila que el inventario ya marca 'active'. Es para el "
            "caso en que la BD se borró por fuera del gateway; sin esto, un 'active' responde "
            "409 para no enmascarar ese borrado."
        ),
    ),
):
    """
    Ejecuta el ``CREATE DATABASE`` faltante sobre una BD **ya registrada** en el inventario 🔌.

    Es la salida para una fila que quedó ``pending`` (registrada sin aprovisionar) o ``error``
    (el DDL del alta falló). Antes solo se podía borrar la fila y recrearla, perdiendo notas,
    entorno, blueprint e historial de migraciones.

    **No aplica las migraciones del blueprint** — eso sigue siendo
    ``POST /{id}/migrations/apply`` — y **no otorga privilegios**: se asignan aparte con
    ``POST /server-users/{id}/grants``.

    409 si la BD ya existe en el motor: adoptar una base preexistente es
    ``POST /managed-databases/adopt``.
    """
    result = ManagedDatabaseController().provision_database(
        db_id, allow_recreate=allow_recreate, admin=admin
    )
    msg = (
        "Base de datos creada en el motor."
        if result["provisioned"]
        else "La base de datos ya había sido creada por una operación simultánea; "
        "se reconcilió el estado del inventario."
    )
    return success(data=result, message=msg)


# --------------------------------------------------------------------------- #
# Migraciones del blueprint sobre ESTA BD (tocan el motor destino vía Alembic) #
# --------------------------------------------------------------------------- #
@router.get(
    "/{db_id}/migrations/status", response_model=ApiResponse[MigrationStatusOut]
)
def migration_status(admin: AdminDep, db_id: int):
    return success(data=ManagedMigrationController().status(db_id))


@router.post("/{db_id}/migrations/apply", response_model=ApiResponse[MigrationApplyOut])
@limiter.limit("10/minute")
def apply_migrations(
    request: Request,
    admin: AdminDep,
    db_id: int,
    version: str | None = Query(
        None,
        pattern=r"^\d{4,10}$",
        description=(
            "Versión objetivo (inclusive). En UNA sola llamada aplica secuencialmente, en "
            "orden, TODAS las migraciones pendientes hasta esta versión. Si se omite, aplica "
            "hasta la ÚLTIMA disponible. Forward-only: una versión ≤ la actual no aplica nada "
            "(para revertir, usa /rollback). 422 si la versión no existe en el blueprint."
        ),
    ),
    force: bool = Query(
        False,
        description=(
            "Override de cuarentena tras un fallo previo (inspeccionado). **NO** saltea el "
            "bloqueo de migraciones destructivas del entorno: ese guard no tiene override."
        ),
    ),
    dry_run: bool = Query(
        False,
        description=(
            "No aplica: devuelve el plan (versión actual + pendientes). Informa en "
            "'blocked_by' qué versiones bloquearía el entorno, sin fallar."
        ),
    ),
    on_failure: str = Query(
        "auto",
        pattern="^(auto|reconcile|leave)$",
        description=(
            "Qué hacer si una migración falla A MITAD (solo posible en MySQL/MariaDB: en "
            "PostgreSQL el motor deshace la migración por sí solo). "
            "'auto' (default) deshace lo aplicado SOLO si puede deshacerlo todo; "
            "'reconcile' deshace igual, salteando y reportando lo que no tiene reverso; "
            "'leave' no toca nada (cuarentena + checkpoint, comportamiento anterior). "
            "Con 'auto'/'reconcile' exitosos la BD NO queda en cuarentena: vuelve a su "
            "versión anterior de forma limpia y solo hay que corregir la migración."
        ),
    ),
    allow_result_capture: bool = Query(
        False,
        description=(
            "Consentimiento EXPLÍCITO de esta corrida para las versiones con "
            "'capture_selects=true': sus SELECT guardan el resultado (filas de ESTA base de "
            "datos, cifradas) en el gateway, legibles luego con "
            "GET .../migrations/{version}/select-results. Sin el flag, si alguna versión "
            "PENDIENTE (no todo el blueprint) tiene la captura activada se responde 409 sin "
            "ejecutar ninguna sentencia de la migración: para saber qué está pendiente el "
            "gateway lee antes la versión actual del destino, así que sí abre una conexión "
            "de solo lectura. Mismo 409 si alguna de esas versiones pendientes no está "
            "revisada (reviewed=false); con dry_run=true ninguno de los dos bloquea."
        ),
    ),
):
    result = ManagedMigrationController().apply(
        db_id, up_to_version=version, force=force, dry_run=dry_run,
        on_failure=on_failure, allow_result_capture=allow_result_capture, admin=admin,
    )
    msg = _apply_message(result, dry_run=dry_run)
    return success(data=result, message=msg)


def _apply_message(result: dict, *, dry_run: bool) -> str:
    """Mensaje legible del resultado de apply (real o dry-run)."""
    frm, to = result.get("from_version"), result.get("to_version")
    target = result.get("target_version")
    pend = result.get("pending_versions") or []
    if dry_run:
        if result.get("no_op"):
            return f"Plan (dry-run): la BD ya está al día en {frm or 'sin versión'}; nada pendiente."
        return f"Plan (dry-run): {len(pend)} pendiente(s) — {frm or '∅'} → {to}: {', '.join(pend)}."
    if result.get("no_op"):
        if target is not None:
            return (
                f"La versión solicitada ({target}) ya está aplicada o es anterior a la actual "
                f"({frm}): no se aplica nada (usa /rollback para revertir)."
            )
        return f"La BD ya está en la versión más reciente ({frm or 'sin versión'}); nada que aplicar."
    if result.get("failed"):
        rec = result.get("reconciliation")
        if rec and rec.get("fully_reconciled"):
            return (
                f"Falló la migración {rec['version']} y el sistema deshizo automáticamente "
                f"las {rec['undone_count']} sentencia(s) que ya se habían aplicado: la BD "
                f"quedó limpia en {to or '∅'}. Corregí la migración y reintentá."
            )
        if rec and rec.get("attempted"):
            return (
                f"Falló la migración {rec['version']} y la reconciliación automática quedó "
                f"INCOMPLETA ({rec['undone_count']}/{rec['statements_to_undo']} reversos). "
                "Revisa el estado y usa /migrations/reconcile-partial."
            )
        return (
            f"Aplicadas {result.get('applied_count', 0)} migración(es) con FALLO: "
            f"{frm or '∅'} → {to}. Revisa la cuarentena y /migrations/status "
            "(¿aplicación parcial?)."
        )
    return f"Aplicadas {result.get('applied_count', 0)} migración(es): {frm or '∅'} → {to}."


@router.post("/{db_id}/migrations/rollback", response_model=ApiResponse[MigrationRollbackOut])
@limiter.limit("10/minute")
def rollback_migration(
    request: Request,
    admin: AdminDep,
    db_id: int,
    confirm_version: str = Query(
        ...,
        pattern=r"^\d{4,10}$",
        description=(
            "Confirmación obligatoria (operación DESTRUCTIVA): repetir la versión "
            "ACTUAL de la BD desde la que se parte."
        ),
    ),
    target_version: str | None = Query(
        None,
        pattern=r"^\d{4,10}$",
        description=(
            "Versión destino a la que revertir (debe ser ANTERIOR a la actual). En UNA "
            "sola llamada aplica secuencialmente, en orden, todos los downgrades "
            "necesarios. Si se omite, revierte solo la última. 409 si alguna migración "
            "del camino no tiene down_sql confirmado; 422 si la versión no existe o no "
            "es anterior a la actual."
        ),
    ),
    allow_result_capture: bool = Query(
        False,
        description=(
            "Consentimiento EXPLÍCITO de esta corrida para las versiones del camino a "
            "revertir que tengan 'capture_selects=true': las sentencias de lectura de su "
            "down_sql guardan el resultado (filas de ESTA base de datos, cifradas) en el "
            "gateway. Mismo control que en /apply — el rollback también captura. Sin el "
            "flag se responde 409 sin ejecutar ninguna sentencia de la migración (el "
            "gateway sí lee antes la versión actual del destino para saber qué camino hay "
            "que revertir); también 409 si alguna de esas versiones no está revisada "
            "(reviewed=false)."
        ),
    ),
):
    result = ManagedMigrationController().rollback(
        db_id,
        confirm_version=confirm_version,
        target_version=target_version,
        allow_result_capture=allow_result_capture,
        admin=admin,
    )
    return success(data=result, message=_rollback_message(result))


def _rollback_message(result: dict) -> str:
    """Mensaje legible del resultado de rollback."""
    frm, to = result.get("from_version"), result.get("to_version")
    n = result.get("reverted_count", 0)
    if result.get("no_op"):
        return f"Nada que revertir: la BD ya está en {frm or 'base'}."
    if result.get("failed"):
        return (
            f"Rollback con fallo: revertidas {n}, la BD quedó en {to or 'base'}. "
            "Revisa la cuarentena."
        )
    return f"Revertidas {n} migración(es): {frm} → {to or 'base'}."


@router.post(
    "/{db_id}/migrations/reconcile-partial",
    response_model=ApiResponse[MigrationReconcilePartialOut],
)
@limiter.limit("10/minute")
def reconcile_partial_migration(
    request: Request,
    admin: AdminDep,
    db_id: int,
    confirm_version: str = Query(
        ...,
        pattern=r"^\d{4,10}$",
        description=(
            "Confirmación obligatoria: repetir la versión PARCIALMENTE aplicada (la que "
            "informa 'partial_application' en /migrations/status)."
        ),
    ),
    dry_run: bool = Query(
        False,
        description=(
            "Devuelve los reversos EXACTOS que se ejecutarían, sin tocar el motor. "
            "Recomendado antes de reconciliar."
        ),
    ),
    force: bool = Query(
        False,
        description=(
            "Procede aunque alguna sentencia ya aplicada no tenga reverso: la saltea y la "
            "reporta (409 sin esto). Esos cambios quedan en la BD y hay que resolverlos a "
            "mano."
        ),
    ),
):
    """
    Deshace las sentencias que SÍ se aplicaron de una migración que falló a mitad.

    Cuando un ``apply`` muere en la sentencia k de N, Alembic nunca registró la versión
    (el stamp va al final del ``upgrade()``), así que la BD queda con k sentencias
    aplicadas mientras el ledger sigue en la versión anterior. Este endpoint ejecuta el
    reverso EXACTO de esas k sentencias, en orden inverso, hasta que el plano físico
    vuelve a coincidir con el ledger. NO toca la tabla de versión: la versión parcial
    nunca existió para Alembic.

    Requiere que la versión tenga MANIFIESTO de sentencias (lo tienen las versiones
    generadas por adopción de un diff estructural). Sin él, el emparejamiento
    sentencia↔reverso es inferible y se responde 409 con el motivo.
    """
    result = ManagedMigrationController().reconcile_partial(
        db_id,
        confirm_version=confirm_version,
        dry_run=dry_run,
        force=force,
        admin=admin,
    )
    if result.get("dry_run"):
        msg = (
            f"Dry-run: se desharían {result['statements_to_undo']} sentencia(s) de la "
            f"aplicación parcial de {result['version']}."
        )
    elif result.get("fully_reconciled"):
        msg = (
            f"Estado reconciliado: deshechas {result['undone_count']} sentencia(s). "
            "La BD volvió a coincidir con su versión registrada."
        )
    else:
        msg = (
            f"Reconciliación incompleta: deshechas {result['undone_count']}, quedan "
            f"{result['remaining_applied_statements']} sentencia(s) aplicadas. Revisa el error."
        )
    return success(data=result, message=msg)


@router.post("/{db_id}/migrations/stamp", response_model=ApiResponse[MigrationStatusOut])
@limiter.limit("10/minute")
def stamp_migration(
    request: Request,
    admin: AdminDep,
    db_id: int,
    version: str = Query(..., pattern=r"^\d{4,10}$", description="Versión a marcar"),
    force: bool = Query(
        False,
        description=(
            "Descarta cualquier checkpoint de aplicación parcial detectado para esta BD "
            "(409 sin esto si existe uno) y omite el gate de revisión de una versión con "
            "'capture_selects=true' aún sin revisar. Úsalo solo tras reconciliar manualmente "
            "el estado físico real del motor."
        ),
    ),
):
    result = ManagedMigrationController().stamp(db_id, version, force=force, admin=admin)
    msg = "Versión marcada (stamp)." + (" Checkpoint parcial descartado." if force else "")
    return success(data=result, message=msg)


@router.get(
    "/{db_id}/migrations/{version}/select-results",
    response_model=ApiResponse[MigrationSelectResultsOut],
)
@limiter.limit("20/minute")
def migration_select_results(
    request: Request,
    admin: AdminDep,
    db_id: int,
    version: str = FPath(..., pattern=r"^\d{4,10}$", description="Versión de la migración"),
):
    """
    Resultados capturados de las sentencias de LECTURA de una versión sobre esta BD.

    Solo existen si la versión tiene ``capture_selects=true`` (opt-in), está revisada
    (``reviewed=true``) y el ``apply``/``rollback`` corrió con ``allow_result_capture=true``
    — los tres controles rigen para AMBAS direcciones, porque el ``down_sql`` captura igual
    que el ``up_sql``. Están disponibles tanto si la migración terminó bien como si falló a
    mitad de camino — que es el caso para el que la feature existe.

    **Devuelve DATOS DE NEGOCIO de la base gestionada** (la única excepción deliberada a la
    regla de que el gateway no almacena datos): el payload está cifrado en reposo y la
    lectura se audita fail-closed ANTES de descifrarlo.

    Ojo con ``durability='rolled_back'`` (solo PostgreSQL): esas filas describen lo que se
    vio DURANTE el intento, no el estado final de la base.
    """
    return success(data=ManagedMigrationController().select_results(db_id, version, admin=admin))


@router.delete(
    "/{db_id}/migrations/{version}/select-results",
    response_model=ApiResponse[None],
)
def purge_migration_select_results(
    admin: AdminDep,
    db_id: int,
    version: str = FPath(..., pattern=r"^\d{4,10}$", description="Versión de la migración"),
):
    """
    Purga manual de los resultados capturados de una versión en esta BD.

    Las capturas expiran solas por TTL (``MIGRATION_CAPTURE_TTL_HOURS``, default 7 días);
    esto es la vía para borrarlas ya, en cuanto el diagnóstico terminó.
    """
    deleted = ManagedMigrationController().purge_select_results(db_id, version, admin=admin)
    return empty(f"{deleted} resultado(s) capturado(s) eliminado(s).")


@router.get(
    "/{db_id}/migrations/history",
    response_model=ApiResponse[list[MigrationHistoryOut]],
)
def migration_history(admin: AdminDep, db_id: int, pagination: PaginationDep):
    items, total = ManagedMigrationController().history(
        db_id, limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)
