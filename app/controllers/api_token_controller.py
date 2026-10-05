"""
Controller de los tokens de agente.

EL SECRETO SE MUESTRA UNA VEZ Y NO SE GUARDA
--------------------------------------------
Lo que persiste es su HMAC. Si alguien lo pierde, **se emite otro** — no hay recuperación, y eso
es lo correcto: un sistema que pueda mostrarte de nuevo un secreto es un sistema que lo tiene.

UN TOKEN POR MÁQUINA O REPO
---------------------------
La revocación granular es el objetivo, no la comodidad. Un token compartido entre seis máquinas es
un token que **nadie revoca**, porque romperlo rompe a los seis — y entonces el control existe
en el papel y no en la práctica. Por eso ``name`` es obligatorio y describe el destino.
"""

from datetime import UTC, datetime, timedelta

from sqlalchemy.exc import IntegrityError

from app.core.environments import MCP_DATA_TOKEN_MAX_TTL_DAYS, MCP_TOKEN_MAX_TTL_DAYS
from app.core.mcp_auth import mint, token_hmac
from app.exceptions import AppHttpException
from app.models.api_token import ApiToken
from app.models.project import Project
from app.services import audit
from app.services import project_catalog as project_codes
from app.services.capability_catalog import (
    AGENT_ALLOWED,
    AGENT_DATA_EXCEPTIONS,
    CODE_FORBIDDEN,
    CODE_STEP_UP_REQUIRED,
    Capability,
    parse_stored_scopes,
)

CODE_NOT_FOUND = "api_token.not_found"
CODE_TTL_TOO_LONG = "api_token.ttl_too_long"
CODE_SCOPE_NOT_ALLOWED = "api_token.scope_not_allowed"
CODE_PROJECT_REQUIRED = "api_token.project_required"
CODE_ALREADY_REVOKED = "api_token.already_revoked"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


def _project_not_found(project_id: int) -> AppHttpException:
    """
    422 y no 404: el recurso de la ruta es el token; el proyecto es un CAMPO inválido del
    payload. Reusa ``project.not_found`` para que la SPA lo clasifique igual que en
    ``/projects``. Sin esto, la FK ``RESTRICT`` de ``api_tokens.project_id`` reventaba el
    ``INSERT`` y el operador recibía un 500 sin código.
    """
    return AppHttpException(
        message="El proyecto del token no existe.",
        status_code=422,
        public_context={"code": project_codes.CODE_NOT_FOUND, "project_id": project_id},
    )


def _require_issuer_step_up(admin, capability: Capability) -> None:
    """
    Re-autenticación del EMISOR para un scope de datos: contraseña fresca (step-up).

    Un token no tiene contraseña que reconfirmar, así que el step-up de ``data.*`` (invariante 11
    relajado para ``AGENT_DATA_EXCEPTIONS``) lo cumple quien lo emite o edita. ``method="POST"``
    fijo: el PATCH de edición también es una escritura y no puede heredar el "GET no pide" de
    ``assert_step_up``. Un ``admin`` que no es un ``Actor`` (llamada interna, dict legado) falla
    CERRADO: sin ventana de step-up no hay re-autenticación.
    """
    from app.core.actor import Actor
    from app.core.step_up import assert_step_up

    if not isinstance(admin, Actor):
        raise AppHttpException(
            message="Esta operación requiere confirmar tu contraseña.",
            status_code=403,
            public_context={"code": CODE_STEP_UP_REQUIRED},
        )
    if admin.is_agent:
        raise AppHttpException(
            message="No tienes permiso para esta operación.",
            status_code=403,
            public_context={"code": CODE_FORBIDDEN},
        )
    assert_step_up(admin, capability, method="POST")


