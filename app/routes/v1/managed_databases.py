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

from typing import Annotated

from fastapi import APIRouter, Depends, Path as FPath, Query, Request

from app.controllers.managed_database_controller import ManagedDatabaseController
from app.controllers.managed_migration_controller import ManagedMigrationController
from app.core.authz import (
    BlueprintsRead,
    DatabasesRead,
    EnvironmentsWrite,
    ServersAdmin,
    require_at,
)
from app.core.actor import Actor
from app.core.limiter import limiter
from app.models.enums import EngineType, ProvisionStatus
from app.schemas.managed_database import (
    AgentAccessIn,
    AdoptDatabaseIn,
    DataCredentialOut,
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
from app.core.scope import assert_at
from app.core.scope_targets import (
    database,
    capability_for_database_update,
    database_update,
    managed_create,
    managed_create_for,
)
from app.services.capability_catalog import Capability
from app.utils.pagination import PaginationDep
from app.utils.response import ApiResponse, empty, paginated, success

router = APIRouter(prefix="/managed-databases", tags=["Managed Databases"])

# Capa 1 + capa 2 sobre la BD de la ruta (``db_id``). Alias locales y no en ``authz``: el tipo de
# destino es decisión de cada ruta, y un alias global por (capacidad, destino) se multiplicaría.
DatabasesWriteAtDb = Annotated[
    Actor, Depends(require_at(Capability.DATABASES_WRITE, target=database))
]
BlueprintsApplyAtDb = Annotated[
    Actor, Depends(require_at(Capability.BLUEPRINTS_APPLY, target=database))
]
BlueprintsCapturesAtDb = Annotated[
    Actor, Depends(require_at(Capability.BLUEPRINTS_CAPTURES, target=database))
]
# Opt-in de DATOS: ``data.read`` EN el entorno de la base (capa 2) y step-up. Es owner-only, así que
# "otro owner" del segundo aprobador es alguien que ya tiene ``data.read`` aquí.
DataReadAtDb = Annotated[Actor, Depends(require_at(Capability.DATA_READ, target=database))]
# PATCH de inventario: igual que ``DatabasesWriteAtDb`` salvo que un PATCH que trae SOLO
# ``environment_id`` (reclasificar) exige ``environments.write`` y no ``databases.write``: el
# ``security_officer`` con rol base ``viewer`` tiene que poder hacerlo. Si toca otro campo
# además, sigue necesitando ``databases.write`` (y el controller exige también
# ``environments.write`` si el entorno cambia). Ver ``database_update``.
DatabasesWriteAtDbUpdate = Annotated[
    Actor,
    Depends(
        require_at(
            Capability.DATABASES_WRITE,
            target=database_update,
            capability_for=capability_for_database_update,
        )
    ),
]
# Alta y adopción: el destino sale del PAYLOAD (servidor + entorno declarado u omitido).
DatabasesWriteAtCreate = Annotated[
    Actor, Depends(require_at(Capability.DATABASES_WRITE, target=managed_create))
]


@router.get("", response_model=ApiResponse[list[ManagedDatabaseOut]])
def list_databases(
    actor: DatabasesRead,
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


# Mismo cubo que ``/provision``, y por la misma razón: emite el mismo ``CREATE DATABASE``.
# Faltaba, y con ``apply_migrations`` el alta pasa además a ejecutar migraciones — o sea que
# sería la puerta SIN límite a una operación que sí lo tiene (``apply`` está en 10/min).
@router.post("", response_model=ApiResponse[ManagedDatabaseOut], status_code=201)
@limiter.limit("10/minute")
def create_database(
    request: Request,
    actor: DatabasesWriteAtCreate,
    payload: ManagedDatabaseCreate,
    provision: bool = Query(False),
):
    """
    Registra la BD y, con ``provision=true``, la crea en el motor.

    **Con ``apply_migrations=true`` exige además ``blueprints.apply``.** El alta con migración
    ejecuta el SQL del blueprint en el motor con la credencial pseudo-root: es la misma
    operación que ``POST /{db_id}/migrations/apply``, que sí la exige. Sin este chequeo,
    ``databases.write`` (``operator``) alcanzaba: un operador escribía una versión
    (``blueprints.write``) y la ejecutaba él mismo creando una base nueva, rompiendo el "quien
    escribe no es quien aplica" del catálogo. Va en la RUTA porque depende del payload y la
    firma declara una sola capacidad (§6.3 punto 1); se evalúa ANTES de tocar nada.

    Capa 2 sobre el PEOR entre el entorno declarado (u omitido: el activo más protegido) y el
    derivado del servidor, para que declarar ``development`` no sirva de coartada. El
    escalamiento va con ``assert_at`` sobre el mismo destino y no con ``assert_capability``:
    esa mira el rol unión y dejaría pasar a quien solo es ``owner`` en desarrollo.
    """
    if payload.apply_migrations:
        assert_at(
            actor,
            Capability.BLUEPRINTS_APPLY,
            managed_create_for(payload.server_id, payload.environment_id),
        )
    created = ManagedDatabaseController().create_database(
        payload.model_dump(), provision=provision, admin=actor
    )
    msg = "Base de datos registrada en el inventario."
    if provision:
        msg = "Base de datos creada y aprovisionada en el motor."
    return success(data=created, message=msg)


@router.post("/adopt", response_model=ApiResponse[ManagedDatabaseOut], status_code=201)
def adopt_database(actor: DatabasesWriteAtCreate, payload: AdoptDatabaseIn):
    """
    Adopta una BD que YA existe en el motor (Plan 09): registra metadata sin ejecutar
    CREATE DATABASE. 404 si la BD no existe; 409 si ya está en el inventario.

    **Con ``model_version`` exige además ``blueprints.apply``.** Declarar la versión hace
    ``stamp`` en el motor y escribe la caché de versión aplicada, que es lo que lee cualquier
    gate de promoción: es la misma decisión que ``POST /{db_id}/migrations/stamp``, que sí la
    exige. Va en la RUTA porque depende del payload y se evalúa ANTES de tocar nada, así que
    un 403 no deja ninguna versión estampada.
    """
    if payload.model_version is not None:
        assert_at(
            actor,
            Capability.BLUEPRINTS_APPLY,
            managed_create_for(payload.server_id, payload.environment_id),
        )
    created = ManagedDatabaseController().adopt_database(payload.model_dump(), admin=actor)
    return success(data=created, message="Base de datos existente adoptada al inventario.")


@router.get("/{db_id}", response_model=ApiResponse[ManagedDatabaseOut])
def get_database(actor: DatabasesRead, db_id: int):
    return success(data=ManagedDatabaseController().get_database(db_id))


@router.patch("/{db_id}", response_model=ApiResponse[ManagedDatabaseOut])
def update_database(actor: DatabasesWriteAtDbUpdate, db_id: int, payload: ManagedDatabaseUpdate):
    """
    Metadatos del inventario. Cambiar ``environment_id`` (reclasificar) exige
    ``environments.write`` —lo valida el controller contra el valor actual—. Un PATCH con SOLO
    ``environment_id`` exige únicamente esa capacidad (sin ``databases.write``): quien reclasifica
    es el ``security_officer``, aunque su rol base sea ``viewer``. Con otros campos además, exige
    las dos.
    """
    updated = ManagedDatabaseController().update_database(
        db_id, payload.model_dump(exclude_unset=True), admin=actor
    )
    return success(data=updated, message="Base de datos actualizada.")


@router.delete("/{db_id}", response_model=ApiResponse[None])
def delete_database(
    actor: DatabasesWriteAtDb,
    db_id: int,
    drop_remote: bool = Query(False),
    confirm_name: str | None = Query(
        None,
        description="Obligatorio si drop_remote=true: repetir el nombre exacto de la BD para confirmar el DROP en el motor.",
    ),
):
    """
    Saca la BD del inventario y, con ``drop_remote=true``, la BORRA del motor.

    **``drop_remote`` exige ``databases.drop``, no ``write``.** Son dos operaciones muy
    distintas detrás de un query param: sin él esto olvida una fila —reversible adoptándola de
    nuevo—; con él ejecuta un DROP DATABASE sobre la base de un tercero. Mapear la ruta entera
    a ``databases.drop`` sería el error simétrico: pediría el permiso más alto del módulo para
    limpiar una fila del inventario.

    El ``confirm_name`` sigue siendo obligatorio con ``drop_remote``: la capacidad dice quién
    puede, la confirmación dice sobre qué.
    """
    if drop_remote:
        # Escalamiento por payload: ``assert_at`` y no ``assert_capability``, porque la ruta
        # tiene capa 2 y la capa 1 sola (rol UNIÓN) dejaría pasar un DROP en producción.
        assert_at(actor, Capability.DATABASES_DROP, database(db_id))
    ManagedDatabaseController().delete_database(
        db_id, drop_remote=drop_remote, confirm_name=confirm_name, admin=actor
    )
    return empty("Base de datos eliminada.")


@router.put("/{db_id}/agent-access", response_model=ApiResponse[ManagedDatabaseOut])
def set_agent_access(actor: EnvironmentsWrite, db_id: int, payload: AgentAccessIn):
    """
    Abre o cierra esta BD para los agentes (MCP). Es el **opt-in por base**.

    ENDPOINT PROPIO Y NO UN CAMPO DEL PATCH, a propósito: es la palanca que decide si la
    estructura de la base de un tercero sale del gateway hacia el contexto de un modelo. Como
    campo de `ManagedDatabaseUpdate` se movería junto con un cambio de nombre o de entorno, sin
    gesto propio y sin rastro distinguible.

    Detrás de ``environments.write`` (solo ``security_officer``) y no del rol operativo ni de
    ``access.admin``, porque es **dato de política**: la regla del §4.5 es que toda fila que un
    guard lee es una frontera de privilegio, así que su escritor necesita al menos el privilegio
    del guard que puede apagar. Quien administra el acceso no decide qué BDs ven los agentes.

    Se audita con ``record_intent`` **fail-closed** cuando ABRE: si el rastro no se puede
    persistir, la apertura no ocurre. Cerrar se audita best-effort — negar acceso no necesita
    la misma garantía que otorgarlo.

    El veto (``blocked``) **gana sobre el permiso** y no tiene override: ni ``force``, ni nada.
    """
    return success(
        data=ManagedDatabaseController().set_agent_access(
            db_id,
            allowed=payload.allowed,
            blocked=payload.blocked,
            admin=actor,
        ),
        message="Acceso de agentes actualizado.",
    )


@router.post(
    "/{db_id}/data-credential/provision", response_model=ApiResponse[DataCredentialOut]
)
@limiter.limit("3/minute")
def provision_data_credential(request: Request, actor: ServersAdmin, db_id: int):
    """
    Crea la cuenta de DATOS del MCP para ESTA base con la pseudo-root: ``SELECT`` sobre esa base
    y nada más. Sin cuerpo: ni la contraseña, ni el usuario, ni los grants los elige el cliente.

    A diferencia de la credencial de estructura (por servidor), esta es **por base**: no alcanza
    ninguna otra. Es DCL sobre la base de un tercero, por eso exige ``servers.admin`` (el mismo
    privilegio que usa la pseudo-root) y 3/min. Idempotente: si la cuenta es del gateway, rota la
    contraseña y re-aplica el grant. La credencial queda SIN verificar hasta la sonda, así que
    ninguna tool de datos puede usarla todavía. Solo HTTP: no hay tool del MCP.

    Errores 409 (``public_context.code``): ``data_credential.account_already_exists`` (la cuenta
    es de un tercero), ``data_credential.provision_in_progress`` y
    ``data_credential.database_not_eligible``.
    """
    return success(
        data=ManagedDatabaseController().provision_data_credential(db_id, admin=actor),
        message="Credencial de datos aprovisionada; pendiente de verificación.",
    )


@router.post(
    "/{db_id}/data-credential/verify", response_model=ApiResponse[DataCredentialOut]
)
@limiter.limit("6/minute")
def verify_data_credential(request: Request, actor: ServersAdmin, db_id: int):
    """
    Corre la sonda NEGATIVA de la credencial de datos: conecta con la cuenta de ESTA base y exige
    que el motor muestre SELECT sobre exactamente esa base, sin privilegios de escritura, sin
    tablas que reenvíen a otro servidor (``FEDERATED``/``CONNECT``/``SPIDER``) ni extensiones
    que lo hagan (``dblink``/FDW en PostgreSQL). Sin cuerpo.

    Solo si pasa fija ``verified_at``; las tools de datos la exigen reciente
    (``MCP_DATA_CREDENTIAL_MAX_AGE_DAYS``). Si falla, BORRA la verificación anterior y responde 422
    ``managed_database.data_probe_failed`` con ``public_context.reasons`` (``CREDENTIAL_TOO_BROAD``,
    ``WRITE_PRIVILEGE_PRESENT``, ``FEDERATED_TABLE_PRESENT``, ``PROBE_NOT_GREEN``) y
    ``public_context.violations`` (motivos cortos, nunca el texto de un grant).

    Errores 409: ``data_credential.missing`` (sin credencial) y
    ``data_credential.provision_in_progress`` (aprovisionar o revocar en curso).
    """
    return success(
        data=ManagedDatabaseController().verify_data_credential(db_id, admin=actor),
        message="Credencial de datos verificada.",
    )


@router.get("/{db_id}/data-credential", response_model=ApiResponse[DataCredentialOut])
def get_data_credential_status(actor: DatabasesRead, db_id: int):
    """
    Estado de la credencial de datos y de su opt-in: existe, sonda (``verified_at``, códigos),
    ``data_access_state`` (``closed``/``pending``/``open``) y si el entorno exige segundo owner.
    Solo lectura; nunca lleva usuario, contraseña ni grants.
    """
    return success(data=ManagedDatabaseController().get_data_credential_status(db_id))


@router.post("/{db_id}/data-access/request", response_model=ApiResponse[DataCredentialOut])
@limiter.limit("10/minute")
def request_data_access(request: Request, actor: DataReadAtDb, db_id: int):
    """
    Pide abrir la lectura de DATOS de esta base a agentes (opt-in por base). Exige ``data.read``
    en el entorno y step-up. Sin cuerpo.

    En ``production`` (y en bases sin entorno) el pedido queda ``pending`` hasta que OTRO owner lo
    apruebe; en los demás entornos abre en el acto. Se audita fail-closed. Abrirlo NO alcanza: las
    tools de datos además exigen credencial con sonda verde reciente y el kill switch encendido.

    Errores: 409 ``data_credential.missing`` (sin credencial) y ``data_access.already_open``.
    """
    return success(
        data=ManagedDatabaseController().request_data_access(db_id, admin=actor),
        message="Pedido de acceso a datos registrado.",
    )


@router.post("/{db_id}/data-access/approve", response_model=ApiResponse[DataCredentialOut])
@limiter.limit("10/minute")
def approve_data_access(request: Request, actor: DataReadAtDb, db_id: int):
    """
    Aprueba el pedido pendiente y abre la lectura de datos. Lo aprueba OTRO owner: el solicitante
    recibe 403 ``data_access.self_approval_forbidden``; sin pedido pendiente, 409
    ``data_access.not_pending``. Exige ``data.read`` en el entorno y step-up. Se audita fail-closed.
    """
    return success(
        data=ManagedDatabaseController().approve_data_access(db_id, admin=actor),
        message="Acceso a datos aprobado.",
    )


@router.delete("/{db_id}/data-access", response_model=ApiResponse[DataCredentialOut])
def revoke_data_access(actor: DataReadAtDb, db_id: int):
    """
    Cierra el acceso a datos de la base o cancela un pedido pendiente. Inmediato e idempotente.
    Exige ``data.read`` en el entorno y step-up (el DELETE es un método no seguro y la exención de
    step-up es solo para ``POST .../cancel``). No toca el motor ni la credencial (para borrar la cuenta: ``DELETE .../data-credential``).
    """
    return success(
        data=ManagedDatabaseController().revoke_data_access(db_id, admin=actor),
        message="Acceso a datos cerrado.",
    )


@router.delete(
    "/{db_id}/data-credential", response_model=ApiResponse[DataCredentialOut]
)
def clear_data_credential(actor: ServersAdmin, db_id: int):
    """
    Palanca de emergencia: corta la lectura de datos de esta base y borra su cuenta del motor.
    Idempotente: sin credencial no hace nada. Si el motor no contesta responde el error del motor
    pero la credencial queda des-verificada (ya inusable) y el reintento termina la revocación.
    """
    return success(
        data=ManagedDatabaseController().clear_data_credential(db_id, admin=actor),
        message="Credencial de datos revocada.",
    )


@router.post(
    "/{db_id}/reassign-owner", response_model=ApiResponse[ManagedDatabaseOut]
)
def reassign_owner(
    actor: DatabasesWriteAtDb,
    db_id: int,
    payload: ReassignOwnerIn,
    provision: bool = Query(False),
):
    """
    Reasigna el usuario del motor dueño de la BD en el inventario.

    Con ``provision=true`` además lo aplica en el motor (re-GRANT, y en PostgreSQL
    ``ALTER DATABASE ... OWNER TO``). **Eso exige ``databases.drop`` en la BD**, no solo
    ``databases.write``: en PostgreSQL el dueño de una base puede hacerle ``DROP DATABASE``, y en
    MySQL/MariaDB el re-GRANT le da ``ALL PRIVILEGES`` sobre ella. Entregar ese control a otro
    usuario del motor es equivalente a poder borrarla, así que pide la misma capacidad.
    Sin ``provision`` solo cambia la fila del gateway y basta ``databases.write``.
    """
    if provision:
        assert_at(actor, Capability.DATABASES_DROP, database(db_id))
    updated = ManagedDatabaseController().reassign_owner(
        db_id, payload.owner_id, provision=provision, admin=actor
    )
    return success(data=updated, message="Propietario reasignado.")


@router.post(
    "/{db_id}/provision", response_model=ApiResponse[ManagedDatabaseProvisionOut]
)
@limiter.limit("10/minute")
def provision_database(
    request: Request,
    actor: DatabasesWriteAtDb,
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
        db_id, allow_recreate=allow_recreate, admin=actor
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
def migration_status(actor: BlueprintsRead, db_id: int):
    return success(data=ManagedMigrationController().status(db_id))


@router.post("/{db_id}/migrations/apply", response_model=ApiResponse[MigrationApplyOut])
@limiter.limit("10/minute")
def apply_migrations(
    request: Request,
    actor: BlueprintsApplyAtDb,
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
):
    """
    Aplica las versiones pendientes del blueprint sobre esta BD 🔌.

    **Captura de resultados**: si alguna versión PENDIENTE (no todo el blueprint) tiene
    'capture_selects=true' y NO está revisada, se responde 409
    'migration.capture_unreviewed' sin ejecutar ninguna sentencia de la migración — para saber
    qué está pendiente el gateway lee antes la versión actual del destino, así que sí abre una
    conexión de solo lectura. Ese es el único gate: el consentimiento por corrida
    ('allow_result_capture') se retiró. Con 'dry_run=true' no bloquea y el plan informa en
    'will_capture_versions' qué versiones van a capturar.
    """
    result = ManagedMigrationController().apply(
        db_id, up_to_version=version, force=force, dry_run=dry_run,
        on_failure=on_failure, admin=actor,
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
    actor: BlueprintsApplyAtDb,
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
):
    """
    Revierte esta BD secuencialmente hasta 'target_version' 🔌 (operación DESTRUCTIVA).

    **Captura de resultados**: el rollback captura igual que el apply (el codegen emite la
    llamada también para el down_sql), así que rige el MISMO gate — 409
    'migration.capture_unreviewed' si alguna versión del camino a revertir tiene
    'capture_selects=true' sin revisar, sin ejecutar ninguna sentencia de la migración (el
    gateway sí lee antes la versión actual del destino para saber qué camino hay que revertir).
    """
    result = ManagedMigrationController().rollback(
        db_id,
        confirm_version=confirm_version,
        target_version=target_version,
        admin=actor,
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
    actor: BlueprintsApplyAtDb,
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
        admin=actor,
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
    actor: BlueprintsApplyAtDb,
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
    purge: bool = Query(
        False,
        description=(
            "VACÍA la tabla de versión antes de escribir, en vez de resolver el puntero "
            "actual para moverlo. Única salida para una BD cuyo puntero nombra una revisión "
            "que ya no existe en la cadena (Alembic muere con 'Can't locate revision' y esa "
            "base queda sin apply, sin rollback y sin stamp). Requiere 'force': descarta el "
            "valor viejo SIN leerlo."
        ),
    ),
):
    result = ManagedMigrationController().stamp(
        db_id, version, force=force, purge=purge, admin=actor
    )
    msg = (
        "Versión marcada (stamp)."
        + (" Checkpoint parcial descartado." if force else "")
        + (" Tabla de versión vaciada antes de escribir." if purge else "")
    )
    return success(data=result, message=msg)


@router.get(
    "/{db_id}/migrations/{version}/select-results",
    response_model=ApiResponse[MigrationSelectResultsOut],
)
@limiter.limit("20/minute")
def migration_select_results(
    request: Request,
    actor: BlueprintsCapturesAtDb,
    db_id: int,
    version: str = FPath(..., pattern=r"^\d{4,10}$", description="Versión de la migración"),
):
    """
    Resultados capturados de las sentencias de LECTURA de una versión sobre esta BD.

    Solo existen si la versión tiene ``capture_selects=true`` (opt-in) y está revisada
    (``reviewed=true``) — los dos controles rigen para AMBAS direcciones, porque el
    ``down_sql`` captura igual que el ``up_sql``. Están disponibles tanto si la migración terminó bien como si falló a
    mitad de camino — que es el caso para el que la feature existe.

    **Devuelve DATOS DE NEGOCIO de la base gestionada** (la única excepción deliberada a la
    regla de que el gateway no almacena datos): el payload está cifrado en reposo y la
    lectura se audita fail-closed ANTES de descifrarlo.

    Ojo con ``durability='rolled_back'`` (solo PostgreSQL): esas filas describen lo que se
    vio DURANTE el intento, no el estado final de la base.
    """
    return success(data=ManagedMigrationController().select_results(db_id, version, admin=actor))


@router.delete(
    "/{db_id}/migrations/{version}/select-results",
    response_model=ApiResponse[None],
)
def purge_migration_select_results(
    actor: BlueprintsCapturesAtDb,
    db_id: int,
    version: str = FPath(..., pattern=r"^\d{4,10}$", description="Versión de la migración"),
):
    """
    Purga manual de los resultados capturados de una versión en esta BD.

    Las capturas expiran solas por TTL (``MIGRATION_CAPTURE_TTL_HOURS``, default 7 días);
    esto es la vía para borrarlas ya, en cuanto el diagnóstico terminó.

    Exige ``blueprints.captures`` (la capacidad que gobierna LEERLAS), no ``blueprints.write``:
    purgar destruye la evidencia de una migración, y quien no puede verla tampoco puede
    borrarla.
    """
    deleted = ManagedMigrationController().purge_select_results(db_id, version, admin=actor)
    return empty(f"{deleted} resultado(s) capturado(s) eliminado(s).")


@router.get(
    "/{db_id}/migrations/history",
    response_model=ApiResponse[list[MigrationHistoryOut]],
)
def migration_history(actor: BlueprintsRead, db_id: int, pagination: PaginationDep):
    items, total = ManagedMigrationController().history(
        db_id, limit=pagination.size, offset=pagination.offset
    )
    return paginated(items, total=total, pagination=pagination)
