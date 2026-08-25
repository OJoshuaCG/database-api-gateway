"""Schemas Pydantic del recurso ModelMigration (migraciones de un blueprint)."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field

# Versión: solo dígitos (4–10). Se compara/ordena NUMÉRICAMENTE (no lexicográfico).
_VERSION = r"^\d{4,10}$"
# Cota de tamaño del SQL de una migración (256 KB). Falla temprano con 422 en vez de
# depender solo del límite global de tamaño de request (RequestSizeMiddleware).
_MAX_SQL = 262_144


class ModelMigrationCreate(BaseModel):
    version: str | None = Field(
        None,
        pattern=_VERSION,
        description=(
            "Opcional: si se omite, el gateway asigna automáticamente la SIGUIENTE versión "
            "secuencial (max+1). Pásala solo si quieres fijarla manualmente (0001, 0002…)."
        ),
    )
    name: str = Field(..., min_length=1, max_length=200)
    up_sql: str = Field(..., min_length=1, max_length=_MAX_SQL, description="Delta SQL base (estilo MySQL de referencia)")
    up_sql_mysql: str | None = Field(None, max_length=_MAX_SQL, description="Override manual MySQL/MariaDB (opcional)")
    up_sql_postgresql: str | None = Field(None, max_length=_MAX_SQL, description="Override manual PostgreSQL (opcional)")
    down_sql: str | None = Field(
        None, max_length=_MAX_SQL,
        description="Rollback confirmado (opcional). Si se omite, se sugiere uno auto-generado.",
    )
    capture_selects: bool = Field(
        False,
        description=(
            "OPT-IN: guardar el RESULTADO de las sentencias de lectura de esta versión "
            "cuando se aplique/revierta (GET .../migrations/{version}/select-results). "
            "Es la única vía por la que el gateway persiste datos de negocio, así que la "
            "versión nace SIN revisar: reviewed=false → apply/rollback dan 409 hasta aprobarla "
            "con PATCH reviewed=true. Esa aprobación es de una CONSULTA concreta y se revoca "
            "sola si el SQL cambia. El MISMO control rige para el rollback: el down_sql también "
            "captura."
        ),
    )


class ModelMigrationPatch(BaseModel):
    """Confirma el rollback o añade overrides DESPUÉS de crear la migración."""

    name: str | None = Field(None, min_length=1, max_length=200)
    up_sql: str | None = Field(
        None,
        min_length=1,
        max_length=_MAX_SQL,
        description=(
            "Corrige el SQL base del delta (dialecto de referencia: MySQL). Si alguna BD "
            "tiene la versión aplicada HOY, el default sigue siendo 409 "
            "'model_migration.sql_frozen' (fix-forward); para editarla igual hay que "
            "reenviar 'confirm_version' + 'confirm_token' del edit-preview, asumiendo la "
            "divergencia. Al cambiarlo se regenera el rollback sugerido y el checksum."
        ),
    )
    down_sql: str | None = Field(None, max_length=_MAX_SQL, description="Confirma el rollback de esta versión")
    up_sql_mysql: str | None = Field(None, max_length=_MAX_SQL, description="Añade/actualiza override MySQL")
    up_sql_postgresql: str | None = Field(None, max_length=_MAX_SQL, description="Añade/actualiza override PostgreSQL")
    reviewed: bool | None = Field(
        None,
        description=(
            "Aprueba (true) un baseline de snapshot tras revisar su DDL — habilita su apply "
            "(R1). En una versión con 'capture_selects=true' la aprobación es de una "
            "CONSULTA CONCRETA: si en la misma llamada (o en una posterior) cambia el SQL "
            "('up_sql', overrides o 'down_sql'), se vuelve a poner en false automáticamente."
        ),
    )
    capture_selects: bool | None = Field(
        None,
        description=(
            "Activa/desactiva la captura de resultados de los SELECT de esta versión. "
            "ACTIVARLA vuelve a poner reviewed=false (hay que re-aprobar la versión "
            "sabiendo que va a extraer datos), y con la captura activa cualquier cambio de "
            "SQL también revoca la aprobación; desactivarla no purga lo ya capturado — "
            "para eso está DELETE .../migrations/{version}/select-results."
        ),
    )
    confirm_version: str | None = Field(
        None,
        description=(
            "SOLO para editar el SQL de una versión que alguna BD tiene aplicada HOY. Debe "
            "ser exactamente la versión de la ruta; obliga a identificar conscientemente qué "
            "se toca (mismo criterio que 'confirm_target_name' al borrar una BD). Va junto "
            "con 'confirm_token'; sin ambos, la edición sigue respondiendo 409 "
            "'model_migration.sql_frozen'."
        ),
    )
    confirm_token: str | None = Field(
        None,
        description=(
            "Token emitido por POST .../migrations/{version}/edit-preview, atado al SQL "
            "EXACTO que se previsualizó (TTL corto). Si el SQL de este PATCH no es el que se "
            "previsualizó, el token no valida (422). ATENCIÓN: editar NO re-ejecuta nada — "
            "las BDs que ya aplicaron la versión conservan físicamente lo que corrió y "
            "quedan divergentes; el gateway lo registra en auditoría y marca la versión con "
            "'sql_diverged'."
        ),
    )


_SQL_FACTS_DESC = (
    "Hechos derivados del SQL para las insignias del listado. Se calculan con heurísticas de "
    "texto sobre el SQL enmascarado (barato, se paga por fila), no con el análisis completo: "
    "para el veredicto fino está POST .../migrations/validate."
)


_SQL_FROZEN_DESC = (
    "True = el SQL de esta versión ya no se puede modificar: alguna BD la aplicó con éxito, o "
    "hay una aplicación parcial a medias. Es la MISMA condición que evalúa el 409 del PATCH; se "
    "publica para que el cliente pueda bloquear el campo de entrada en vez de descubrirlo al "
    "guardar."
)
_DELETABLE_DESC = (
    "True = el DELETE de esta versión pasaría hoy: es la punta de la secuencia, ninguna BD la "
    "aplicó con éxito y no hay aplicación parcial sin resolver."
)
_SQL_DIVERGED_DESC = (
    "True = el SQL de esta versión se editó DESPUÉS de que alguna BD la aplicara, así que "
    "esa(s) BD(s) conservan físicamente lo que corrió antes y esta versión ya no las "
    "describe. NO restringe nada —el SQL nuevo es el que se aplica de acá en más— pero la "
    "UI no debería mostrar la versión como fiel al plano de todas sus BDs. El detalle (qué "
    "BDs, cuándo, con qué checksum previo) está en el log de auditoría, acción "
    "'migration.sql_edited_after_apply'."
)
_BLOCK_REASON_DESC = (
    "Por qué está restringida, o null si no lo está: 'applied' (alguna BD depende de ella), "
    "'partial' (aplicación a medias sin resolver) o 'not_tip' (hay versiones posteriores). "
    "'not_tip' solo impide BORRARLA — editarla sigue permitido."
)


class ModelMigrationSummary(BaseModel):
    """Item compacto para listados (no incluye el SQL completo ni traducciones)."""

    model_config = ConfigDict(from_attributes=True)

    sql_frozen: bool = Field(False, description=_SQL_FROZEN_DESC)
    deletable: bool = Field(True, description=_DELETABLE_DESC)
    block_reason: str | None = Field(None, description=_BLOCK_REASON_DESC)
    sql_diverged: bool = Field(False, description=_SQL_DIVERGED_DESC)

    id: int
    model_id: int
    version: str
    name: str
    has_mysql_override: bool
    has_postgresql_override: bool
    has_rollback: bool
    checksum: str
    kind: str = "schema"
    is_baseline: bool = False
    reviewed: bool = True
    capture_selects: bool = False
    has_seed: bool = Field(False, description="La migración inserta o modifica datos. " + _SQL_FACTS_DESC)
    forced_collations: list[str] = Field(
        default_factory=list, description="COLLATE explícitos encontrados en el SQL."
    )
    destructive: bool = Field(
        False, description="Contiene DROP o TRUNCATE. " + _SQL_FACTS_DESC
    )
    created_at: datetime


class ModelMigrationOut(BaseModel):
    """Detalle completo: incluye SQL, overrides, rollback y traducciones calculadas."""

    model_config = ConfigDict(from_attributes=True)

    sql_frozen: bool = Field(False, description=_SQL_FROZEN_DESC)
    deletable: bool = Field(True, description=_DELETABLE_DESC)
    block_reason: str | None = Field(None, description=_BLOCK_REASON_DESC)
    sql_diverged: bool = Field(False, description=_SQL_DIVERGED_DESC)

    id: int
    model_id: int
    version: str
    name: str
    up_sql: str
    up_sql_mysql: str | None = None
    up_sql_postgresql: str | None = None
    down_sql: str | None = None
    down_sql_suggested: str | None = None
    translated: dict[str, str] = Field(
        default_factory=dict, description="up_sql traducido por motor (mysql, postgresql)"
    )
    checksum: str
    kind: str = Field(
        "schema", description="'schema' (DDL) | 'data' (datos-semilla upsert, atado a source_engine)"
    )
    source_engine: str | None = Field(
        None, description="Motor de origen si proviene de un snapshot; None = portable (Plan 09)"
    )
    is_baseline: bool = False
    has_non_portable: bool = Field(
        False, description="True si incluye objetos procedurales no traducibles cross-engine"
    )
    reviewed: bool = Field(
        True, description="False = baseline de snapshot pendiente de revisión; no aplicable hasta aprobarse (R1)"
    )
    capture_selects: bool = Field(
        False,
        description=(
            "True = los SELECT de esta versión guardan su resultado (cifrado) al aplicarse "
            "O al revertirse (el down_sql captura igual que el up_sql). Requiere "
            "reviewed=true, tanto para el apply como para el rollback."
        ),
    )
    has_seed: bool = Field(False, description="La migración inserta o modifica datos. " + _SQL_FACTS_DESC)
    forced_collations: list[str] = Field(
        default_factory=list, description="COLLATE explícitos encontrados en el SQL."
    )
    destructive: bool = Field(
        False, description="Contiene DROP o TRUNCATE. " + _SQL_FACTS_DESC
    )
    created_at: datetime
    updated_at: datetime


class PartialApplicationOut(BaseModel):
    """Una migración que quedó APLICADA A MEDIAS en esta BD."""

    version: str | None = None
    model_migration_id: int
    applied_statements: int = Field(
        0, description="Sentencias que ya commitearon con éxito (el DDL va en AUTOCOMMIT)."
    )
    total_statements: int = Field(0, description="Sentencias totales de la migración.")
    reconcilable: bool = Field(
        False,
        description=(
            "True si el gateway puede deshacer automáticamente lo aplicado "
            "(POST /migrations/reconcile-partial). Requiere manifiesto de sentencias."
        ),
    )
    reason: str | None = Field(
        None, description="Por qué NO es reconciliable automáticamente, si aplica."
    )
    statements_to_undo: int = Field(0, description="Reversos que se ejecutarían.")


class MigrationStatusOut(BaseModel):
    """Estado de una BD gestionada frente a las migraciones de su blueprint."""

    managed_database_id: int
    model_id: int | None = None
    slug: str | None = None
    database_exists: bool = Field(
        True,
        description=(
            "False si la BD NO existe en el motor destino: quedó registrada en el inventario "
            "sin aprovisionarse, o alguien la borró por fuera del gateway. Con esto en false, "
            "'current_version' es null por AUSENCIA y no por 'todavía sin migraciones', y "
            "'pending_versions' lista TODAS las del blueprint. Toda operación que ejecuta "
            "(apply/rollback/stamp/reconcile-partial) responde 409 hasta que se aprovisione "
            "con POST /managed-databases/{id}/provision."
        ),
    )
    current_version: str | None = None
    latest_available: str | None = None
    pending_count: int
    pending_versions: list[str]
    has_partial_application: bool = Field(
        False,
        description=(
            "True si alguna migración quedó aplicada a medias. OJO: 'current_version' NO "
            "lo refleja — Alembic solo registra la versión cuando el upgrade TERMINA, así "
            "que el estado se lee como sano aunque la BD tenga cambios físicos de una "
            "versión que el ledger no conoce. Con esto en true, 'rollback' responde 409."
        ),
    )
    partial_application: list[PartialApplicationOut] = Field(default_factory=list)


class MigrationReconcileStatementOut(BaseModel):
    seq: int
    status: str | None = None  # applied | failed (ausente en dry_run)
    sql: str | None = None  # solo en dry_run
    error: str | None = None
    execution_ms: int | None = None


class MigrationReconcilePartialOut(BaseModel):
    """
    Resultado de reconciliar una aplicación PARCIAL: deshacer las sentencias que sí
    corrieron de una migración que falló a mitad.

    NO toca la tabla de versión de Alembic: la versión parcial nunca se registró, así que
    esto no es un ``downgrade`` sino una compensación que devuelve el plano físico al
    estado que el ledger ya afirma.
    """

    managed_database_id: int
    database_name: str
    server_id: int
    version: str
    applied_statements: int
    total_statements: int
    statements_to_undo: int
    unreversible_statements: list[dict] = Field(
        default_factory=list,
        description="Sentencias aplicadas SIN reverso: quedan en la BD (solo con force=true).",
    )
    unconfirmed_reverses: list[dict] = Field(
        default_factory=list,
        description=(
            "Reversos que existen pero NO son demostrablemente seguros: pueden fallar (una "
            "UNIQUE/CHECK/FK que se re-crea valida los datos actuales) o no restaurar los "
            "datos (recrear una tabla borrada devuelve la estructura, no las filas). "
            "Revisalos en el dry-run: no bloquean, son el mejor reverso disponible."
        ),
    )
    dry_run: bool = False
    undone_count: int = 0
    failed: bool = False
    fully_reconciled: bool = Field(
        False, description="True si la BD volvió a coincidir con su versión registrada."
    )
    remaining_applied_statements: int = Field(
        0, description="Sentencias que siguen aplicadas si la reconciliación no terminó."
    )
    statements: list[MigrationReconcileStatementOut] = Field(default_factory=list)
    results: list[MigrationReconcileStatementOut] = Field(default_factory=list)


class MigrationResultOut(BaseModel):
    """Resultado de aplicar/revertir una migración sobre una BD."""

    migration_id: int
    version: str
    status: str  # applied | failed
    error: str | None = None
    execution_ms: int
    resumed: bool = Field(
        False, description="True si este intento retomó desde un checkpoint parcial previo"
    )
    resumed_from_statement: int | None = Field(
        None, description="Sentencia (1-based) desde la que se retomó, si resumed=true"
    )
    statement_total: int | None = Field(
        None, description="Cantidad total de sentencias de esta migración/dirección"
    )
    failed_at_statement_index: int | None = Field(
        None,
        description=(
            "Sentencia (1-based) en la que falló, si status=failed. Null si no se pudo "
            "determinar (migración no resumible) o si status=applied."
        ),
    )


class MigrationAutoReconcileOut(BaseModel):
    """
    Auto-reconciliación ejecutada por el sistema tras un fallo a mitad de una migración.

    Existe para que un `apply` fallido no deje la BD en un estado que el admin tenga que
    entender y desarmar a mano. Se deshacen EXACTAMENTE las sentencias que alcanzaron a
    commitear, en orden inverso, y la BD vuelve a su versión anterior.
    """

    version: str = Field(..., description="Versión que falló y se deshizo")
    attempted: bool = True
    undone_count: int = 0
    statements_to_undo: int = 0
    fully_reconciled: bool = Field(
        False, description="True si la BD volvió a coincidir con su versión registrada"
    )
    unconfirmed_reverses: list[dict] = Field(
        default_factory=list,
        description="Reversos ejecutados que NO son demostrablemente seguros (revisar)",
    )
    unreversible_statements: list[dict] = Field(
        default_factory=list,
        description="Sentencias aplicadas sin reverso: siguen en la BD",
    )
    error: str | None = None


class MigrationApplyOut(BaseModel):
    """
    Resultado de `POST .../migrations/apply` (cubre apply real y dry-run).

    Una sola llamada aplica TODAS las pendientes en orden hasta `target_version`
    (o hasta la última si se omite). `from_version`→`to_version` reportan el salto real;
    `no_op=true` cuando no había nada que aplicar (ya al día o versión pedida ≤ actual).
    """

    managed_database_id: int
    database_name: str | None = None
    server_id: int | None = None
    from_version: str | None = Field(None, description="Versión de la BD ANTES de aplicar")
    to_version: str | None = Field(None, description="Versión de la BD DESPUÉS de aplicar")
    target_version: str | None = Field(
        None, description="Versión objetivo solicitada; null = última disponible"
    )
    applied_count: int = 0
    failed: bool = False
    quarantined: bool = False
    no_op: bool = Field(False, description="True si no había migraciones que aplicar")
    dry_run: bool = False
    database_exists: bool = Field(
        True,
        description=(
            "Solo en dry-run: False si la BD no existe en el motor destino (registrada sin "
            "aprovisionar, o borrada por fuera). El dry-run INFORMA y no falla —es la llamada "
            "de diagnóstico—, pero fuerza 'no_op'; el apply real responde 409 "
            "'managed_database.not_provisioned'."
        ),
    )
    pending_versions: list[str] = Field(default_factory=list)
    environment_slug: str | None = Field(
        None, description="Entorno de esta BD, o null si no está clasificada."
    )
    blocked_by: list[str] = Field(
        default_factory=list,
        description=(
            "Solo en dry-run: versiones pendientes que el entorno bloquearía por ser "
            "destructivas. INFORMATIVO — el dry-run no falla, justamente para que se pueda ver "
            "qué frena el apply. En el apply real esas versiones devuelven 409 "
            "'environment.destructive_blocked'."
        ),
    )
    results: list[MigrationResultOut] = Field(default_factory=list)
    captured_versions: list[str] = Field(
        default_factory=list,
        description=(
            "Versiones en las que ESTA corrida escribió capturas. No es adorno: es lo que hace "
            "falta para armar el enlace a GET .../migrations/{version}/select-results. Sin este "
            "campo el cliente adivinaba con 'to_version', así que un apply 0005→0010 cuya "
            "captura ocurrió en 0007 enlazaba a una página vacía."
        ),
    )
    will_capture_versions: list[str] = Field(
        default_factory=list,
        description=(
            "Solo en dry-run: versiones pendientes que van a guardar el resultado de sus SELECT "
            "(filas de esta base, cifradas) en el gateway. Es un AVISO, no un bloqueo — el plan "
            "no falla por esto. Reemplaza al 409 de consentimiento por corrida que se retiró: la "
            "información se da en la llamada que existe para decidir, no trabando la que existe "
            "para ejecutar. Distinto de 'captured_versions', que es el HECHO de la corrida real."
        ),
    )
    captured_select_count: int = Field(
        0,
        description=(
            "Cuántas capturas de resultados de SELECT escribió ESTA corrida (no lo que haya "
            "acumulado en la tabla de corridas anteriores: contarlo así afirmaba escrituras "
            "que no habían ocurrido). Es un PUNTERO, no los datos: las filas nunca viajan acá "
            "(con LOGGER_MIDDLEWARE_SHOW_BODY=true irían al log de aplicación). Se leen —las "
            "de esta corrida y las anteriores— con "
            "GET /managed-databases/{id}/migrations/{version}/select-results."
        ),
    )
    select_results_available: bool = Field(
        False, description="Atajo de captured_select_count > 0 para el frontend."
    )
    reconciliation: MigrationAutoReconcileOut | None = Field(
        None,
        description=(
            "Qué hizo el sistema por su cuenta si una migración falló A MITAD (solo puede "
            "pasar en MySQL/MariaDB: en PostgreSQL el motor deshace la migración solo). "
            "null = no hizo falta. Con 'fully_reconciled=true' la BD volvió limpia a su "
            "versión anterior y NO queda en cuarentena: solo hay que corregir la migración."
        ),
    )


class MigrationRollbackOut(BaseModel):
    """
    Resultado de `POST .../migrations/rollback`. Revierte SECUENCIALMENTE en una sola
    llamada desde `from_version` hasta `to_version` (= `target_version` solicitado, o
    una versión menos si se omitió). `reverted_versions` lista lo deshecho (de la más
    reciente a la más antigua).
    """

    managed_database_id: int
    database_name: str | None = None
    server_id: int | None = None
    from_version: str | None = Field(None, description="Versión ANTES de revertir")
    to_version: str | None = Field(None, description="Versión DESPUÉS de revertir (null = base)")
    target_version: str | None = Field(None, description="Destino solicitado/resuelto")
    reverted_count: int = 0
    failed: bool = False
    quarantined: bool = False
    no_op: bool = False
    reverted_versions: list[str] = Field(default_factory=list)
    results: list[MigrationResultOut] = Field(default_factory=list)
    captured_select_count: int = Field(
        0,
        description=(
            "Capturas de SELECT que escribió ESTE rollback (puntero). 0 es lo normal cuando "
            "el down_sql no tiene lecturas, aunque la versión sí tenga capturas de su apply: "
            "esas se leen igual con GET .../select-results."
        ),
    )
    captured_versions: list[str] = Field(
        default_factory=list,
        description=(
            "Versiones en las que ESTE rollback escribió capturas. Es lo que hace falta para "
            "enlazar a GET .../migrations/{version}/select-results sin adivinar."
        ),
    )
    select_results_available: bool = Field(
        False, description="Atajo de captured_select_count > 0 para el frontend."
    )


class MigrationSelectResultItemOut(BaseModel):
    """
    UNA sentencia de lectura capturada dentro de una migración.

    ``durability='rolled_back'`` (solo posible en PostgreSQL) significa que el motor deshizo
    la transacción de la migración: las filas son diagnóstico de lo que se vio DURANTE el
    intento, no el estado final de la BD.
    """

    statement_index: int = Field(
        ..., description="Índice 1-based de la sentencia (el mismo que usa el checkpoint)"
    )
    direction: str = Field(..., description="'up' (apply) | 'down' (rollback)")
    sql: str = Field(..., description="Sentencia capturada (recortada, sin contraseñas)")
    sql_hash: str
    status: str = Field(..., description="'ok' | 'error' (la sentencia corrió; la captura falló)")
    durability: str = Field(..., description="'committed' | 'rolled_back' | 'unknown'")
    columns: list[str] = Field(default_factory=list)
    rows: list[list] = Field(
        default_factory=list,
        description="Filas como LISTAS (no dicts): una consulta puede repetir nombres de columna.",
    )
    row_count: int = 0
    truncated: bool = Field(
        False, description="True si el resultado real excedía los topes de filas/bytes."
    )
    payload_bytes: int = 0
    error: str | None = None
    captured_at: datetime
    migration_checksum: str


class MigrationSelectResultsOut(BaseModel):
    """Capturas de una versión sobre UNA BD gestionada. Lectura AUDITADA (fail-closed)."""

    managed_database_id: int
    database_name: str | None = None
    server_id: int | None = None
    model_migration_id: int
    version: str
    capture_selects: bool = Field(
        False, description="Si la versión tiene la captura habilitada actualmente."
    )
    stale: bool = Field(
        False,
        description=(
            "True si alguna captura fue tomada con un checksum distinto al actual de la "
            "versión: describe un SQL que ya cambió. (El PATCH que edita el SQL purga estas "
            "filas, así que en la práctica solo se ve con datos previos a ese fix.)"
        ),
    )
    expected_indexes: list[int] = Field(
        default_factory=list,
        description=(
            "Índices de sentencia que HOY serían capturables en el 'up' de esta versión "
            "para el motor de esta BD. Se derivan en el momento de la lectura, no se "
            "persisten."
        ),
    )
    missing_indexes: list[int] = Field(
        default_factory=list,
        description=(
            "De los esperados, los que no tienen captura: típicamente la migración falló "
            "antes de llegar a esa sentencia, o se aplicó cuando la captura estaba apagada."
        ),
    )
    durability_warning: str | None = Field(
        None,
        description="Presente si alguna captura quedó 'rolled_back' (el motor deshizo la migración).",
    )
    items: list[MigrationSelectResultItemOut] = Field(default_factory=list)


class MigrationHistoryOut(BaseModel):
    """Entrada del historial de aplicación de migraciones de una BD gestionada."""

    model_config = ConfigDict(from_attributes=True)

    id: int
    managed_database_id: int
    model_migration_id: int
    version: str | None = None  # versión de la migración (join), si existe
    applied_at: datetime
    status: str
    error: str | None = None
    execution_ms: int | None = None


class ApplyAllItemOut(BaseModel):
    """Resultado del apply masivo para una BD del blueprint (apply o dry-run)."""

    managed_database_id: int
    database_name: str | None = None
    server_id: int | None = None
    ok: bool
    applied: list[MigrationResultOut] = Field(default_factory=list)
    dry_run: bool = False
    pending_versions: list[str] = Field(default_factory=list)
    error: str | None = None
    error_code: str | None = Field(
        None,
        description=(
            "Código estable del rechazo (p. ej. 'environment.destructive_blocked'). Va APARTE "
            "de 'error', que es prosa para mostrar: el except del lote se queda solo con el "
            "mensaje y descarta el public_context, así que este es el único transporte del "
            "código para los rechazos por BD."
        ),
    )
    environment_slug: str | None = Field(
        None, description="Entorno de esta BD, o null si no está clasificada."
    )
    blocked_by: list[str] = Field(
        default_factory=list,
        description=(
            "Versiones que el entorno bloquea. En dry-run es INFORMATIVO (el plan no falla); "
            "en un apply real acompaña al rechazo."
        ),
    )
    captured_select_count: int = Field(
        0,
        description=(
            "Filas de SELECT capturadas en esta BD durante la corrida. Paridad con "
            "MigrationApplyOut: sin este dato, tras un apply masivo no había forma de saber "
            "en qué BDs quedaron capturas."
        ),
    )
    captured_versions: list[str] = Field(
        default_factory=list,
        description=(
            "Versiones en las que se escribieron capturas en ESTA BD. Sin esto el cliente "
            "adivinaba con la última versión aplicada del ítem, que no tiene por qué ser en la "
            "que se capturó."
        ),
    )
    unreviewed_capture: list[str] = Field(
        default_factory=list,
        description=(
            "Versiones con captura SIN revisar que frenaron a esta BD. Acompaña a "
            "error_code='migration.capture_unreviewed'. Este rechazo viaja por ítem dentro de "
            "una respuesta 200 (el guard corre por BD dentro del bucle), así que el "
            "public_context de la respuesta HTTP no existe para él."
        ),
    )
    select_results_available: bool = Field(
        False,
        description=(
            "Hay resultados legibles en GET /managed-databases/{id}/migrations/{version}"
            "/select-results para esta BD."
        ),
    )


class ApplyAllOut(BaseModel):
    model_id: int
    total_databases: int = Field(
        ..., description="TODAS las BDs del blueprint. No refleja los filtros del lote."
    )
    matched_databases: int = Field(
        0,
        description=(
            "BDs que coincidieron con los filtros ANTES del tope 'max_databases'. Comparado "
            "con 'processed' dice si hubo recorte: sin este número, 'processed 3 / total 40' "
            "no distingue 'sobraron 37' de 'en ese entorno solo había 3'."
        ),
    )
    processed: int
    results: list[ApplyAllItemOut]


# --------------------------------------------------------------------------- #
# Validación estática del SQL de una migración (sin aplicar)                   #
# --------------------------------------------------------------------------- #
class MigrationValidateIn(BaseModel):
    """
    Entrada del validador. O bien ``up_sql`` (borrador que aún no existe como versión), o
    bien ``version`` (una ya guardada, para no reenviar el SQL). Si llegan las dos, manda
    ``up_sql``: es lo que el usuario tiene delante en el formulario.
    """

    up_sql: str | None = Field(None, max_length=_MAX_SQL)
    version: str | None = Field(None, pattern=_VERSION)
    managed_database_id: int | None = Field(
        None,
        description=(
            "Si se indica, además del análisis estático se comprueba contra el catálogo de "
            "esa BD que las tablas referenciadas existan. Abre una conexión de solo lectura."
        ),
    )


class ValidateStatementOut(BaseModel):
    """Una sentencia analizada. Misma forma que `QueryStatementPlanOut` de la consola SQL."""

    seq: int
    sql: str
    kind: str
    danger: str
    reasons: list[dict] = Field(default_factory=list)
    seeds: bool = False
    destructive: bool = False
    collations: list[str] = Field(default_factory=list)
    parse_error: str | None = None


class MigrationValidateOut(BaseModel):
    statements: list[ValidateStatementOut] = Field(default_factory=list)
    has_seed: bool = False
    forced_collations: list[str] = Field(default_factory=list)
    forced_charsets: list[str] = Field(default_factory=list)
    destructive_statements: list[int] = Field(default_factory=list)
    parse_errors: list[dict] = Field(default_factory=list)
    gateway_internal_tables: list[str] = Field(default_factory=list)
    postgresql_blockers: list[str] = Field(
        default_factory=list,
        description=(
            "Construcciones que no se traducen con certeza a PostgreSQL. No vacío = el apply "
            "contra un destino PostgreSQL respondería 422 salvo que se defina un "
            "'up_sql_postgresql' explícito."
        ),
    )
    resumable: bool = True
    referenced_tables: list[str] = Field(
        default_factory=list,
        description="Tablas que el SQL necesita preexistentes (no las que él mismo crea).",
    )
    checked_database: str | None = Field(
        None, description="BD contra la que se verificó la existencia de objetos, si se pidió."
    )
    missing_tables: list[str] = Field(
        default_factory=list,
        description=(
            "Tablas que el SQL necesita PREEXISTENTES y no existen en 'checked_database'. "
            "Excluye las que la propia migración crea: sin eso, un baseline —que es todo "
            "CREATE TABLE— reportaba cada una de sus tablas como ausente. La comparación es "
            "insensible a mayúsculas a propósito: MySQL sobre Linux distingue y PostgreSQL "
            "pliega a minúsculas, y un validador que grita en falso deja de leerse."
        ),
    )
    catalog_error: str | None = Field(
        None,
        description=(
            "Por qué no se pudo leer el catálogo de 'checked_database' (motor caído, "
            "credencial inválida). El análisis estático se devuelve igual: perder también lo "
            "que sí se pudo comprobar sin conexión sería el peor desenlace."
        ),
    )
    pending_before: list[str] = Field(
        default_factory=list,
        description=(
            "Versiones que la BD comprobada tiene pendientes ANTES de la validada. Si no está "
            "vacío, las tablas que ESAS versiones crean todavía no existen: lo que falla es la "
            "premisa de la comprobación, no el SQL, y 'missing_tables' hay que leerlo con eso "
            "delante."
        ),
    )
    blueprint_collation: str | None = None
    collation_conflicts: list[str] = Field(
        default_factory=list,
        description="COLLATE forzados que difieren del declarado por el blueprint.",
    )


class MigrationEditPreviewIn(BaseModel):
    """
    Cuerpo de ``POST .../migrations/{version}/edit-preview``.

    Son los MISMOS campos de SQL del PATCH, y tienen que serlo: el token se ata al checksum
    que la versión tendrá una vez aplicados, así que previsualizar con un SQL y mandar otro
    en el PATCH invalida el token. Los campos no enviados conservan su valor actual, igual
    que en el PATCH.
    """

    up_sql: str | None = Field(None, min_length=1, max_length=_MAX_SQL)
    down_sql: str | None = Field(None, max_length=_MAX_SQL)
    up_sql_mysql: str | None = Field(None, max_length=_MAX_SQL)
    up_sql_postgresql: str | None = Field(None, max_length=_MAX_SQL)


class MigrationEditPreviewOut(BaseModel):
    """
    Qué habilita la edición y a QUIÉN va a dejar divergente.

    ``blocking_databases`` no es informativo de más: es el conjunto de BDs cuyo plano físico
    va a dejar de coincidir con lo que la versión declara. Se lee del MOTOR, no de la caché
    del inventario, porque es la información por la que existe esta llamada.
    """

    model_id: int
    version: str
    requires_confirmation: bool = Field(
        ...,
        description=(
            "False = ninguna BD tiene esta versión vigente, así que el PATCH común ya la "
            "edita y NO se emite token. True = hace falta reenviar 'confirm_version' + "
            "'confirm_token' en el PATCH."
        ),
    )
    blocking_databases: list[dict] = Field(
        default_factory=list,
        description=(
            "BDs que hoy tienen la versión aplicada y quedarán divergentes. Cada ítem trae "
            "'managed_database_id', 'reason' (vocabulario cerrado: still_applied, "
            "unreadable, unknown_database, unknown_blueprint) y 'current_version'. Una BD "
            "ILEGIBLE cuenta como bloqueante (fail-closed): un motor caído no es prueba de "
            "que ya no tenga la versión."
        ),
    )
    resulting_checksum: str = Field(
        ..., description="Checksum que tendrá la versión con el SQL propuesto."
    )
    confirm_version: str = Field(..., description="Valor a reenviar en el PATCH.")
    confirm_token: str | None = Field(
        None, description="Token a reenviar en el PATCH. null si no hace falta confirmar."
    )
    expires_at: datetime | None = Field(
        None, description="Vencimiento del token. Vencido: pedir el preview de nuevo (410)."
    )
