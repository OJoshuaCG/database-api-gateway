"""
Controller de ManagedDatabase (bases de datos gestionadas).

Orquesta el inventario del gateway y el aprovisionamiento real en el motor:
CREATE DATABASE, DROP DATABASE y reasignación de propietario. **No otorga privilegios**: crear
una BD (con o sin ``provision``) no le da ningún GRANT al propietario — se asignan aparte, de
forma explícita y granular, vía ``POST /server-users/{id}/grants``.

Consistencia GW↔motor (sin rollback silencioso):
    insertar status=pending → ejecutar DDL → status=active (éxito)
                                          └→ status=error  (falla; detalle en notas)
El registro en estado ``error`` se conserva para auditoría/reintento; el error HTTP
real (502/504/409/...) se propaga al cliente.

Con ``provision=False`` la fila queda en ``pending`` y el motor no se toca. ``provision_database``
es la vía para completar ese alta después, sin borrar la fila.

Integridad: el propietario debe ser un ServerUser del MISMO servidor (se valida en
el controller; endurecimiento futuro con FK compuesta — ver docs/plans/00).
"""

from sqlalchemy.exc import IntegrityError

from app.controllers.common import build_target, engine_value, get_server_or_404
from app.controllers.environment_controller import EnvironmentController
from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.core.remote_engine import DUPLICATE_DATABASE_CODES
from app.exceptions import AppHttpException
from app.models.database_model import DatabaseModel
from app.models.enums import EngineType, ProvisionStatus
from app.models.managed_database import ManagedDatabase
from app.models.model_migration import ModelMigration
from app.models.server import Server
from app.models.server_user import ServerUser
from app.services import audit, charset_catalog
from app.services import environment_catalog as ecodes
from app.services import provisioning_catalog as pcodes
from app.services.db_admin.factory import get_adapter
from app.services.db_admin.identifiers import (
    ensure_not_reserved_database,
    validate_identifier,
)

#: Marca del bloque de diagnóstico que escribe el gateway dentro de ``notes``. Todo lo que NO
#: empieza con esto es del operador y no se toca.
_GW_NOTE_MARK = "[gateway]"


def _merge_note(existing: str | None, detail: str) -> str:
    """
    Conserva la nota del operador y REEMPLAZA el bloque de diagnóstico del gateway.

    Se reemplaza en vez de acumular: si no, cinco reintentos fallidos dejan cinco líneas de lo
    mismo y la nota del operador queda enterrada.
    """
    kept = "\n".join(
        ln for ln in (existing or "").splitlines() if not ln.startswith(_GW_NOTE_MARK)
    ).strip()
    line = f"{_GW_NOTE_MARK} {detail}"
    return f"{kept}\n{line}" if kept else line


def _is_duplicate_database(exc: AppHttpException) -> bool:
    """
    ¿El motor rechazó un ``CREATE DATABASE`` porque la base YA existe?

    El status no alcanza: ``map_driver_error`` manda a 409 tanto 1007/42P04 (la base existe)
    como 1396/42710/2BP01 (usuario duplicado, objeto duplicado, dependencias). Hay que mirar el
    código nativo, que viaja como STRING en ``context["remote_error_code"]``.
    """
    ctx = getattr(exc, "context", None)
    return (
        getattr(exc, "status_code", None) == 409
        and isinstance(ctx, dict)
        and str(ctx.get("remote_error_code") or "") in DUPLICATE_DATABASE_CODES
    )


