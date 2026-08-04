"""
Controller de PerfilesdePermisos (perfiles de permisos).

CRUD sobre la BD de metadatos. Cada item del perfil se VALIDA contra el catálogo
cerrado de privilegios (`db_admin/privileges.py`) para el motor del perfil: si un
privilegio no es válido para ese motor/nivel, o es administrativo (DENY), se rechaza
con 422. Los tokens se normalizan a su forma canónica antes de persistir.

NO ejecuta GRANTs: solo define/gestiona la plantilla. La aplicación a un usuario la
hará el motor de permisos granular (Plan 07).
"""

from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.enums import EngineType
from app.models.permission_profile import PermissionProfile, PermissionProfileItem
from app.services.db_admin import privileges as priv_catalog
from app.services.db_admin.dtos import GrantLevel


class PermissionProfileController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Validación / serialización                                          #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _engine_value(engine) -> str:
        try:
            return EngineType(engine).value
        except ValueError as exc:
            raise AppHttpException(
                message="Motor inválido. Use: mysql, mariadb o postgresql.",
                status_code=422,
                context={"engine": str(engine)},
            ) from exc

    @staticmethod
    def _validate_items(engine: str, items: list[dict]) -> list[dict]:
        """Valida los items contra el catálogo del motor. Devuelve items canónicos."""
        if not items:
            raise AppHttpException(
                message="El perfil debe tener al menos un item de permisos.",
                status_code=422,
            )
        seen: set[str] = set()
        result: list[dict] = []
        for item in items:
            level = GrantLevel(item["level"])  # acepta member o value
            if level.value in seen:
                raise AppHttpException(
                    message="Hay niveles duplicados en el perfil.",
                    status_code=422,
                    context={"level": level.value},
                )
            seen.add(level.value)
            # Lanza 422 si algún privilegio es inválido/DENY para (motor, nivel).
            canonical, _ = priv_catalog.validate_privileges(
                item["privileges"], engine, level
            )
            result.append({"level": level.value, "privileges": canonical})
        return result

    @staticmethod
    def _serialize(profile: PermissionProfile, items: list[PermissionProfileItem]) -> dict:
        return {
            "id": profile.id,
            "name": profile.name,
            "engine": profile.engine,
            "description": profile.description,
            "is_active": profile.is_active,
            "items": [
                {
                    "level": it.level,
                    "privileges": it.privileges.split(","),
                    "requires_confirmation": any(
                        priv_catalog.token_is_sensitive(profile.engine, p)
                        for p in it.privileges.split(",")
                    ),
                }
                for it in items
            ],
            "created_at": profile.created_at,
            "updated_at": profile.updated_at,
        }

    def _items_of(self, session, profile_id: int) -> list[PermissionProfileItem]:
        return (
            session.query(PermissionProfileItem)
            .filter(PermissionProfileItem.profile_id == profile_id)
            .order_by(PermissionProfileItem.level)
            .all()
        )

    def _items_of_many(
        self, session, profile_ids: list[int]
    ) -> dict[int, list[PermissionProfileItem]]:
        """Items de varios perfiles en UNA query (evita el N+1 del listado)."""
        if not profile_ids:
            return {}
        rows = (
            session.query(PermissionProfileItem)
            .filter(PermissionProfileItem.profile_id.in_(profile_ids))
            .order_by(PermissionProfileItem.profile_id, PermissionProfileItem.level)
            .all()
        )
        grouped: dict[int, list[PermissionProfileItem]] = {pid: [] for pid in profile_ids}
        for row in rows:
            grouped[row.profile_id].append(row)
        return grouped

    @staticmethod
    def _applicable_to(
        profile: PermissionProfile, items: list[PermissionProfileItem], engine: str
    ) -> bool:
        """
        ¿Este perfil es aplicable sobre ``engine``?

        Mismo motor → sí. Motor distinto de la MISMA familia (MySQL↔MariaDB) → solo si
        TODOS sus privilegios siguen siendo otorgables en el motor destino. Un perfil sin
        items no es aplicable (fail-closed; no debería existir: `_validate_items` exige ≥1).
        """
        if profile.engine == engine:
            return True
        if not priv_catalog.same_family(profile.engine, engine):
            return False
        if not items:
            return False
        return all(
            priv_catalog.tokens_valid_for(
                engine, GrantLevel(it.level), [p for p in it.privileges.split(",") if p]
            )
            for it in items
        )

    def _get_or_404(self, session, profile_id: int) -> PermissionProfile:
        profile = session.get(PermissionProfile, profile_id)
        if not profile:
            raise AppHttpException(
                message="Perfil de permisos no encontrado.",
                status_code=404,
                context={"profile_id": profile_id},
            )
        return profile

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def list_profiles(
        self,
        engine: str | None = None,
        active: bool | None = None,
        exact_engine: bool = False,
    ) -> list[dict]:
        """
        Lista perfiles, opcionalmente filtrados por motor y estado.

        El filtro de motor es **compatible por familia**: pedir ``mysql`` también devuelve
        los perfiles ``mariadb`` cuyos privilegios son TODOS otorgables en MySQL, y
        viceversa (la única asimetría real es `DELETE HISTORY`, exclusivo de MariaDB).
        Motivo: un perfil sirve para el otro motor de la familia siempre que su contenido
        sea válido allí, y esconderlo dejaba el selector de «Aplicar perfil» vacío sin
        explicación. Con ``exact_engine=True`` se recupera la igualdad estricta.

        El motor de cada perfil viaja en la respuesta (`engine`), así que la UI puede
        distinguir los de la familia de los del motor exacto.
        """
        if engine is not None:
            engine = self._engine_value(engine)
        session = self._session()
        try:
            q = session.query(PermissionProfile)
            if engine is not None:
                if exact_engine:
                    q = q.filter(PermissionProfile.engine == engine)
                else:
                    # Pre-filtro amplio; la compatibilidad fina se decide por token abajo.
                    q = q.filter(
                        PermissionProfile.engine.in_(priv_catalog.family_members(engine))
                    )
            if active is not None:
                q = q.filter(PermissionProfile.is_active == active)
            profiles = q.order_by(PermissionProfile.engine, PermissionProfile.name).all()
            items_by_profile = self._items_of_many(session, [p.id for p in profiles])
            out: list[dict] = []
            for profile in profiles:
                items = items_by_profile.get(profile.id, [])
                if (
                    engine is not None
                    and not exact_engine
                    and not self._applicable_to(profile, items, engine)
                ):
                    continue
                out.append(self._serialize(profile, items))
            return out
        finally:
            session.close()

    def get_profile(self, profile_id: int) -> dict:
        session = self._session()
        try:
            profile = self._get_or_404(session, profile_id)
            return self._serialize(profile, self._items_of(session, profile_id))
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Escritura                                                          #
    # ------------------------------------------------------------------ #
    def create_profile(self, data: dict) -> dict:
        engine = self._engine_value(data["engine"])
        items = self._validate_items(engine, data["items"])
        session = self._session()
        try:
            profile = PermissionProfile(
                name=data["name"],
                engine=engine,
                description=data.get("description"),
                is_active=data.get("is_active", True),
            )
            session.add(profile)
            try:
                session.flush()  # obtener profile.id sin cerrar la transacción
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un perfil con ese nombre para ese motor.",
                    status_code=409,
                    context={"name": data.get("name"), "engine": engine},
                ) from exc
            for it in items:
                session.add(
                    PermissionProfileItem(
                        profile_id=profile.id,
                        level=it["level"],
                        privileges=",".join(it["privileges"]),
                    )
                )
            session.commit()
            session.refresh(profile)
            return self._serialize(profile, self._items_of(session, profile.id))
        finally:
            session.close()

    def update_profile(self, profile_id: int, data: dict) -> dict:
        session = self._session()
        try:
            profile = self._get_or_404(session, profile_id)
            if "name" in data and data["name"] is not None:
                profile.name = data["name"]
            if "description" in data:
                profile.description = data["description"]
            if data.get("is_active") is not None:
                profile.is_active = data["is_active"]
            # Reemplazo total de items (revalidados contra el motor del perfil).
            if data.get("items") is not None:
                validated = self._validate_items(profile.engine, data["items"])
                for old in self._items_of(session, profile_id):
                    session.delete(old)
                session.flush()
                for it in validated:
                    session.add(
                        PermissionProfileItem(
                            profile_id=profile_id,
                            level=it["level"],
                            privileges=",".join(it["privileges"]),
                        )
                    )
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                raise AppHttpException(
                    message="Ya existe un perfil con ese nombre para ese motor.",
                    status_code=409,
                    context={"profile_id": profile_id},
                ) from exc
            session.refresh(profile)
            return self._serialize(profile, self._items_of(session, profile_id))
        finally:
            session.close()

    def delete_profile(self, profile_id: int) -> None:
        session = self._session()
        try:
            profile = self._get_or_404(session, profile_id)
            # Borrar items explícitamente (el ON DELETE CASCADE no es fiable en SQLite
            # sin PRAGMA foreign_keys; en MySQL/PG la FK también lo respalda).
            for item in self._items_of(session, profile_id):
                session.delete(item)
            session.delete(profile)
            session.commit()
        finally:
            session.close()