def _validate_scopes(raw_scopes: list[str], *, admin) -> list[str]:
    """
    Valida los scopes contra el **techo de agente** y devuelve sus valores canónicos.

    ``admin`` es el EMISOR (alta y PATCH lo pasan): si algún scope es de datos
    (``AGENT_DATA_EXCEPTIONS``) exige un step-up FRESCO del emisor y deja un rastro
    ``api_token.data_scope_grant`` con ``record_intent`` fail-closed (si el rastro no se persiste,
    el scope no se otorga). Los scopes sin datos no tocan ``admin``.

    Una sola implementación para el alta y la edición: si cada ruta tuviera su copia, la
    edición podría quedar más laxa que el alta y el PATCH sería la puerta trasera para darle a
    un token una capacidad que mute o divulgue. El techo excluye toda capacidad así (salvo
    la excepción cerrada de datos, arriba) y ``access.admin``, por eso ni un token ni su edición
    pueden escalar privilegios. Los errores llevan solo el ``allowed`` del techo, nunca el detalle
    de la fila.
    """
    validos: list[str] = []
    for raw in raw_scopes:
        try:
            cap = Capability(raw)
        except ValueError as exc:
            raise AppHttpException(
                message=f"Scope inválido: {raw!r}.",
                status_code=422,
                public_context={"code": CODE_SCOPE_NOT_ALLOWED},
            ) from exc
        if cap not in AGENT_ALLOWED:
            raise AppHttpException(
                message=(
                    f"El scope {raw!r} está fuera del techo de agente: un token no puede "
                    "recibir una capacidad que mute o divulgue."
                ),
                status_code=422,
                public_context={
                    "code": CODE_SCOPE_NOT_ALLOWED,
                    "allowed": sorted(c.value for c in AGENT_ALLOWED),
                },
            )
        validos.append(cap.value)

    datos = sorted(c for c in {Capability(v) for v in validos} if c in AGENT_DATA_EXCEPTIONS)
    for cap in datos:
        _require_issuer_step_up(admin, cap)
    if datos:
        # Fail-closed: otorgar acceso a filas de un tercero no puede quedar sin rastro.
        audit.record_intent(
            "api_token.data_scope_grant",
            admin=admin,
            target_type="api_token",
            touched_engine=False,
            detail=f"INTENT otorgar scopes de datos [{','.join(c.value for c in datos)}] a un token",
        )
    return validos


def _has_data_scope(scopes: list[str]) -> bool:
    return any(Capability(v) in AGENT_DATA_EXCEPTIONS for v in scopes)


def _data_token_ttl_cap_is_active() -> bool:
    """
    ¿Rige el tope PROPIO de vida de los tokens de datos?

    `MCP_DATA_TOKEN_MAX_TTL_DAYS=0` (el valor por defecto) lo desactiva: un token con scope de
    datos vive entonces lo mismo que cualquiera, hasta `MCP_TOKEN_MAX_TTL_DAYS`. Se lee del módulo
    en cada llamada, no se cachea, para que un cambio de configuración en un test o al arrancar se
    vea en la siguiente emisión o edición.
    """
    return MCP_DATA_TOKEN_MAX_TTL_DAYS > 0


def _require_editor_may_add_data_scopes(admin, fila: ApiToken, added: list[Capability]) -> None:
    """
    Agregar un scope de datos a un token YA emitido exige ser quien lo emitió o tener hoy ese
    permiso.

    Sin esta regla cualquier ``access.admin`` podía sumarle ``data.read``/``data.query`` al token
    de otro. Como el token ejerce la INTERSECCIÓN con las capacidades de su emisor, si el emisor
    las tiene (es ``owner``), el token ganaba lectura de filas por la decisión de un tercero, sin
    que el emisor participara y sin el segundo aprobador que el catálogo pide cuando esos
    permisos se otorgan sueltos. Si el emisor no las tiene, el scope quedaba inerte, así que ese
    caso no cambia nada. Solo cuenta lo que se AGREGA: quitar un scope de datos, o editar los
    demás permisos de un token que ya los traía, no exige nada nuevo.

    El rastro ``api_token.data_scope_grant`` (intención) lo deja ``_validate_scopes`` antes de
    mirar la fila, así que un intento denegado queda auditado como intento.
    """
    if not added:
        return
    from app.core.actor import identity_of  # import local: el módulo lo hace igual en create_token

    editor_id, _ = identity_of(admin)
    editor_is_the_original_issuer = (
        editor_id is not None and editor_id == fila.created_by_admin_id
    )
    if editor_is_the_original_issuer:
        return
    capability_check = getattr(admin, "has", None)
    editor_holds_every_added_scope = callable(capability_check) and all(
        capability_check(capability) for capability in added
    )
    if editor_holds_every_added_scope:
        return
    raise AppHttpException(
        message=(
            "Solo quien emitió el token, o alguien que ya tenga ese permiso de datos, puede "
            "agregárselo. Pedile a un owner que emita un token nuevo con ese permiso."
        ),
        status_code=403,
        public_context={"code": CODE_FORBIDDEN},
    )


def _ttl_too_long_for_data(dias: int) -> AppHttpException:
    return AppHttpException(
        message=(
            f"Un token con scope de datos vive como máximo {MCP_DATA_TOKEN_MAX_TTL_DAYS} días: "
            "un bearer que lee filas de un tercero no puede quedar meses en el repo de otra gente."
        ),
        status_code=422,
        public_context={"code": CODE_TTL_TOO_LONG, "max_days": MCP_DATA_TOKEN_MAX_TTL_DAYS},
    )