class ManagedDatabaseController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    @staticmethod
    def _serialize(d: ManagedDatabase) -> dict:
        return {
            "id": d.id,
            "name": d.name,
            "server_id": d.server_id,
            "owner_id": d.owner_id,
            "model_id": d.model_id,
            "model_version": d.model_version,
            "environment_id": d.environment_id,
            "charset": d.charset,
            "collation": d.collation,
            "status": d.status,
            "notes": d.notes,
            "origin": d.origin,
            "created_at": d.created_at,
            "updated_at": d.updated_at,
        }

    def _get_or_404(self, session, db_id: int) -> ManagedDatabase:
        d = session.get(ManagedDatabase, db_id)
        if not d:
            raise AppHttpException(
                message="Base de datos gestionada no encontrada.",
                status_code=404,
                context={"managed_database_id": db_id},
            )
        return d

    def _serialize_by_id(self, db_id: int) -> dict:
        """Re-lee y serializa una BD por id en una sesión propia (estado ya commiteado)."""
        session = self._session()
        try:
            return self._serialize(self._get_or_404(session, db_id))
        finally:
            session.close()

    def _set_status(
        self,
        db_id: int,
        status: ProvisionStatus,
        *,
        detail: str | None = None,
        replace_notes: bool = True,
    ) -> None:
        """
        Fija el estado y, opcionalmente, deja un diagnóstico en ``notes``.

        ``replace_notes=False`` CONSERVA la nota del operador y solo reemplaza el bloque
        marcado del gateway (ver ``_merge_note``). El default sigue siendo ``True`` a
        propósito: ``create_database`` y ``_set_quarantine`` escriben un texto que la SPA lee
        tal cual (``useCreateManagedDatabase`` para el toast de error, ``isQuarantined`` en la
        vista de migraciones), y cambiarles el formato es una decisión de producto aparte.
        """
        session = self._session()
        try:
            d = session.get(ManagedDatabase, db_id)
            if d:
                d.status = status
                if detail is not None:
                    d.notes = detail if replace_notes else _merge_note(d.notes, detail)
                session.commit()
        finally:
            session.close()

    @staticmethod
    def _require_owner_on_server(session, owner_id: int, server_id: int) -> ServerUser:
        owner = session.get(ServerUser, owner_id)
        if not owner:
            raise AppHttpException(
                message="El propietario (server_user) no existe.",
                status_code=422,
                context={"owner_id": owner_id},
            )
        if owner.server_id != server_id:
            raise AppHttpException(
                message="El propietario pertenece a otro servidor.",
                status_code=409,
                context={"owner_id": owner_id, "server_id": server_id},
            )
        return owner

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def list_databases(
        self,
        *,
        server_id: int | None = None,
        owner_id: int | None = None,
        model_id: int | None = None,
        environment_id: int | None = None,
        only_unassigned: bool = False,
        status: str | None = None,
        engine: EngineType | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[dict], int]:
        session = self._session()
        try:
            # ``environment_id=None`` ya significa "sin filtro", así que "las que NO tienen
            # entorno" necesita un parámetro propio. Los dos juntos son una contradicción y se
            # rechazan: devolver lista vacía en silencio sería un filtro que miente.
            if only_unassigned and environment_id is not None:
                raise AppHttpException(
                    message=(
                        "'environment_id' y 'only_unassigned' son mutuamente excluyentes: "
                        "el segundo pide justamente las BDs sin entorno."
                    ),
                    status_code=422,
                    public_context={"code": ecodes.CODE_FILTER_CONFLICT},
                    context={"environment_id": environment_id},
                )
            q = session.query(ManagedDatabase)
            if server_id is not None:
                q = q.filter(ManagedDatabase.server_id == server_id)
            if owner_id is not None:
                q = q.filter(ManagedDatabase.owner_id == owner_id)
            if model_id is not None:
                q = q.filter(ManagedDatabase.model_id == model_id)
            if environment_id is not None:
                q = q.filter(ManagedDatabase.environment_id == environment_id)
            if only_unassigned:
                q = q.filter(ManagedDatabase.environment_id.is_(None))
            if status is not None:
                q = q.filter(ManagedDatabase.status == status)
            if engine is not None:
                q = q.join(Server, Server.id == ManagedDatabase.server_id).filter(
                    Server.engine == engine
                )
            total = q.count()
            rows = q.order_by(ManagedDatabase.id.desc()).limit(limit).offset(offset).all()
            return [self._serialize(r) for r in rows], total
        finally:
            session.close()

    def get_database(self, db_id: int) -> dict:
        session = self._session()
        try:
            return self._serialize(self._get_or_404(session, db_id))
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Escritura (inventario + motor)                                      #
    # ------------------------------------------------------------------ #
    def create_database(
        self, data: dict, *, provision: bool, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            server = get_server_or_404(session, data["server_id"])
            owner = self._require_owner_on_server(session, data["owner_id"], server.id)
            if data.get("model_id") is not None and not session.get(
                DatabaseModel, data["model_id"]
            ):
                raise AppHttpException(
                    message="El blueprint (model_id) no existe.",
                    status_code=422,
                    context={"model_id": data["model_id"]},
                )
            owner_username = owner.username
            target = build_target(server) if provision else None

            # Catálogo GLOBAL de charsets/collations. Se valida acá además de en
            # ``ServerDatabaseController`` porque este método es entrypoint público por sí
            # mismo (``POST /managed-databases``). Es idempotente: revalidar valores ya
            # canónicos devuelve exactamente los mismos. Se aplica también con
            # ``provision=False`` (la fila declara con qué charset se creará la BD; no tiene
            # sentido persistir una elección que el gateway rechazaría al aprovisionar).
            # NO aplica a ``adopt_database``: ahí el charset se LEE del motor (se registra la
            # realidad existente, no se elige nada).
            req_charset, req_collation = charset_catalog.resolve_enabled_combination(
                engine_value(server), data.get("charset"), data.get("collation")
            )

            # ``model_version`` en el alta se valida contra el blueprint, con el MISMO
            # criterio que ``adopt_database`` unas líneas más abajo. Antes se persistía el
            # string crudo del cliente: ``max_length=50`` no exige que sea numérico y
            # ``version_sort_key`` hace ``int(version)``, así que un "v3-hotfix" reventaba toda
            # comparación de versiones posterior. Es el mismo agujero que se cerró en el PATCH.
            declared_version = data.get("model_version")
            if declared_version is not None:
                if data.get("model_id") is None:
                    raise AppHttpException(
                        message=(
                            "'model_version' requiere 'model_id' (la versión pertenece a un "
                            "blueprint)."
                        ),
                        status_code=422,
                        context={"model_version": declared_version},
                    )
                if (
                    session.query(ModelMigration.id)
                    .filter(
                        ModelMigration.model_id == data["model_id"],
                        ModelMigration.version == declared_version,
                    )
                    .first()
                    is None
                ):
                    raise AppHttpException(
                        message=(
                            f"La versión {declared_version} no existe en el blueprint indicado."
                        ),
                        status_code=422,
                        context={
                            "model_id": data["model_id"],
                            "model_version": declared_version,
                        },
                    )

            # Entorno: valida que exista y esté activo, o resuelve el marcado ``is_default``
            # cuando no se manda. OJO con la semántica del POST: la ruta usa ``model_dump()``
            # sin ``exclude_unset``, así que ausente y ``null`` explícito son indistinguibles y
            # los DOS reciben el default. Está dicho en el ``description`` del campo.
            env_id = EnvironmentController.resolve_for_assignment(
                session, data.get("environment_id")
            )

            md = ManagedDatabase(
                name=data["name"],
                server_id=server.id,
                owner_id=owner.id,
                model_id=data.get("model_id"),
                model_version=data.get("model_version"),
                environment_id=env_id,
                charset=req_charset,
                collation=req_collation,
                status=ProvisionStatus.pending,
                notes=data.get("notes"),
            )
            session.add(md)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe una base de datos con ese nombre en el servidor.",
                    status_code=409,
                    context={"name": data.get("name")},
                ) from exc
            session.refresh(md)
            db_id, db_name = md.id, md.name
            charset, collation = md.charset, md.collation
            server_id = md.server_id
            result = self._serialize(md)
        finally:
            session.close()

        if provision:
            adapter = get_adapter(target)
            # Crear la BD en el motor. POLÍTICA: NO se otorga ningún privilegio al
            # propietario por defecto — un usuario sin privilegios no recibe ninguno
            # (jamás ALL PRIVILEGES; eso solo lo tiene la credencial pseudo-root de la
            # conexión). Los permisos se asignan después de forma explícita y granular.
            # En PostgreSQL el propietario queda como OWNER NATIVO de la BD (es la
            # propiedad, no un GRANT). Si CREATE falla, no quedó nada en el motor.
            try:
                adapter.create_database(
                    db_name, charset=charset, collation=collation, owner=owner_username
                )
            except AppHttpException as exc:
                self._set_status(
                    db_id,
                    ProvisionStatus.error,
                    detail=f"Error al crear la BD en el motor (HTTP {getattr(exc, 'status_code', '?')}).",
                )
                audit.record(
                    "managed_database.create",
                    status="error",
                    admin=admin,
                    target_type="managed_database",
                    target_id=db_id,
                    server_id=server_id,
                    touched_engine=True,
                    detail="fallo al crear la BD en el motor",
                )
                raise
            self._set_status(db_id, ProvisionStatus.active)
            result["status"] = ProvisionStatus.active

        audit.record(
            "managed_database.create",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=provision,
        )
        return result

    def adopt_database(self, data: dict, *, admin: dict | None = None) -> dict:
        """
        Adopta una BD que YA existe en el motor (Plan 09): registra metadata SIN
        ejecutar CREATE DATABASE. Verifica la existencia real (404 si no), exige un
        propietario válido del mismo servidor y marca ``origin='adopted'`` con estado
        ``active`` (ya existe). Idempotente: 409 si ya está en el inventario.
        """
        adopt_version = data.get("model_version")
        if adopt_version is not None and data.get("model_id") is None:
            raise AppHttpException(
                message="'model_version' requiere 'model_id' (la versión pertenece a un blueprint).",
                status_code=422,
                context={"model_version": adopt_version},
            )
        session = self._session()
        try:
            server = get_server_or_404(session, data["server_id"])
            self._require_owner_on_server(session, data["owner_id"], server.id)
            if data.get("model_id") is not None and not session.get(
                DatabaseModel, data["model_id"]
            ):
                raise AppHttpException(
                    message="El blueprint (model_id) no existe.",
                    status_code=422,
                    context={"model_id": data["model_id"]},
                )
            # Validar la versión de partida ANTES de insertar: así el adopt es atómico y
            # no deja una BD registrada-pero-sin-marcar si la versión no existe. (El único
            # fallo posible tras insertar queda siendo la conectividad al motor en el stamp.)
            if adopt_version is not None and (
                session.query(ModelMigration.id)
                .filter(
                    ModelMigration.model_id == data["model_id"],
                    ModelMigration.version == adopt_version,
                )
                .first()
                is None
            ):
                raise AppHttpException(
                    message=f"La versión {adopt_version} no existe en el blueprint indicado.",
                    status_code=422,
                    context={"model_id": data["model_id"], "model_version": adopt_version},
                )
            db_name, server_id = data["name"], server.id
            target = build_target(server)  # descifra mientras la sesión sigue abierta
        finally:
            session.close()

        # Verificar existencia REAL en el motor (solo lectura; no se ejecuta DDL).
        live = get_adapter(target).list_databases()
        if db_name not in live:
            raise AppHttpException(
                message="La base de datos no existe en el motor; no hay nada que adoptar.",
                status_code=404,
                context={"name": db_name, "server_id": server_id},
            )

        session = self._session()
        try:
            env_id = EnvironmentController.resolve_for_assignment(
                session, data.get("environment_id")
            )
            md = ManagedDatabase(
                name=db_name,
                server_id=server_id,
                owner_id=data["owner_id"],
                model_id=data.get("model_id"),
                environment_id=env_id,
                charset=data.get("charset"),
                collation=data.get("collation"),
                status=ProvisionStatus.active,
                origin="adopted",
                notes=data.get("notes"),
            )
            session.add(md)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe una base de datos con ese nombre en el servidor (¿ya adoptada?).",
                    status_code=409,
                    context={"name": db_name},
                ) from exc
            session.refresh(md)
            result = self._serialize(md)
            db_id = md.id
        finally:
            session.close()

        audit.record(
            "managed_database.adopt",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=False,
            detail="BD existente adoptada al inventario",
        )

        # Si el admin declaró que la BD ya está en una versión del blueprint, hacemos
        # 'stamp' de esa versión en el motor (sin ejecutar DDL): así el 'apply' posterior
        # no reintenta crear objetos que ya existen. Import diferido para evitar ciclo.
        if adopt_version is not None:
            from app.controllers.managed_migration_controller import (
                ManagedMigrationController,
            )

            # stamp valida que la versión exista en el blueprint (422 si no), marca el
            # motor y sincroniza model_version. Si falla, la BD queda adoptada pero sin
            # marcar; el admin puede reintentar POST /{id}/migrations/stamp.
            ManagedMigrationController().stamp(db_id, adopt_version, admin=admin)
            result = self._serialize_by_id(db_id)
        return result

    def provision_database(
        self, db_id: int, *, allow_recreate: bool = False, admin: dict | None = None
    ) -> dict:
        """
        Ejecuta el ``CREATE DATABASE`` faltante sobre una fila YA registrada.

        Es la salida para una BD que quedó registrada sin crearse en el motor (``pending``) o
        cuyo DDL de alta falló (``error``). Antes de que existiera, la única vía era borrar la
        fila y recrearla, perdiendo ``notes``, ``environment_id``, ``model_id`` y el historial
        de migraciones que la referencia.

        **No aplica las migraciones del blueprint** (eso es ``POST /{id}/migrations/apply``) y
        **no otorga privilegios**: misma política que ``create_database``.

        Si la BD YA existe en el motor responde **409**: adoptar una base preexistente es
        ``POST /managed-databases/adopt``, y hacerlo acá sería adoptar por la puerta de atrás.
        """
        session = self._session()
        try:
            md = self._get_or_404(session, db_id)
            server = get_server_or_404(session, md.server_id)
            owner = self._require_owner_on_server(session, md.owner_id, md.server_id)

            dialect = engine_value(server)
            target = build_target(server)
            owner_username = owner.username
            db_name, server_id = md.name, md.server_id
            previous_status = md.status
            req_charset, req_collation = md.charset, md.collation
        finally:
            session.close()

        # Guards de identificador que ``create_database`` NO hace (confía en el regex del
        # schema) y que ``ServerDatabaseController.create_database`` sí. Importan MÁS acá: la
        # fila pudo registrarse antes de que el guard existiera, y ahora vamos a emitir DDL con
        # ese nombre.
        validate_identifier(db_name, dialect, "base de datos")
        ensure_not_reserved_database(db_name, dialect)

        # Re-resolver contra el catálogo NO es redundante con el alta: la combinación pudo
        # DESHABILITARSE entre el registro y el aprovisionamiento, y lo que viaja al DDL tiene
        # que salir de la tabla (en PostgreSQL la collation va como literal de string). Si el
        # catálogo cambió, esto da 422 antes de tocar el motor: es preferible a emitir una
        # combinación que el propio gateway ya rechaza.
        charset, collation = charset_catalog.resolve_enabled_combination(
            dialect, req_charset, req_collation
        )

        self._guard_provision_status(db_id, previous_status, allow_recreate=allow_recreate)

        adapter = get_adapter(target)
        exists = db_name in adapter.list_databases()
        # ``error`` está SOBRECARGADO: lo escribe el CREATE de alta que falló (la BD no existe)
        # y también ``_set_quarantine`` tras una migración fallida (la BD sí existe). El
        # chequeo físico es lo único que los distingue, y por eso este guard va acá y no arriba.
        if exists and previous_status == ProvisionStatus.error:
            raise AppHttpException(
                message=(
                    f"La base de datos '{db_name}' ya existe en el motor: su estado 'error' es "
                    "una CUARENTENA por una migración fallida, no un aprovisionamiento "
                    "pendiente. Resolvela con POST /managed-databases/"
                    f"{db_id}/migrations/reconcile-partial o con apply?force=true."
                ),
                status_code=409,
                public_context={
                    "code": pcodes.CODE_QUARANTINED_NOT_MISSING,
                    "database": db_name,
                },
                context={"managed_database_id": db_id, "server_id": server_id},
            )
        if exists:
            raise AppHttpException(
                message=(
                    f"La base de datos '{db_name}' ya existe en el motor, así que no hay nada "
                    "que aprovisionar. Para traerla al inventario sin recrearla hay que quitar "
                    f"este registro (DELETE /managed-databases/{db_id}, sin drop_remote) y "
                    "adoptarla con POST /managed-databases/adopt."
                ),
                status_code=409,
                public_context={"code": pcodes.CODE_EXISTS_IN_ENGINE, "database": db_name},
                context={"managed_database_id": db_id, "server_id": server_id},
            )

        # Fail-closed ANTES del DDL: mismo criterio que ``ServerDatabaseController`` para este
        # mismo CREATE DATABASE. Si la auditoría no se puede persistir, la operación no ocurre.
        audit.record_intent(
            "managed_database.provision",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=True,
            detail=f"CREATE DATABASE (re-aprovisionamiento desde '{previous_status.value}')",
        )

        provisioned = True
        try:
            # Sin GRANT, igual que en el alta: crear una BD no otorga ningún privilegio (jamás
            # ALL PRIVILEGES; eso solo lo tiene la credencial pseudo-root de la conexión). En
            # PostgreSQL el OWNER nativo lo pone el propio CREATE DATABASE.
            adapter.create_database(
                db_name, charset=charset, collation=collation, owner=owner_username
            )
        except AppHttpException as exc:
            # El ``list_databases()` de arriba es CONSEJO, no barrera: hay una ventana TOCTOU
            # entre él y el CREATE. Dos llamadas simultáneas al mismo endpoint sobre la misma
            # fila terminan acá, y la que pierde recibe 1007/42P04 — que es un ÉXITO por
            # carrera, no un error: la BD existe y es la de esta fila. Devolver 409 por un
            # resultado correcto sería mentir. (Distinto del 409 de arriba, que rechaza adoptar
            # una base preexistente AJENA.)
            if not _is_duplicate_database(exc):
                self._set_status(
                    db_id,
                    ProvisionStatus.error,
                    detail=(
                        "Error al aprovisionar la BD en el motor "
                        f"(HTTP {getattr(exc, 'status_code', '?')})."
                    ),
                    replace_notes=False,
                )
                audit.record(
                    "managed_database.provision",
                    status="error",
                    admin=admin,
                    target_type="managed_database",
                    target_id=db_id,
                    server_id=server_id,
                    touched_engine=True,
                    detail="fallo al aprovisionar la BD en el motor",
                )
                raise
            provisioned = False

        self._set_status(db_id, ProvisionStatus.active)
        audit.record(
            "managed_database.provision",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=True,
            detail=(
                None if provisioned else "convergencia por carrera: la BD ya había sido creada"
            ),
        )
        return {
            # Se RELEE en vez de parchear la serialización previa al DDL: así ``updated_at`` y
            # ``notes`` son los reales y no los del momento anterior al cambio de estado.
            "database": self._serialize_by_id(db_id),
            "provisioned": provisioned,
            "previous_status": previous_status,
            "charset": charset,
            "collation": collation,
        }

    @staticmethod
    def _guard_provision_status(
        db_id: int, status: ProvisionStatus, *, allow_recreate: bool
    ) -> None:
        """
        Estados que NO se aprovisionan. ``pending`` y ``error`` siguen de largo: el primero es
        el caso normal y el segundo lo desambigua el chequeo físico del llamador.
        """
        if status == ProvisionStatus.active and not allow_recreate:
            raise AppHttpException(
                message=(
                    "El inventario marca esta base de datos como activa. Si la borraron por "
                    "fuera del gateway y hace falta recrearla, repetí la llamada con "
                    "allow_recreate=true; si no, revisá el servidor antes de emitir DDL."
                ),
                status_code=409,
                public_context={"code": pcodes.CODE_ALREADY_ACTIVE},
                context={"managed_database_id": db_id, "required": "allow_recreate=true"},
            )
        if status == ProvisionStatus.archived:
            # Sin escape, a propósito. Hoy nada en ``app/`` escribe ``archived``, así que este
            # guard es inalcanzable — razón de más para que sea el estricto: el día que se
            # cablee la transición, este endpoint no va a revivir una BD retirada por accidente.
            raise AppHttpException(
                message=(
                    "La base de datos está archivada (retirada del uso). Reactivala en el "
                    "inventario antes de aprovisionarla."
                ),
                status_code=409,
                public_context={"code": pcodes.CODE_ARCHIVED},
                context={"managed_database_id": db_id},
            )

    def update_database(
        self, db_id: int, data: dict, *, admin: dict | None = None
    ) -> dict:
        """Actualiza solo metadatos del inventario (no ejecuta DDL en el motor)."""
        session = self._session()
        try:
            md = self._get_or_404(session, db_id)
            if data.get("model_id") is not None and not session.get(
                DatabaseModel, data["model_id"]
            ):
                raise AppHttpException(
                    message="El blueprint (model_id) no existe.",
                    status_code=422,
                    context={"model_id": data["model_id"]},
                )
            # ``environment_id`` se valida como en el alta (existe + activo), y ``None``
            # explícito DESCLASIFICA. Que esté en esta tupla es lo que hace posible
            # desclasificar: la asignación va por presencia de clave y la ruta usa
            # ``exclude_unset=True``, así que ``PATCH {"environment_id": null}`` sí distingue
            # "vaciar" de "no enviado" — pero solo si el campo se recorre acá.
            if "environment_id" in data and data["environment_id"] is not None:
                EnvironmentController.resolve_for_assignment(session, data["environment_id"])

            before = {
                "model_id": md.model_id,
                "environment_id": md.environment_id,
                "charset": md.charset,
                "collation": md.collation,
            }
            # ``model_version`` YA NO se acepta acá, y no es un olvido. Era escribible a
            # ciegas por el cliente, sin confirmación y sin rastro de qué cambió, así que
            # "declarar que esta BD está en la versión X" era un PATCH — y esa caché es la que
            # cualquier gate de promoción entre entornos tiene que leer. Además ``max_length=50``
            # no valida que sea numérico, y ``version_sort_key`` hace ``int(version)``: un
            # valor como "v3-hotfix" reventaba toda comparación de versiones.
            # La versión la escriben ``apply`` / ``rollback`` / ``stamp`` releyendo el motor.
            for field in ("model_id", "environment_id", "charset", "collation", "notes"):
                if field in data:
                    setattr(md, field, data[field])
            session.commit()
            session.refresh(md)
            after = {
                "model_id": md.model_id,
                "environment_id": md.environment_id,
                "charset": md.charset,
                "collation": md.collation,
            }
            changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
            result = self._serialize(md)
        finally:
            session.close()
        # Con ``detail`` vacío, una RECLASIFICACIÓN de entorno era indistinguible en el log de
        # un cambio de ``notes``. Para una columna de política eso no alcanza.
        audit.record(
            "managed_database.update",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            touched_engine=False,
            detail=" ".join(f"{k}:{old}->{new}" for k, (old, new) in changed.items())
            or "sin cambios efectivos",
        )
        return result

    def delete_database(
        self,
        db_id: int,
        *,
        drop_remote: bool,
        confirm_name: str | None = None,
        admin: dict | None = None,
    ) -> None:
        session = self._session()
        try:
            md = self._get_or_404(session, db_id)
            server = get_server_or_404(session, md.server_id)
            db_name, server_id = md.name, md.server_id
            target = build_target(server) if drop_remote else None
        finally:
            session.close()

        if drop_remote:
            # Confirmación explícita (doble intención) para una operación IRREVERSIBLE:
            # el cliente debe repetir el nombre exacto de la BD.
            if confirm_name != db_name:
                raise AppHttpException(
                    message=(
                        "Confirmación requerida: para ejecutar DROP DATABASE en el motor, "
                        "'confirm_name' debe coincidir exactamente con el nombre de la base de datos."
                    ),
                    status_code=422,
                    context={"managed_database_id": db_id, "required": "confirm_name == name"},
                )
            # Auditar la INTENCIÓN antes de la acción irreversible (queda traza aunque
            # el proceso muera entre el DROP y el registro del resultado).
            audit.record(
                "managed_database.delete",
                status="attempt",
                admin=admin,
                target_type="managed_database",
                target_id=db_id,
                server_id=server_id,
                touched_engine=True,
                detail="DROP DATABASE solicitado (confirmado)",
            )
            get_adapter(target).drop_database(db_name)

        session = self._session()
        try:
            md = session.get(ManagedDatabase, db_id)
            if md:
                session.delete(md)
                session.commit()
        finally:
            session.close()

        audit.record(
            "managed_database.delete",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=drop_remote,
        )

    def reassign_owner(
        self, db_id: int, new_owner_id: int, *, provision: bool, admin: dict | None = None
    ) -> dict:
        session = self._session()
        try:
            md = self._get_or_404(session, db_id)
            server = get_server_or_404(session, md.server_id)
            new_owner = self._require_owner_on_server(session, new_owner_id, md.server_id)
            old_owner = session.get(ServerUser, md.owner_id)
            db_name, server_id = md.name, md.server_id
            new_username, new_host = new_owner.username, new_owner.host
            old_username = old_owner.username if old_owner else None
            old_host = old_owner.host if old_owner else "%"
            target = build_target(server) if provision else None
        finally:
            session.close()

        if provision:
            try:
                get_adapter(target).reassign_database_owner(
                    db_name,
                    new_username,
                    new_host=new_host,
                    old_owner=old_username,
                    old_host=old_host,
                )
            except AppHttpException:
                # PostgreSQL aplica ALTER OWNER, GRANT y REVOKE en pasos no atómicos
                # (distintas conexiones/BDs): un fallo a mitad puede dejar el motor en
                # estado parcial mientras el inventario conserva el dueño anterior. Se
                # registra para reconciliación posterior (ver docs/plans/06).
                audit.record(
                    "managed_database.reassign_owner",
                    status="error",
                    admin=admin,
                    target_type="managed_database",
                    target_id=db_id,
                    server_id=server_id,
                    touched_engine=True,
                    detail="fallo al reasignar propietario en el motor; posible estado parcial (revisar/reconciliar)",
                )
                raise

        session = self._session()
        try:
            md = self._get_or_404(session, db_id)
            md.owner_id = new_owner_id
            session.commit()
            session.refresh(md)
            result = self._serialize(md)
        finally:
            session.close()

        audit.record(
            "managed_database.reassign_owner",
            admin=admin,
            target_type="managed_database",
            target_id=db_id,
            server_id=server_id,
            touched_engine=provision,
        )
        return result
