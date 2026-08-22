"""
Controller de ENTORNOS de despliegue.

CRUD puro sobre la BD de metadatos del gateway: NO toca ningún motor destino. Pero **no es un
CRUD cualquiera**: cada fila es política de seguridad, así que las operaciones que DEBILITAN
esa política tienen un gesto de confirmación y auditoría fail-closed, y el borrado no puede
desclasificar BDs por efecto colateral.

Tres reglas que este archivo tiene que preservar:

1. **A lo sumo un ``is_default``, y con bloqueo de filas.** Ver ``_claim_default``.
2. **Borrar exige cero BDs asignadas.** No hay ``force``. Ver ``delete_environment``.
3. **Debilitar la política exige ``confirm_slug`` y se audita con ``record_intent``.** Ver
   ``_weakenings``.
"""

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from app.core.database import Database
from app.core.environments import DB_HOST, DB_NAME, DB_PASS, DB_PORT, DB_USER
from app.exceptions import AppHttpException
from app.models.environment import Environment
from app.models.managed_database import ManagedDatabase
from app.services import audit
from app.services import environment_catalog as ecodes


class EnvironmentController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    def _session(self):
        return self.db.get_declarative_base_session()

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    @staticmethod
    def _serialize(e: Environment, *, database_count: int = 0) -> dict:
        return {
            "id": e.id,
            "name": e.name,
            "slug": e.slug,
            "rank": e.rank,
            "color": e.color,
            "is_default": e.is_default,
            "is_active": e.is_active,
            "blocks_destructive_migrations": e.blocks_destructive_migrations,
            "database_count": database_count,
            "created_at": e.created_at,
            "updated_at": e.updated_at,
        }

    def _get_or_404(self, session, environment_id: int) -> Environment:
        env = session.get(Environment, environment_id)
        if not env:
            raise AppHttpException(
                message="Entorno no encontrado.",
                status_code=404,
                public_context={"code": ecodes.CODE_NOT_FOUND},
                context={"environment_id": environment_id},
            )
        return env

    @staticmethod
    def _counts_for(session, environment_ids: list[int]) -> dict[int, int]:
        """
        BDs por entorno en UNA query para toda la página.

        Contar dentro del bucle sería una query por fila. Mismo criterio y mismo motivo que
        ``ProjectController._counts_for``.
        """
        if not environment_ids:
            return {}
        rows = (
            session.query(
                ManagedDatabase.environment_id, func.count(ManagedDatabase.id)
            )
            .filter(ManagedDatabase.environment_id.in_(environment_ids))
            .group_by(ManagedDatabase.environment_id)
            .all()
        )
        return {env_id: count for env_id, count in rows}

    @staticmethod
    def _claim_default(session, env: Environment) -> None:
        """
        Deja a ``env`` como el ÚNICO default, apagando el de los demás.

        El bloqueo de filas de la primera línea NO es decorativo: sin él hay una carrera real
        que deja DOS defaults **sin ningún error**. Con A encendiendo la fila 1 y B la fila 2,
        ambas partiendo de la fila 3 como default:

            1. A hace ``UPDATE ... SET is_default=0 WHERE is_default=1`` → toca la fila 3.
            2. B hace el mismo UPDATE. Bloquea en la fila 3; al desbloquear re-lee lo último
               commiteado, ve la 3 ya en 0, y NO ve la 1 en 1 porque A todavía no se encendió.
            3. A se enciende. B se enciende. Dos defaults, sin violación de nada.

        Pasa igual en MySQL/MariaDB (REPEATABLE READ) y en PostgreSQL (READ COMMITTED): el
        UPDATE no toma predicate locks sobre filas que no matchean. ``SERIALIZABLE`` lo
        convertiría en un 40001 que nadie reintenta, y MariaDB no lo detecta.

        Un índice único parcial (``WHERE is_default``) resolvería esto en el esquema, pero no
        es portable: MySQL 8 no tiene índices parciales y el truco funcional
        ``UNIQUE ((CASE WHEN is_default THEN 1 END))`` existe en MySQL 8.0.13+ pero NO en
        MariaDB 11.

        Serializar el catálogo entero es gratis porque son 3-10 filas. SQLite ignora
        ``FOR UPDATE`` en silencio (es single-writer y los tests son single-thread), así que
        este invariante NO está verificado contra concurrencia real en la suite.
        """
        session.query(Environment).order_by(Environment.id).with_for_update().all()
        for other in session.query(Environment).filter(Environment.id != env.id).all():
            if other.is_default:
                other.is_default = False
        env.is_default = True

    @staticmethod
    def _weakenings(env: Environment, data: dict) -> list[str]:
        """
        Qué cambios de este PATCH DEBILITAN la política del entorno.

        Cada uno de estos exige ``confirm_slug`` y se audita con ``record_intent`` en vez de
        ``record``. El criterio: un aflojamiento de política es exactamente el tipo de evento
        para el que existe la auditoría fail-closed — si no se puede registrar, no se hace.

        ``is_active → False`` cuenta como debilitamiento aunque el toggle se llame "activo":
        desactivar un entorno lo saca del cálculo de promoción de los que vengan después, y es
        un cambio de política ejecutado por algo que no lo parece.
        """
        out = []
        if data.get("blocks_destructive_migrations") is False and env.blocks_destructive_migrations:
            out.append("blocks_destructive_migrations")
        if data.get("is_active") is False and env.is_active:
            out.append("is_active")
        if data.get("is_default") is False and env.is_default:
            out.append("is_default")
        return out

    @staticmethod
    def _require_confirmation(env: Environment, weakened: list[str], confirm_slug: str | None) -> None:
        if not weakened:
            return
        if (confirm_slug or "").strip().lower() != env.slug:
            raise AppHttpException(
                message=(
                    f"Este cambio debilita la política del entorno ({', '.join(weakened)}). "
                    f"Repetí el slug '{env.slug}' en 'confirm_slug' para confirmarlo."
                ),
                status_code=422,
                public_context={
                    "code": ecodes.CODE_CONFIRMATION_REQUIRED,
                    "expected_slug": env.slug,
                    "weakened": weakened,
                },
                context={"environment_id": env.id},
            )

    def _guard_resulting_state(self, session, env: Environment) -> None:
        """
        Valida el estado RESULTANTE, no el enviado.

        Criterio copiado de ``charset_catalog.update_option``: mirar solo el payload deja pasar
        las dos mitades del problema por separado (encender ``is_default`` en una fila ya
        inactiva, y desactivar la fila que YA es default). Se evalúa después de fusionar.
        """
        if env.is_default and not env.is_active:
            raise AppHttpException(
                message="Un entorno inactivo no puede ser el entorno por defecto.",
                status_code=422,
                public_context={"code": ecodes.CODE_DEFAULT_MUST_BE_ACTIVE},
                context={"environment_id": env.id},
            )
        # Quedarse sin default no es inocuo: las BDs nuevas nacerían con environment_id NULL,
        # que es permisivo en el guard. Se exige designar otro primero.
        remaining = (
            session.query(Environment.id)
            .filter(Environment.is_default.is_(True), Environment.id != env.id)
            .first()
        )
        if not env.is_default and remaining is None:
            raise AppHttpException(
                message=(
                    "No puede quedar ningún entorno por defecto: las BDs nuevas quedarían sin "
                    "clasificar. Designá otro entorno como default primero."
                ),
                status_code=409,
                public_context={"code": ecodes.CODE_DEFAULT_REQUIRED},
                context={"environment_id": env.id},
            )

    @staticmethod
    def _raise_duplicate(session, *, name: str | None, slug: str | None, exclude_id: int | None = None) -> None:
        """
        409 con un código POR COLUMNA.

        Un solo código para "algo está duplicado" obliga a la SPA a adivinar cuál de los dos
        inputs marcar. Se pre-chequea acá y además se captura el ``IntegrityError`` en el
        llamador como red de la carrera entre el SELECT y el INSERT.
        """
        q = session.query(Environment.id, Environment.name, Environment.slug)
        if exclude_id is not None:
            q = q.filter(Environment.id != exclude_id)
        for _id, existing_name, existing_slug in q.all():
            if name is not None and existing_name == name:
                raise AppHttpException(
                    message=f"Ya existe un entorno con el nombre '{name}'.",
                    status_code=409,
                    public_context={"code": ecodes.CODE_NAME_TAKEN},
                    context={"name": name},
                )
            if slug is not None and existing_slug == slug:
                raise AppHttpException(
                    message=f"Ya existe un entorno con el slug '{slug}'.",
                    status_code=409,
                    public_context={"code": ecodes.CODE_SLUG_TAKEN},
                    context={"slug": slug},
                )

    # ------------------------------------------------------------------ #
    # Lectura                                                            #
    # ------------------------------------------------------------------ #
    def list_environments(
        self, *, only_active: bool = False, limit: int, offset: int
    ) -> tuple[list[dict], int]:
        session = self._session()
        try:
            q = session.query(Environment)
            if only_active:
                q = q.filter(Environment.is_active.is_(True))
            total = q.count()
            # (rank, id): el orden total de promoción. `rank` no es único, así que sin el
            # desempate por id dos entornos empatados saldrían en orden arbitrario y el
            # listado dejaría de ser estable entre requests.
            rows = (
                q.order_by(Environment.rank.asc(), Environment.id.asc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            counts = self._counts_for(session, [r.id for r in rows])
            return [
                self._serialize(r, database_count=counts.get(r.id, 0)) for r in rows
            ], total
        finally:
            session.close()

    def get_environment(self, environment_id: int) -> dict:
        session = self._session()
        try:
            env = self._get_or_404(session, environment_id)
            counts = self._counts_for(session, [env.id])
            return self._serialize(env, database_count=counts.get(env.id, 0))
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Escritura                                                          #
    # ------------------------------------------------------------------ #
    def create_environment(self, data: dict, *, admin: dict | None = None) -> dict:
        session = self._session()
        try:
            self._raise_duplicate(session, name=data["name"], slug=data["slug"])
            wants_default = bool(data.pop("is_default", False))
            env = Environment(**data)
            session.add(env)
            session.flush()
            if wants_default:
                self._claim_default(session, env)
            self._guard_resulting_state(session, env)
            session.commit()
            session.refresh(env)
            audit.record(
                "environment.create",
                admin=admin,
                target_type="environment",
                target_id=env.id,
                touched_engine=False,
                detail=(
                    f"slug={env.slug} rank={env.rank} default={env.is_default} "
                    f"active={env.is_active} blocks_destructive={env.blocks_destructive_migrations}"
                ),
            )
            return self._serialize(env, database_count=0)
        except IntegrityError:
            # Red de la carrera entre el pre-chequeo y el INSERT: sin esto sale un 500.
            session.rollback()
            raise AppHttpException(
                message="Ya existe un entorno con ese nombre o slug.",
                status_code=409,
                public_context={"code": ecodes.CODE_SLUG_TAKEN},
                context={"name": data.get("name"), "slug": data.get("slug")},
            ) from None
        finally:
            session.close()

    def update_environment(
        self, environment_id: int, data: dict, *, confirm_slug: str | None = None,
        admin: dict | None = None,
    ) -> dict:
        session = self._session()
        try:
            env = self._get_or_404(session, environment_id)
            weakened = self._weakenings(env, data)
            self._require_confirmation(env, weakened, confirm_slug)
            if weakened:
                # ANTES de tocar nada y FAIL-CLOSED: si el rastro no se puede persistir, el
                # aflojamiento de política no se ejecuta. Es el contrato de ``record_intent``
                # (levanta 500 y la operación no debe continuar), y el motivo por el que va
                # acá y no después del commit. ``touched_engine=False``: esto no ejecuta DDL,
                # pero exige rastro garantizado igual — mismo caso que revelar una contraseña.
                audit.record_intent(
                    "environment.weaken",
                    admin=admin,
                    target_type="environment",
                    target_id=env.id,
                    touched_engine=False,
                    detail=f"INTENT debilitar {','.join(weakened)} en slug={env.slug}",
                )
            if "name" in data:
                self._raise_duplicate(
                    session, name=data["name"], slug=None, exclude_id=env.id
                )

            before = {
                "rank": env.rank,
                "is_default": env.is_default,
                "is_active": env.is_active,
                "blocks_destructive_migrations": env.blocks_destructive_migrations,
            }
            wants_default = data.pop("is_default", None)
            for field in ("name", "rank", "color", "is_active", "blocks_destructive_migrations"):
                if field in data:
                    setattr(env, field, data[field])
            if wants_default is True:
                self._claim_default(session, env)
            elif wants_default is False:
                env.is_default = False
            self._guard_resulting_state(session, env)
            session.commit()
            session.refresh(env)

            after = {
                "rank": env.rank,
                "is_default": env.is_default,
                "is_active": env.is_active,
                "blocks_destructive_migrations": env.blocks_destructive_migrations,
            }
            changed = {k: (before[k], after[k]) for k in before if before[k] != after[k]}
            detail = f"slug={env.slug} " + " ".join(
                f"{k}:{old}->{new}" for k, (old, new) in changed.items()
            )
            audit.record(
                "environment.update",
                admin=admin,
                target_type="environment",
                target_id=env.id,
                touched_engine=False,
                detail=(
                    (detail or f"slug={env.slug} sin cambios efectivos")
                    + (f" weakened={','.join(weakened)}" if weakened else "")
                ),
            )
            counts = self._counts_for(session, [env.id])
            return self._serialize(env, database_count=counts.get(env.id, 0))
        except IntegrityError:
            session.rollback()
            raise AppHttpException(
                message="Ya existe un entorno con ese nombre.",
                status_code=409,
                public_context={"code": ecodes.CODE_NAME_TAKEN},
                context={"name": data.get("name")},
            ) from None
        finally:
            session.close()

    def delete_environment(
        self, environment_id: int, *, confirm_slug: str | None = None,
        admin: dict | None = None,
    ) -> None:
        """
        Borra un entorno. **Exige que no tenga ninguna BD asignada.**

        Deliberadamente NO hay un ``?force=true`` que desclasifique en masa. Tres razones:

        1. Ningún ``DELETE`` del repo tiene ``force``; los destructivos exigen re-tipear el
           identificador (``confirm_name``, ``confirm_username``). Un flag en una URL termina
           en un script.
        2. Desclasificar N BDs de producción de un plumazo es "irreversible sobre N filas a la
           vez", que es justo el criterio por el que esos otros endpoints piden re-tipeo.
        3. La FK es ``ON DELETE RESTRICT``, así que el motor tampoco lo permitiría: un ``force``
           tendría que desvincular primero, y ahí el borrado deja de ser una operación de
           organización y pasa a ser un cambio de política encubierto.

        La vía de retiro de un entorno que todavía tiene BDs es ``is_active=false``.

        El conteo se hace acá y no se delega a la FK porque en los tests SQLite no aplica
        claves foráneas (el esquema se crea con ``create_all``, sin ``PRAGMA foreign_keys=ON``),
        así que confiar solo en el motor dejaría el caso sin cubrir donde más se ejercita.
        """
        session = self._session()
        try:
            env = self._get_or_404(session, environment_id)
            count = (
                session.query(func.count(ManagedDatabase.id))
                .filter(ManagedDatabase.environment_id == env.id)
                .scalar()
                or 0
            )
            if count:
                raise AppHttpException(
                    message=(
                        f"El entorno '{env.slug}' tiene {count} base(s) de datos asignada(s). "
                        "Reasignalas antes de borrarlo, o desactivalo con is_active=false."
                    ),
                    status_code=409,
                    public_context={
                        "code": ecodes.CODE_HAS_DATABASES,
                        "database_count": count,
                    },
                    context={"environment_id": env.id},
                )
            # Borrar el default deja al sistema sin default: las BDs nuevas nacerían sin
            # clasificar. Mismo criterio que el PATCH.
            if env.is_default:
                self._require_confirmation(env, ["is_default"], confirm_slug)
                other = (
                    session.query(Environment.id)
                    .filter(Environment.id != env.id, Environment.is_active.is_(True))
                    .first()
                )
                if other is None:
                    raise AppHttpException(
                        message=(
                            "Es el único entorno por defecto y no hay otro activo para "
                            "reemplazarlo. Creá o activá otro y designalo default primero."
                        ),
                        status_code=409,
                        public_context={"code": ecodes.CODE_DEFAULT_REQUIRED},
                        context={"environment_id": env.id},
                    )
            slug, was_default = env.slug, env.is_default
            session.delete(env)
            session.commit()
            audit.record(
                "environment.delete",
                admin=admin,
                target_type="environment",
                target_id=environment_id,
                touched_engine=False,
                detail=f"slug={slug} was_default={was_default}",
            )
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # Consumido por otros controllers                                     #
    # ------------------------------------------------------------------ #
    @staticmethod
    def resolve_for_assignment(session, environment_id: int | None) -> int | None:
        """
        Valida el entorno que se va a asignar a una BD, o resuelve el default.

        Se recibe la ``session`` en vez de abrir una propia porque los llamadores
        (``ManagedDatabaseController.create_database`` / ``adopt_database``) validan dentro de
        su propia transacción, igual que ya hacen con la validación owner↔server.

        ``environment_id=None`` NO es un error: cae en el entorno marcado ``is_default``, y si
        no hay ninguno queda en ``None`` (sin clasificar). OJO con lo que eso significa: el
        default sembrado es ``development``, el entorno MÁS PERMISIVO, así que una BD nueva
        "nace clasificada" pero no "nace protegida". La red de seguridad para encontrar lo que
        quedó mal clasificado es el filtro ``only_unassigned`` del listado.
        """
        if environment_id is None:
            row = (
                session.query(Environment.id)
                .filter(Environment.is_default.is_(True), Environment.is_active.is_(True))
                .first()
            )
            return row[0] if row else None
        env = session.get(Environment, environment_id)
        if env is None:
            raise AppHttpException(
                message="El entorno indicado no existe.",
                status_code=404,
                public_context={"code": ecodes.CODE_NOT_FOUND},
                context={"environment_id": environment_id},
            )
        if not env.is_active:
            raise AppHttpException(
                message=f"El entorno '{env.slug}' está inactivo y no se puede asignar.",
                status_code=422,
                public_context={"code": ecodes.CODE_INACTIVE},
                context={"environment_id": environment_id},
            )
        return env.id