class ApiTokenController:
    def _session(self):
        from app.core.database import Database

        return Database().get_declarative_base_session()

    @staticmethod
    def _serialize(t: ApiToken) -> dict:
        """
        **Nunca el secreto ni su HMAC.** El ``token_id`` sí, porque es la parte pública y es lo
        que aparece en el rastro de auditoría: sin él, una fila de `mcp.*` no se puede cruzar
        con el token que la originó.
        """
        ahora = _utcnow()
        # Los scopes EFECTIVOS, no el string crudo de la fila. La diferencia importa cuando la
        # fila fue manipulada: con `scopes="access.admin"` en la BD, el crudo se le mostraba al
        # operador como si el token tuviera esa capacidad, mientras el efectivo es vacío. La
        # autorización ya era fail-closed —`parse_scopes` intersecta con el techo de agente— pero
        # la PANTALLA afirmaba otra cosa, y en una revisión de accesos eso es lo que se lee.
        # ``parse_stored_scopes``: con el kill switch de datos apagado el scope sigue en la fila
        # (inerte). Mostrarlo evita que la SPA lo "pierda" al guardar un PATCH.
        efectivos = sorted(c.value for c in parse_stored_scopes(t.scopes))
        return {
            "id": t.id,
            "token_id": t.token_id,
            "name": t.name,
            "scopes": efectivos,
            "project_id": t.project_id,
            "expires_at": t.expires_at,
            "last_used_at": t.last_used_at,
            "revoked_at": t.revoked_at,
            "note": t.note,
            "active": t.revoked_at is None and t.expires_at > ahora,
            "created_at": t.created_at,
        }

    def list_tokens(self, *, limit: int, offset: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            q = session.query(ApiToken).order_by(ApiToken.created_at.desc(), ApiToken.id.desc())
            total = q.count()
            return [self._serialize(t) for t in q.limit(limit).offset(offset).all()], total
        finally:
            session.close()

    def create_token(self, data: dict, *, admin) -> dict:
        """
        Emite un token y devuelve el bearer **una sola vez**.

        Valida el TTL contra el tope y los scopes contra el **techo de agente**, no contra el
        catálogo completo: el techo ya excluye toda capacidad que mute o divulgue, así que un
        token no puede recibir una ni por error del operador ni por una fila manipulada. La
        intersección se vuelve a aplicar al autenticar (``token_actor``), o sea que es
        fail-closed en el lector además del escritor.
        """
        from app.core.actor import identity_of

        project_id = data.get("project_id")
        if not project_id:
            raise AppHttpException(
                message=(
                    "El token necesita un proyecto: un token sin proyecto no alcanzaría "
                    "ninguna base, así que lo único que podría significar es 'token global'."
                ),
                status_code=422,
                public_context={"code": CODE_PROJECT_REQUIRED},
            )

        dias = int(data.get("expires_in_days") or MCP_TOKEN_MAX_TTL_DAYS)
        if dias < 1 or dias > MCP_TOKEN_MAX_TTL_DAYS:
            raise AppHttpException(
                message=(
                    f"El TTL tiene que estar entre 1 y {MCP_TOKEN_MAX_TTL_DAYS} días. "
                    "No hay tokens perpetuos: un token de agente vive en el repo de otra gente."
                ),
                status_code=422,
                public_context={
                    "code": CODE_TTL_TOO_LONG,
                    "max_days": MCP_TOKEN_MAX_TTL_DAYS,
                },
            )

        validos = _validate_scopes(
            data.get("scopes") or [Capability.BLUEPRINTS_READ.value], admin=admin
        )
        if _data_token_ttl_cap_is_active() and _has_data_scope(validos):
            if dias > MCP_DATA_TOKEN_MAX_TTL_DAYS:
                raise _ttl_too_long_for_data(dias)

        token_id, secreto, bearer = mint()
        admin_id, _ = identity_of(admin)
        session = self._session()
        try:
            if session.get(Project, int(project_id)) is None:
                raise _project_not_found(int(project_id))
            fila = ApiToken(
                token_id=token_id,
                secret_hmac=token_hmac(secreto),
                name=data["name"],
                scopes=",".join(sorted(set(validos))),
                project_id=int(project_id),
                expires_at=_utcnow() + timedelta(days=dias),
                created_by_admin_id=admin_id,
                note=data.get("note"),
            )
            session.add(fila)
            try:
                session.commit()
            except IntegrityError:
                # Carrera: el proyecto se borró entre el chequeo y el INSERT. Solo se traduce
                # si de verdad es eso; cualquier otra violación sigue siendo un 500 honesto.
                session.rollback()
                if session.get(Project, int(project_id)) is None:
                    raise _project_not_found(int(project_id)) from None
                raise
            session.refresh(fila)
            salida = self._serialize(fila)
        finally:
            session.close()

        audit.record(
            "api_token.create",
            admin=admin,
            target_type="api_token",
            target_id=salida["id"],
            touched_engine=False,
            detail=(
                f"token={token_id} nombre='{data['name']}' proyecto={project_id} "
                f"scopes=[{','.join(validos)}] ttl={dias}d"
            ),
        )
        # El bearer viaja SOLO acá. `_serialize` no lo tiene, así que ningún listado posterior
        # puede devolverlo ni por accidente.
        return {**salida, "token": bearer}

    def revoke_token(self, token_pk: int, *, admin) -> dict:
        """
        Revoca. Idempotente en el efecto y **409 si ya estaba revocado**, para que quien lo pide
        sepa que no fue su acción la que cortó el acceso.

        No hay "reactivar": ``revoked_at`` no se deshace. Un ciclo revocar/reactivar dejaría un
        token que alguien creyó muerto y no lo está, y eso es peor que emitir uno nuevo.
        """
        session = self._session()
        try:
            fila = session.get(ApiToken, token_pk)
            if fila is None:
                raise AppHttpException(
                    message="Token no encontrado.",
                    status_code=404,
                    public_context={"code": CODE_NOT_FOUND},
                )
            if fila.revoked_at is not None:
                raise AppHttpException(
                    message="Este token ya estaba revocado.",
                    status_code=409,
                    public_context={"code": CODE_ALREADY_REVOKED},
                )
            fila.revoked_at = _utcnow()
            session.commit()
            session.refresh(fila)
            salida = self._serialize(fila)
        finally:
            session.close()

        audit.record(
            "api_token.revoke",
            admin=admin,
            target_type="api_token",
            target_id=token_pk,
            touched_engine=False,
            detail=f"token={salida['token_id']} nombre='{salida['name']}'",
        )
        return salida

    def update_token(self, token_pk: int, data: dict, *, admin) -> dict:
        """
        Reemplaza los ``scopes`` de un token existente. **Solo los scopes**.

        Los scopes viven en la fila y ``mcp_auth.authenticate`` la lee en cada request, sin
        caché: el cambio rige desde la llamada siguiente y el bearer no cambia, así que no hay
        que reemitir ni redistribuir nada. Se valida contra el mismo techo de agente que el alta
        (``_validate_scopes``).

        **409 si está revocado**: editar un token muerto no tiene efecto útil y daría la falsa
        impresión de haberlo tocado (mismo código que ``revoke_token``). Uno vencido sí se puede
        editar: es inofensivo y no justifica un código nuevo.

        La auditoría registra scopes antes→después y nada más: ni secreto ni HMAC.
        """
        validos = sorted(set(_validate_scopes(data["scopes"], admin=admin)))
        session = self._session()
        try:
            fila = session.get(ApiToken, token_pk)
            if fila is None:
                raise AppHttpException(
                    message="Token no encontrado.",
                    status_code=404,
                    public_context={"code": CODE_NOT_FOUND},
                )
            if fila.revoked_at is not None:
                raise AppHttpException(
                    message="Este token está revocado: no se puede editar.",
                    status_code=409,
                    public_context={"code": CODE_ALREADY_REVOKED},
                )
            # Los EFECTIVOS de antes, igual que los muestra `_serialize`, para que el rastro diga
            # lo que el token podía hacer y no un string crudo de la fila.
            antes = sorted(c.value for c in parse_stored_scopes(fila.scopes))
            data_scopes_before = {
                Capability(v) for v in antes if Capability(v) in AGENT_DATA_EXCEPTIONS
            }
            data_scopes_added = sorted(
                (
                    Capability(v)
                    for v in validos
                    if Capability(v) in AGENT_DATA_EXCEPTIONS
                    and Capability(v) not in data_scopes_before
                ),
                key=lambda capability: capability.value,
            )
            _require_editor_may_add_data_scopes(admin, fila, data_scopes_added)
            if _data_token_ttl_cap_is_active() and _has_data_scope(validos):
                latest_allowed_expiry = _utcnow() + timedelta(days=MCP_DATA_TOKEN_MAX_TTL_DAYS)
                if fila.expires_at > latest_allowed_expiry:
                    # La vida restante cuenta: agregar datos a un token de 90 días lo dejaría
                    # leyendo filas 90 días. Hay que emitir otro (o esperar a que le queden <=
                    # el tope).
                    raise _ttl_too_long_for_data(0)
            fila.scopes = ",".join(validos)
            session.commit()
            session.refresh(fila)
            salida = self._serialize(fila)
        finally:
            session.close()

        audit.record(
            "api_token.update",
            admin=admin,
            target_type="api_token",
            target_id=token_pk,
            touched_engine=False,
            detail=(
                f"token={salida['token_id']} nombre='{salida['name']}' "
                f"scopes=[{','.join(antes)}]->[{','.join(validos)}]"
            ),
        )
        return salida
