"""
Controller de DatabaseModel (blueprints/categorías).

CRUD puro sobre la BD de metadatos del gateway: NO toca ningún motor destino.
"""

from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.database_model import DatabaseModel
from app.models.managed_database import ManagedDatabase
from app.models.model_migration import ModelMigration
from app.models.project import ProjectDatabaseModel
from app.services import audit


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

    def create_model(self, data: dict, *, admin: dict | None = None) -> dict:
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

    def update_model(self, model_id: int, data: dict, *, admin: dict | None = None) -> dict:
        session = self._session()
        try:
            model = self._get_or_404(session, model_id)
            # `charset`/`collation` se pueden LIMPIAR (volver a "sin declarar"), así que no
            # pueden ir en el bucle de arriba, que ignora los `None` para distinguir "no
            # enviado" de "enviado vacío" en los campos obligatorios.
            for field in ("charset", "collation"):
                if field in data:
                    setattr(model, field, data[field])
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

    def delete_model(self, model_id: int, *, admin: dict | None = None) -> None:
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
                    row.model_version = current
                    out[row.id] = current
                except AppHttpException:
                    continue
            session.commit()
        finally:
            session.close()
        return out

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
