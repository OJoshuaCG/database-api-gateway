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

from app.core.environments import MCP_TOKEN_MAX_TTL_DAYS
from app.core.mcp_auth import mint, token_hmac
from app.exceptions import AppHttpException
from app.models.api_token import ApiToken
from app.services import audit
from app.services.capability_catalog import AGENT_ALLOWED, Capability, parse_scopes

CODE_NOT_FOUND = "api_token.not_found"
CODE_TTL_TOO_LONG = "api_token.ttl_too_long"
CODE_SCOPE_NOT_ALLOWED = "api_token.scope_not_allowed"
CODE_PROJECT_REQUIRED = "api_token.project_required"
CODE_ALREADY_REVOKED = "api_token.already_revoked"


def _utcnow() -> datetime:
    return datetime.now(UTC).replace(tzinfo=None)


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
        # fila fue manipulada: con `scopes="gateway.admin"` en la BD, el crudo se le mostraba al
        # operador como si el token tuviera esa capacidad, mientras el efectivo es vacío. La
        # autorización ya era fail-closed —`parse_scopes` intersecta con el techo de agente— pero
        # la PANTALLA afirmaba otra cosa, y en una revisión de accesos eso es lo que se lee.
        efectivos = sorted(c.value for c in parse_scopes(t.scopes))
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

        scopes = data.get("scopes") or [Capability.BLUEPRINTS_READ.value]
        validos = []
        for raw in scopes:
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

        token_id, secreto, bearer = mint()
        admin_id, _ = identity_of(admin)
        session = self._session()
        try:
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
            session.commit()
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
