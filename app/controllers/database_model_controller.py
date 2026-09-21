"""
Controller de DatabaseModel (blueprints/categorías).

CRUD puro sobre la BD de metadatos del gateway: NO toca ningún motor destino.
"""

import hashlib
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.database_model import DatabaseModel
from app.models.managed_database import ManagedDatabase
from app.models.model_migration import ModelMigration
from app.models.project import ProjectDatabaseModel
from app.core.logger import get_logger
from app.services import audit
from app.services import confirm_token as confirm_token_service
from app.services import database_model_catalog as dm_codes

if TYPE_CHECKING:
    from app.core.actor import Actor

logger = get_logger(__name__)


class DatabaseModelController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    @staticmethod
    def _serialize(m: DatabaseModel) -> dict:
        return {
            "id": m.id,
            "name": m.name,
            "slug": m.slug,
            "description": m.description,
            "current_version": m.current_version,
            "is_active": m.is_active,
            "charset": m.charset,
            "collation": m.collation,
            "created_at": m.created_at,
            "updated_at": m.updated_at,
        }

    def _get_or_404(self, session, model_id: int) -> DatabaseModel:
        m = session.get(DatabaseModel, model_id)
        if not m:
            raise AppHttpException(
                message="Blueprint no encontrado.",
                status_code=404,
                context={"model_id": model_id},
            )
        return m

    def list_models(self, *, limit: int, offset: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            total = session.query(DatabaseModel).count()
            rows = (
                session.query(DatabaseModel)
                .order_by(DatabaseModel.id.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [self._serialize(r) for r in rows], total
        finally:
            session.close()

    def get_model(self, model_id: int) -> dict:
        session = self._session()
        try:
            return self._serialize(self._get_or_404(session, model_id))
        finally:
            session.close()

    def create_model(self, data: dict, *, admin: "dict | Actor | None" = None) -> dict:
        session = self._session()
        try:
            model = DatabaseModel(
                name=data["name"],
                slug=data["slug"],
                description=data.get("description"),
                current_version=data.get("current_version", "0.0.0"),
                is_active=data.get("is_active", True),
                charset=data.get("charset"),
                collation=data.get("collation"),
            )
            session.add(model)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un blueprint con ese nombre o slug.",
                    status_code=409,
                    public_context={"code": dm_codes.CODE_NAME_OR_SLUG_TAKEN},
                    context={"slug": data.get("slug")},
                ) from exc
            session.refresh(model)
            result = self._serialize(model)
            model_id = model.id
        finally:
            session.close()
        audit.record(
            "database_model.create", admin=admin, target_type="database_model", target_id=model_id
        )
        return result

    def update_model(self, model_id: int, data: dict, *, admin: "dict | Actor | None" = None) -> dict:
        session = self._session()
        try:
            model = self._get_or_404(session, model_id)
            # `charset`/`collation` se pueden LIMPIAR (volver a "sin declarar"), así que no
            # pueden ir en el bucle de arriba, que ignora los `None` para distinguir "no
            # enviado" de "enviado vacío" en los campos obligatorios.
            for field in ("charset", "collation"):
                if field in data:
                    setattr(model, field, data[field])
            # El ``slug`` NO es una etiqueta: ``migrations.version_table_name`` lo usa
            # para nombrar la tabla de versión de Alembic (``_gw_v_{slug}``) DENTRO de cada
            # BD gestionada. Cambiarlo acá no renombra nada en los motores, así que la
            # contabilidad de todas esas bases queda huérfana de golpe: el gateway pasa a
            # leer una tabla inexistente, ``compute_pending`` reporta la cadena ENTERA como
            # pendiente y un ``apply`` la reaplica desde la 0001 sobre bases que ya tienen el
            # esquema. Ya ocurrió en producción. El ``name`` no nombra ninguna tabla y por
            # eso sigue siendo libre: renombrar el blueprint para una persona es seguro,
            # cambiar su identificador para el motor no lo es.
            nuevo_slug = data.get("slug")
            if nuevo_slug is not None and nuevo_slug != model.slug:
                bases = (
                    session.query(ManagedDatabase.id)
                    .filter(ManagedDatabase.model_id == model_id)
                    .count()
                )
                if bases:
                    raise AppHttpException(
                        message=(
                            f"El slug nombra la tabla de versión dentro de {bases} base(s) "
                            "gestionada(s), así que cambiarlo acá las dejaría huérfanas. "
                            f"Usá POST /database-models/{model_id}/rename-slug, que renombra "
                            "también en los motores con preview y confirmación."
                        ),
                        status_code=409,
                        public_context={
                            "code": dm_codes.CODE_SLUG_IN_USE,
                            "current_slug": model.slug,
                            "requested_slug": nuevo_slug,
                            "managed_database_count": bases,
                        },
                        context={"model_id": model_id},
                    )
            for field in ("name", "slug", "description", "current_version", "is_active"):
                if field in data and data[field] is not None:
                    setattr(model, field, data[field])
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un blueprint con ese nombre o slug.",
                    status_code=409,
                    public_context={"code": dm_codes.CODE_NAME_OR_SLUG_TAKEN},
                    context={"model_id": model_id},
                ) from exc
            session.refresh(model)
            result = self._serialize(model)
        finally:
            session.close()
        audit.record(
            "database_model.update", admin=admin, target_type="database_model", target_id=model_id
        )
        return result

    def delete_model(self, model_id: int, *, admin: "dict | Actor | None" = None) -> None:
        session = self._session()
        try:
            model = self._get_or_404(session, model_id)
            # Los vínculos con proyectos se sueltan explícitamente y no por el CASCADE de
            # la FK: SQLite no aplica claves foráneas salvo que se active
            # ``PRAGMA foreign_keys``, así que en test quedarían filas apuntando a un
            # blueprint inexistente. Lo que desaparece es la PERTENENCIA; los proyectos
            # siguen existiendo, solo con un blueprint menos.
            session.query(ProjectDatabaseModel).filter(
                ProjectDatabaseModel.model_id == model_id
            ).delete(synchronize_session=False)
            session.delete(model)
            session.commit()
        finally:
            session.close()
        audit.record(
            "database_model.delete", admin=admin, target_type="database_model", target_id=model_id
        )

    def list_model_databases(self, model_id: int, *, refresh: bool = False) -> list[dict]:
        """
        BDs gestionadas que replican este blueprint, **con su estado de despliegue**.

        Cada item lleva además ``pending_count``, ``pending_versions`` y
        ``has_partial_application``: es la respuesta a "¿qué BDs están al día y cuáles no?",
        que antes exigía una llamada por BD a ``/migrations/status``, y cada una de esas abre
        una conexión al motor.

        Aquí no se abre ninguna: ``managed_databases.model_version`` es una copia que el
        gateway ya mantiene (``_sync_model_version_from_engine`` tras cada apply),
        ``compute_pending`` es una función pura y el estado parcial vive en la BD del
        gateway. Son 3 queries locales para toda la tabla.

        Con ``refresh=True`` sí se relee la versión real de cada BD destino y se resincroniza
        la copia: es la vía para corregir el dato si alguien migró una BD por fuera del
        gateway. Eso convierte la llamada en 🔌 y por eso va con rate limit y auditoría en la
        ruta, no aquí.
        """
        from app.controllers.managed_database_controller import ManagedDatabaseController
        from app.services.db_admin import migration_progress
        from app.services.db_admin.migration_integrity import version_sort_key

        session = self._session()
        try:
            self._get_or_404(session, model_id)
            rows = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.model_id == model_id)
                .order_by(ManagedDatabase.id.desc())
                .all()
            )
            versions = [
                r[0]
                for r in session.query(ModelMigration.version)
                .filter(ModelMigration.model_id == model_id)
                .all()
            ]
            versions.sort(key=version_sort_key)
            data = [ManagedDatabaseController._serialize(r) for r in rows]
            current_by_id = {r.id: r.model_version for r in rows}
        finally:
            session.close()

        if refresh:
            current_by_id = self._resync_model_versions(model_id)
            for item in data:
                if item["id"] in current_by_id:
                    item["model_version"] = current_by_id[item["id"]]

        partial_ids = migration_progress.databases_with_incomplete_progress(
            [item["id"] for item in data]
        )
        for item in data:
            current = current_by_id.get(item["id"])
            cur_key = version_sort_key(current) if current else None
            pending = [
                v for v in versions if cur_key is None or version_sort_key(v) > cur_key
            ]
            item["pending_versions"] = pending
            item["pending_count"] = len(pending)
            item["has_partial_application"] = item["id"] in partial_ids
        return data

    def _resync_model_versions(self, model_id: int) -> dict[int, str | None]:
        """
        Relee la versión REAL de cada BD del blueprint y actualiza la copia del gateway. 🔌

        Una BD inalcanzable no rompe la tabla entera: se deja su valor cacheado y se sigue.
        Fallar todo porque un servidor de doce esté caído haría inútil la pantalla justo
        cuando más se necesita.
        """
        from app.controllers.common import build_target, get_server_or_404
        from app.controllers.managed_migration_controller import ManagedMigrationController

        controller = ManagedMigrationController()
        out: dict[int, str | None] = {}
        session = self._session()
        try:
            model = session.get(DatabaseModel, model_id)
            slug = model.slug if model else None
            rows = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.model_id == model_id)
                .all()
            )
            targets = {}
            for row in rows:
                out[row.id] = row.model_version
                if not slug:
                    continue
                try:
                    if row.server_id not in targets:
                        server = get_server_or_404(session, row.server_id)
                        targets[row.server_id] = build_target(server)
                    current = controller.runner.get_current_version(
                        targets[row.server_id], row.name, slug
                    )
                    # ``None`` acá significa "no encontré tabla de versión", y eso es
                    # INDISTINGUIBLE de "la BD está en base". Pisar con ``None`` una versión
                    # que el gateway ya tenía registrada destruye la única evidencia de dónde
                    # estaba parada esa base: es lo que borró el rastro de las BDs de un
                    # blueprint al que le cambiaron el slug, y lo que las hizo desaparecer del
                    # conteo de "aplicado a N BDs" (que se calcula sobre ``model_version``,
                    # no sobre ``model_id``). Un refresh es una LECTURA: si el motor deja de
                    # reportar una versión que antes existía, se conserva la registrada y se
                    # deja rastro. Reconciliar de verdad es trabajo de ``stamp``, que es una
                    # afirmación explícita del operador.
                    if current is None and row.model_version is not None:
                        logger.warning(
                            "resync: la BD %s (id=%s) no reporta tabla de versión para el "
                            "blueprint '%s'; se conserva la versión registrada %s. Puede ser "
                            "un slug renombrado o una tabla de versión borrada.",
                            row.name, row.id, slug, row.model_version,
                        )
                        continue
                    row.model_version = current
                    out[row.id] = current
                except AppHttpException:
                    continue
            session.commit()
        finally:
            session.close()
        return out

    # ------------------------------------------------------------------ #
    # Diagnóstico: qué contabilidad de versiones hay REALMENTE en cada BD  #
    # ------------------------------------------------------------------ #
    #: La BD tiene exactamente la tabla que el slug vigente predice. Nada que hacer.
    _VT_OK = "ok"
    #: NO tiene la esperada pero SÍ otras ``_gw_v_*``. Es la firma del incidente: el gateway
    #: lee un nombre que no existe, obtiene ``None`` y reporta la cadena ENTERA como
    #: pendiente, en silencio. La versión real está en la huérfana.
    _VT_ORPHANED = "orphaned"
    #: Tiene la esperada Y además otras. Residuo de un renombrado o de una recuperación a
    #: medias: el puntero vigente es correcto, pero hay basura que conviene limpiar.
    _VT_MIXED = "mixed"
    #: No tiene ninguna. Es lo NORMAL en una BD que nunca fue posicionada.
    _VT_NONE = "none"
    #: No se pudo leer. No se asume nada (fail-closed para cualquier decisión posterior).
    _VT_UNREACHABLE = "unreachable"

    def version_tables_report(self, model_id: int) -> dict:
        """Qué tablas de versión tiene REALMENTE cada BD del blueprint. 🔌 Solo lectura.

        Existe porque la contabilidad huérfana es INVISIBLE para el resto del gateway:
        ``get_current_version`` lee un único nombre —el que predice el slug vigente— y si no
        está devuelve ``None``, que es indistinguible de "la base está en cero". Ese silencio
        es lo que convirtió un renombrado de slug en "todas las versiones pendientes" y en un
        ``apply`` desde la 0001 sobre bases que ya tenían el esquema.

        Este informe rompe ese silencio comparando lo que el gateway ESPERA contra lo que hay
        en el motor. **No corrige nada**: mover un puntero es trabajo de ``stamp`` y borrar
        una tabla necesita acceso directo, porque la consola SQL bloquea por diseño cualquier
        sentencia que nombre ``_gw_v_*``.

        Un motor caído no rompe el informe entero: esa BD sale como ``unreachable`` y el
        resto se reporta igual. Fallar todo porque un servidor de doce está apagado haría
        inútil la pantalla justo cuando más se necesita.
        """
        from app.controllers.common import build_target, get_server_or_404
        from app.models.server import Server
        from app.services.db_admin.factory import get_adapter
        from app.services.db_admin.identifiers import GATEWAY_TABLE_PREFIXES
        from app.services.db_admin.migrations import MigrationRunner, version_table_name

        # El prefijo sale de la MISMA constante que usan el nombrado y la exclusión: si se
        # escribiera a mano acá, este informe podría dejar de ver justo la tabla que busca.
        prefijo_version = GATEWAY_TABLE_PREFIXES[0]

        session = self._session()
        try:
            model = self._get_or_404(session, model_id)
            slug = model.slug
            filas = [
                (md.id, md.name, md.server_id, md.model_version, srv.name)
                for md, srv in session.query(ManagedDatabase, Server)
                .join(Server, ManagedDatabase.server_id == Server.id)
                .filter(ManagedDatabase.model_id == model_id)
                .order_by(ManagedDatabase.id)
                .all()
            ]
            targets = {}
            for _id, _name, server_id, _mv, _sn in filas:
                if server_id not in targets:
                    targets[server_id] = build_target(get_server_or_404(session, server_id))
        finally:
            session.close()

        esperada = version_table_name(slug)
        runner = MigrationRunner()
        items: list[dict] = []
        for db_id, db_name, server_id, cached, server_name in filas:
            item = {
                "managed_database_id": db_id,
                "database_name": db_name,
                "server_id": server_id,
                "server_name": server_name,
                "expected_table": esperada,
                "present_tables": [],
                "orphan_tables": [],
                "current_version": None,
                "cached_version": cached,
                "status": self._VT_UNREACHABLE,
                "detail": None,
            }
            try:
                presentes = get_adapter(targets[server_id]).list_internal_tables(db_name)
            except AppHttpException as exc:
                item["detail"] = getattr(exc, "message", "No se pudo leer la base.")
                items.append(item)
                continue

            versiones = [n for n in presentes if n.startswith(prefijo_version)]
            huerfanas = [n for n in versiones if n != esperada]
            item["present_tables"] = presentes
            item["orphan_tables"] = huerfanas

            if esperada in versiones:
                item["status"] = self._VT_MIXED if huerfanas else self._VT_OK
                try:
                    item["current_version"] = runner.get_current_version(
                        targets[server_id], db_name, slug
                    )
                except AppHttpException:
                    # La tabla está pero no se pudo leer. No se degrada a "ok": el veredicto
                    # de este informe es la versión, no la existencia del archivo.
                    item["status"] = self._VT_UNREACHABLE
                    item["detail"] = "La tabla existe pero no se pudo leer su versión."
            elif huerfanas:
                item["status"] = self._VT_ORPHANED
                item["detail"] = (
                    "La versión real vive en una tabla que el gateway ya no lee. "
                    "Todas las versiones figuran pendientes aunque no lo estén."
                )
            else:
                item["status"] = self._VT_NONE
            items.append(item)

        resumen = {estado: 0 for estado in (
            self._VT_OK, self._VT_ORPHANED, self._VT_MIXED,
            self._VT_NONE, self._VT_UNREACHABLE,
        )}
        for it in items:
            resumen[it["status"]] += 1

        return {
            "model_id": model_id,
            "slug": slug,
            "expected_table": esperada,
            "databases": items,
            "summary": resumen,
            "needs_attention": resumen[self._VT_ORPHANED] + resumen[self._VT_MIXED] > 0,
        }

    # ------------------------------------------------------------------ #
    # Renombrado del slug, propagado a los motores                        #
    # ------------------------------------------------------------------ #
    #: Qué hay que hacer con cada BD del blueprint.
    _RN_RENAME = "rename"          # tiene la tabla vieja y el nombre nuevo está libre
    _RN_SKIP = "skip"              # nunca fue posicionada: no hay tabla que renombrar
    _RN_CONFLICT = "conflict"      # el nombre DESTINO ya existe ahí
    _RN_UNREACHABLE = "unreachable"  # no se pudo leer (fail-closed)

    _RN_BLOCKING = frozenset({_RN_CONFLICT, _RN_UNREACHABLE})

    @staticmethod
    def _rename_fingerprint(
        model_id: int, old_slug: str, new_slug: str, items: list[dict]
    ) -> str:
        """Huella del parque que congeló el preview; viaja como ``subject`` del token.

        Mismo criterio que ``_plan_fingerprint`` del borrado con renumerado: si entre el
        preview y la ejecución alguna BD cambió de estado —apareció la tabla destino, se
        cayó un servidor—, el token deja de verificar y no se ejecuta un plan que ya no
        describe la realidad.
        """
        parts = [str(model_id), old_slug, new_slug]
        for it in sorted(items, key=lambda i: i["managed_database_id"]):
            parts.append(f"{it['managed_database_id']}:{it['action']}")
        return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()

    def rename_slug_plan(self, model_id: int, new_slug: str) -> dict:
        """Preflight del renombrado. **No escribe nada**, ni en el gateway ni en un motor. 🔌

        Devuelve el plan SIEMPRE: los bloqueos viajan en ``blockers`` en vez de lanzarse,
        porque este mismo cálculo alimenta el preview, que tiene que poder explicar por qué
        no se puede además de negarse.
        """
        from app.controllers.common import build_target, get_server_or_404
        from app.services.db_admin.factory import get_adapter
        from app.services.db_admin.migrations import version_table_name

        session = self._session()
        try:
            model = self._get_or_404(session, model_id)
            old_slug = model.slug
            if new_slug == old_slug:
                raise AppHttpException(
                    message="El slug nuevo es igual al actual.",
                    status_code=422,
                    context={"model_id": model_id},
                )
            ocupado = (
                session.query(DatabaseModel.id)
                .filter(DatabaseModel.slug == new_slug, DatabaseModel.id != model_id)
                .first()
            )
            if ocupado:
                raise AppHttpException(
                    message="Ya existe otro blueprint con ese slug.",
                    status_code=409,
                    public_context={"code": dm_codes.CODE_NAME_OR_SLUG_TAKEN},
                    context={"model_id": model_id},
                )
            filas = [
                (md.id, md.name, md.server_id)
                for md in session.query(ManagedDatabase)
                .filter(ManagedDatabase.model_id == model_id)
                .order_by(ManagedDatabase.id)
                .all()
            ]
            nombres_servidor = {}
            targets = {}
            for _id, _name, server_id in filas:
                if server_id not in targets:
                    server = get_server_or_404(session, server_id)
                    targets[server_id] = build_target(server)
                    nombres_servidor[server_id] = server.name
        finally:
            session.close()

        tabla_vieja = version_table_name(old_slug)
        tabla_nueva = version_table_name(new_slug)
        # Dos slugs distintos pueden truncar al MISMO nombre (63 chars). Ahí no hay nada que
        # renombrar en ningún motor y el cambio es puramente local.
        no_op = tabla_vieja == tabla_nueva

        items: list[dict] = []
        for db_id, db_name, server_id in filas:
            item = {
                "managed_database_id": db_id,
                "database_name": db_name,
                "server_id": server_id,
                "server_name": nombres_servidor.get(server_id),
                "action": self._RN_SKIP,
                "detail": None,
            }
            if no_op:
                items.append(item)
                continue
            try:
                adapter = get_adapter(targets[server_id])
                tiene_vieja = adapter.internal_table_exists(db_name, tabla_vieja)
                tiene_nueva = adapter.internal_table_exists(db_name, tabla_nueva)
            except AppHttpException as exc:
                item["action"] = self._RN_UNREACHABLE
                item["detail"] = getattr(exc, "message", "No se pudo leer la base.")
                items.append(item)
                continue
            if tiene_nueva:
                item["action"] = self._RN_CONFLICT
                item["detail"] = f"Ya existe la tabla '{tabla_nueva}' en esta base."
            elif tiene_vieja:
                item["action"] = self._RN_RENAME
            items.append(item)

        bloqueantes = [i for i in items if i["action"] in self._RN_BLOCKING]
        a_renombrar = [i for i in items if i["action"] == self._RN_RENAME]
        huella = self._rename_fingerprint(model_id, old_slug, new_slug, items)

        plan = {
            "model_id": model_id,
            "current_slug": old_slug,
            "new_slug": new_slug,
            "current_table": tabla_vieja,
            "new_table": tabla_nueva,
            "no_op": no_op,
            "databases": items,
            "rename_count": len(a_renombrar),
            "blockers": bloqueantes,
            "requires_confirmation": bool(a_renombrar) and not bloqueantes,
            "confirm_token": None,
            "expires_at": None,
            "fingerprint": huella,
        }
        # El token se emite SOLO si hay motores que tocar y nada bloquea. Emitir uno que no
        # hace falta entrena al cliente a mandarlo siempre.
        if plan["requires_confirmation"]:
            token, expira = confirm_token_service.issue(
                dm_codes.RENAME_SLUG_OPERATION,
                model_id,
                f"{old_slug}:{new_slug}",
                subject=huella,
            )
            plan["confirm_token"] = token
            plan["expires_at"] = expira
        return plan

    def _enforce_rename_plan(self, plan: dict) -> None:
        """Convierte los bloqueos del plan en el 409 que corresponde. Sin efectos.

        Los dos bloqueos abortan el renombrado ENTERO y no solo la BD que los causa. Dejar
        la mitad del parque con un nombre y la otra mitad con otro es precisamente el estado
        del que cuesta salir: el gateway solo puede apuntar a UN nombre, así que la mitad que
        no coincida queda con su contabilidad huérfana y toda su cadena figurando pendiente.
        """
        conflictos = [i for i in plan["blockers"] if i["action"] == self._RN_CONFLICT]
        if conflictos:
            raise AppHttpException(
                message=(
                    f"{len(conflictos)} base(s) ya tienen una tabla '{plan['new_table']}'. "
                    "Renombrar encima pisaría un puntero de versión ajeno. Resolvé esas "
                    "bases antes de renombrar el slug."
                ),
                status_code=409,
                public_context={
                    "code": dm_codes.CODE_SLUG_RENAME_CONFLICT,
                    "new_table": plan["new_table"],
                    "conflicting_databases": conflictos,
                },
                context={"model_id": plan["model_id"]},
            )
        ilegibles = [i for i in plan["blockers"] if i["action"] == self._RN_UNREACHABLE]
        if ilegibles:
            raise AppHttpException(
                message=(
                    f"No se pudo leer {len(ilegibles)} base(s) del blueprint. Se aborta: "
                    "renombrar el resto dejaría la contabilidad de esas huérfana sin que "
                    "nada falle."
                ),
                status_code=409,
                public_context={
                    "code": dm_codes.CODE_SLUG_RENAME_UNREACHABLE,
                    "unreachable_databases": ilegibles,
                },
                context={"model_id": plan["model_id"]},
            )

    @staticmethod
    def _compensate_renames(hechas: list[dict], targets: dict, desde: str, hacia: str) -> list[dict]:
        """Devuelve a su nombre original las tablas ya renombradas.

        Retorna las que NO se pudieron devolver (lista vacía ⇒ se compensó todo). Devolver la
        lista y no un booleano importa: es lo que el operador necesita para reparar a mano, y
        un ``False`` global reportaría como rotas también a las bases que sí volvieron.

        Se recorre en orden INVERSO por simetría con el resto del repo, aunque acá cada base
        es independiente de las demás.
        """
        from app.services.db_admin.factory import get_adapter

        quedaron: list[dict] = []
        for it in reversed(hechas):
            try:
                get_adapter(targets[it["server_id"]]).rename_internal_table(
                    it["database_name"], desde, hacia
                )
            except Exception:
                logger.exception(
                    "falló la compensación del rename en la BD %s (%s: %s -> %s)",
                    it["managed_database_id"], it["database_name"], desde, hacia,
                )
                quedaron.append(it)
        return quedaron

    def rename_slug(
        self,
        model_id: int,
        new_slug: str,
        *,
        confirm_token: str | None = None,
        admin: "dict | Actor | None" = None,
    ) -> dict:
        """Cambia el ``slug`` del blueprint y propaga el rename de su tabla de versión. 🔌

        **El orden no es negociable: primero los N renames remotos, y el ``slug`` del gateway
        se actualiza ÚLTIMO.** Al revés, un fallo remoto dejaría al gateway apuntando a un
        nombre que no existe en ningún motor — que es exactamente el incidente que originó
        este endpoint: la cadena entera pasa a figurar pendiente y un ``apply`` la reaplica
        desde la primera versión sobre bases que ya tienen el esquema.

        El plan se recalcula DESDE CERO acá: el token no transporta el plan, solo prueba que
        el estado del parque no cambió desde el preview.
        """
        from app.controllers.common import build_target, engine_value, get_server_or_404
        from app.models.enums import EngineType
        from app.services.db_admin.factory import get_adapter
        from app.services.db_admin.migrations import MigrationRunner

        plan = self.rename_slug_plan(model_id, new_slug)
        self._enforce_rename_plan(plan)
        old_slug = plan["current_slug"]
        tabla_vieja, tabla_nueva = plan["current_table"], plan["new_table"]
        a_renombrar = [i for i in plan["databases"] if i["action"] == self._RN_RENAME]

        if a_renombrar:
            if not confirm_token:
                raise AppHttpException(
                    message=(
                        f"Renombrar el slug implica renombrar '{tabla_vieja}' en "
                        f"{len(a_renombrar)} base(s) de sus motores. Pedí el plan en "
                        f"POST /database-models/{model_id}/rename-slug/plan y reenviá su "
                        "confirm_token."
                    ),
                    status_code=409,
                    public_context={
                        "code": dm_codes.CODE_SLUG_RENAME_CONFIRMATION_REQUIRED,
                        "rename_plan": a_renombrar,
                    },
                    context={"model_id": model_id},
                )
            try:
                confirm_token_service.verify(
                    confirm_token,
                    dm_codes.RENAME_SLUG_OPERATION,
                    model_id,
                    f"{old_slug}:{new_slug}",
                    subject=plan["fingerprint"],
                )
            except AppHttpException as exc:
                # Se re-etiqueta con código propio: el 422 genérico del servicio dice "token
                # inválido", que manda a revisar el token cuando lo que pasó es que el parque
                # se movió y hay que volver a mirar el plan.
                raise AppHttpException(
                    message=(
                        "El plan de renombrado quedó viejo: alguna base cambió de estado "
                        "desde el preview. Volvé a pedirlo."
                    ),
                    status_code=getattr(exc, "status_code", 422),
                    public_context={"code": dm_codes.CODE_SLUG_RENAME_PLAN_STALE},
                    context={"model_id": model_id},
                ) from exc

            # Fail-closed ANTES del primer motor: un renombrado que muere a mitad tiene que
            # dejar rastro de lo que intentó, que es justo cuando alguien pregunta qué pasó.
            audit.record_intent(
                dm_codes.RENAME_SLUG_OPERATION,
                admin=admin,
                target_type="database_model",
                target_id=model_id,
                touched_engine=True,
                detail=(
                    f"blueprint {model_id}: '{old_slug}' -> '{new_slug}'; renombra "
                    f"{tabla_vieja} -> {tabla_nueva} en {len(a_renombrar)} BD(s)"
                ),
            )

        session = self._session()
        try:
            targets = {}
            engines = {}
            for it in a_renombrar:
                sid = it["server_id"]
                if sid not in targets:
                    server = get_server_or_404(session, sid)
                    targets[sid] = build_target(server)
                    engines[sid] = EngineType(engine_value(server))
        finally:
            session.close()

        runner = MigrationRunner()
        hechas: list[dict] = []
        for it in a_renombrar:
            sid = it["server_id"]
            try:
                # El lock es el MISMO que toman apply/rollback/stamp (clave =
                # managed_database_id), así que un renombrado no puede cruzarse con una
                # migración en curso sobre esa base.
                with runner.advisory_lock(targets[sid], engine=engines[sid], lock_key=it["managed_database_id"]):
                    get_adapter(targets[sid]).rename_internal_table(
                        it["database_name"], tabla_vieja, tabla_nueva
                    )
                hechas.append(it)
            except Exception as exc:  # noqa: BLE001 — se compensa y se reporta, no se traga
                logger.exception(
                    "rename de slug: falló en la BD %s (%s)",
                    it["managed_database_id"], it["database_name"],
                )
                no_compensadas = self._compensate_renames(
                    hechas, targets, tabla_nueva, tabla_vieja
                )
                audit.record(
                    dm_codes.RENAME_SLUG_OPERATION,
                    status="error",
                    admin=admin,
                    target_type="database_model",
                    target_id=model_id,
                    touched_engine=True,
                    detail=(
                        f"falló en la BD {it['managed_database_id']}; "
                        f"{len(hechas)} renombrada(s), "
                        f"{len(no_compensadas)} sin compensar. Slug NO modificado."
                    ),
                )
                raise AppHttpException(
                    message=(
                        f"Falló el renombrado en la base {it['database_name']}. "
                        + (
                            f"{len(no_compensadas)} base(s) quedaron con el nombre nuevo y "
                            "no se pudieron devolver: hay que repararlas a mano."
                            if no_compensadas
                            else "Las bases ya renombradas volvieron a su nombre original."
                        )
                        + " El slug del blueprint NO se modificó."
                    ),
                    status_code=409,
                    public_context={
                        "code": dm_codes.CODE_SLUG_RENAME_FAILED,
                        "failed": it,
                        "renamed": hechas,
                        "not_compensated": no_compensadas,
                        "old_table": tabla_vieja,
                        "new_table": tabla_nueva,
                    },
                    context={"model_id": model_id},
                ) from exc

        # ÚLTIMO: recién ahora el gateway cambia de nombre. Una transacción local.
        session = self._session()
        try:
            model = self._get_or_404(session, model_id)
            model.slug = new_slug
            session.commit()
            session.refresh(model)
            resultado = self._serialize(model)
        finally:
            session.close()

        audit.record(
            dm_codes.RENAME_SLUG_OPERATION,
            status="success",
            admin=admin,
            target_type="database_model",
            target_id=model_id,
            touched_engine=bool(a_renombrar),
            detail=(
                f"'{old_slug}' -> '{new_slug}'; {len(hechas)} BD(s) renombradas "
                f"({tabla_vieja} -> {tabla_nueva})"
            ),
        )
        return {"model": resultado, "renamed_databases": hechas, "no_op": plan["no_op"]}

    # ------------------------------------------------------------------ #
    # Deriva de charset/collation contra la declaración del blueprint     #
    # ------------------------------------------------------------------ #
    _DRIFT_OK = "ok"
    _DRIFT_DRIFTED = "drifted"
    _DRIFT_UNKNOWN = "unknown"
    _DRIFT_UNDECLARED = "undeclared"
    _DRIFT_NOT_APPLICABLE = "not_applicable"

    def collation_drift(self, model_id: int) -> dict:
        """
        Qué BDs del blueprint se desviaron del charset/collation declarado.

        ``DatabaseModel.charset``/``.collation`` existen desde hace tiempo con un comentario
        que dice que sirven "para detectar BDs que se han desviado" — y hasta acá **nadie los
        leía**. Esto los convierte en referencia usable.

        **CERO conexiones al motor.** Se compara contra ``ManagedDatabase.charset``/
        ``.collation``, que son la copia que el gateway ya mantiene (y que la conversión
        sincroniza al terminar). Por eso la respuesta declara ``source: "cached"``: presentar
        una caché como verdad del motor sería mentir en una pantalla que se va a usar para
        decidir conversiones.

        Cinco estados, y ``unknown`` **no** es ``ok``:

        - ``undeclared``: el blueprint no declaró objetivo. No se inventa uno.
        - ``not_applicable``: la BD es PostgreSQL. Allá el concepto es ``encoding`` +
          ``lc_collate``, que no son equivalentes — el propio modelo lo declara.
        - ``unknown``: la fila no tiene el dato. No se sabe, que es distinto de estar al día.
        - ``drifted`` / ``ok``: hay dato y difiere, o coincide.

        ``source_of_truth`` por fila dice de dónde sale ese dato, y no es adorno:
        ``charset``/``collation`` son **escribibles a mano** por ``PATCH
        /managed-databases/{id}``, así que una fila puede decir ``ok`` porque alguien lo tipeó.
        Es el mismo defecto que el repo ya corrigió para ``model_version`` (ver
        ``T-260824-lz-charset-managed-patch`` en ``TODO.md``); mientras siga abierto, la UI
        necesita poder distinguir un dato leído del motor de una afirmación.
        """
        from app.controllers.common import engine_value
        from app.models.server import Server

        session = self._session()
        try:
            model = self._get_or_404(session, model_id)
            declared_cs, declared_co = model.charset, model.collation
            slug = model.slug
            rows = (
                session.query(ManagedDatabase, Server)
                .join(Server, ManagedDatabase.server_id == Server.id)
                .filter(ManagedDatabase.model_id == model_id)
                .order_by(ManagedDatabase.id.asc())
                .all()
            )
            env_names: dict[int, str] = {}
            env_ids = {md.environment_id for md, _s in rows if md.environment_id}
            if env_ids:
                from app.models.environment import Environment

                env_names = {
                    e.id: e.slug
                    for e in session.query(Environment)
                    .filter(Environment.id.in_(env_ids))
                    .all()
                }
            items = []
            for md, server in rows:
                engine = engine_value(server)
                if declared_cs is None and declared_co is None:
                    status = self._DRIFT_UNDECLARED
                elif engine == "postgresql":
                    status = self._DRIFT_NOT_APPLICABLE
                elif md.charset is None and md.collation is None:
                    status = self._DRIFT_UNKNOWN
                elif (declared_cs and md.charset != declared_cs) or (
                    declared_co and md.collation != declared_co
                ):
                    status = self._DRIFT_DRIFTED
                else:
                    status = self._DRIFT_OK
                items.append(
                    {
                        "managed_database_id": md.id,
                        "database_name": md.name,
                        "server_id": md.server_id,
                        "server_name": server.name,
                        "engine": engine,
                        "environment_slug": env_names.get(md.environment_id),
                        "charset": md.charset,
                        "collation": md.collation,
                        "status": status,
                        "source_of_truth": self._source_of_truth(md),
                    }
                )
        finally:
            session.close()

        return {
            "model_id": model_id,
            "model_slug": slug,
            "declared": (
                {"charset": declared_cs, "collation": declared_co}
                if (declared_cs or declared_co)
                else None
            ),
            "source": "cached",
            "source_note": (
                "Lectura del inventario del gateway, no del motor. Puede estar desactualizada."
            ),
            "databases": items,
        }

    @staticmethod
    def _source_of_truth(md) -> str:
        """
        De dónde sale el charset/collation de esta fila, con vocabulario cerrado.

        Es una aproximación honesta, no una certeza: el gateway no registra por columna quién
        la escribió. ``adopted`` sale de ``origin``; sin dato es ``unknown``; con dato y
        aprovisionada por el gateway, ``provisioned``. Lo que NO se puede distinguir hoy es un
        valor escrito por una conversión de uno tipeado a mano por ``PATCH`` — de ahí el ítem
        de deuda: mientras esas columnas sean escribibles a ciegas, ``ok`` puede ser una
        afirmación en vez de un hecho.
        """
        if md.charset is None and md.collation is None:
            return "unknown"
        if (md.origin or "") == "adopted":
            return "adopted"
        return "provisioned"
