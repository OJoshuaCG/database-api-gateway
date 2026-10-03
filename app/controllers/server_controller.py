"""
Controller de Servers.

- CRUD del inventario sobre la BD de metadatos del gateway (ORM SQLAlchemy).
- Cifra/descifra la credencial pseudo-root con `app.core.crypto`.
- Para test-connection e introspección, arma un `ServerTarget` (descifrando en
  memoria) y delega en el `ServerAdapter` correspondiente.

La credencial descifrada NUNCA se persiste, se serializa ni se loguea.
"""

import secrets
from typing import TYPE_CHECKING

from sqlalchemy.exc import IntegrityError

from app.core.crypto import CryptoConfigError, CryptoError, decrypt, encrypt
from app.core.database import Database
from app.core.net_guard import validate_remote_host
from app.core.environments import (
    DB_HOST,
    DB_NAME,
    DB_PASS,
    DB_PORT,
    DB_USER,
    MCP_READONLY_ACCOUNT_HOST,
    MCP_READONLY_ACCOUNT_USERNAME,
    REMOTE_SSL_MODE,
)
from app.core import remote_engine
from app.core.remote_engine import ServerTarget
from app.exceptions import AppHttpException
from app.models.enums import EngineType, ServerStatus
from app.models.managed_database import ManagedDatabase
from app.models.server import Server
from app.models.server_user import ServerUser
from app.services import audit
from app.services.server_catalog import (
    CODE_CREDENTIAL_REQUIRED_FOR_REBIND,
    CODE_READONLY_CREDENTIAL_MISSING,
    CODE_READONLY_PROBE_FAILED,
)
from app.services.db_admin.dtos import (
    ConnectionInfo,
    EngineUserInfo,
    SeedResult,
    StructureDump,
    TableSchema,
    TableStat,
)
from app.services.db_admin.database_scope import assert_database_in_scope
from app.services.db_admin.identifiers import reserved_database_names, validate_identifier
from app.services.db_admin.protected_accounts import (
    assert_not_privileged_role,
    assert_not_protected_by_name,
)
from app.services.db_admin.query_policy import is_gateway_metadata_target
from app.services.db_admin.factory import get_adapter

if TYPE_CHECKING:
    from app.core.actor import Actor



class ServerController:
    def __init__(self):
        self.db = Database(DB_NAME, DB_USER, DB_PASS, DB_HOST, DB_PORT)

    # ------------------------------------------------------------------ #
    # Helpers                                                            #
    # ------------------------------------------------------------------ #
    def _session(self):
        return self.db.get_declarative_base_session()

    @staticmethod
    def _serialize(s: Server) -> dict:
        """Dict seguro para la API: SIN la credencial cifrada."""
        return {
            "id": s.id,
            "name": s.name,
            "host": s.host,
            "port": s.port,
            "engine": s.engine,
            "root_username": s.root_username,
            "ssl_mode": s.ssl_mode,
            "status": s.status,
            "is_active": s.is_active,
            "notes": s.notes,
            "has_root_password": bool(s.root_password_encrypted),
            # La credencial de solo lectura del MCP: si existe y cuándo se verificó. Nunca el
            # usuario ni el cifrado (plan 12 §5.2).
            "has_readonly_credential": bool(
                s.readonly_username and s.readonly_password_encrypted
            ),
            "readonly_verified_at": s.readonly_verified_at,
            "created_at": s.created_at,
            "updated_at": s.updated_at,
        }

    @staticmethod
    def _encrypt_password(plaintext: str) -> str:
        try:
            return encrypt(plaintext)
        except (CryptoError, CryptoConfigError) as exc:
            raise AppHttpException(
                message="No se pudo cifrar la credencial del servidor.",
                status_code=500,
            ) from exc

    def _get_or_404(self, session, server_id: int) -> Server:
        server = session.get(Server, server_id)
        if not server:
            raise AppHttpException(
                message="Servidor no encontrado.",
                status_code=404,
                context={"server_id": server_id},
            )
        return server

    def _set_status(self, server_id: int, status: ServerStatus) -> None:
        session = self._session()
        try:
            server = session.get(Server, server_id)
            if server:
                server.status = status
                session.commit()
        finally:
            session.close()

    # ------------------------------------------------------------------ #
    # CRUD (solo BD del gateway)                                          #
    # ------------------------------------------------------------------ #
    def list_servers(self, limit: int, offset: int) -> tuple[list[dict], int]:
        session = self._session()
        try:
            total = session.query(Server).count()
            rows = (
                session.query(Server)
                .order_by(Server.id.desc())
                .limit(limit)
                .offset(offset)
                .all()
            )
            return [self._serialize(s) for s in rows], total
        finally:
            session.close()

    def get_server(self, server_id: int) -> dict:
        session = self._session()
        try:
            return self._serialize(self._get_or_404(session, server_id))
        finally:
            session.close()

    def create_server(self, data: dict, *, admin: "dict | Actor | None" = None) -> dict:
        """
        Registra un servidor en el inventario, con su credencial pseudo-root cifrada.

        AUDITADO, y no es un detalle: registrar un servidor es aportarle al gateway una
        credencial pseudo-root y un host, así que es la operación de mayor privilegio del
        plano de control. Este controller no auditaba NADA — era el único camino del repo que
        combinaba máximo privilegio con cero rastro. Nunca se registra la contraseña ni su
        cifrado: solo que la operación ocurrió y sobre qué fila.

        ``touched_engine=False`` a propósito: ``validate_remote_host`` resuelve DNS para el
        guard anti-SSRF pero no abre ninguna conexión al motor. El flag significa "contactó el
        motor", y acá no se contacta.
        """
        # Anti-SSRF: validar el destino ANTES de persistir/conectar.
        validate_remote_host(data["host"])
        session = self._session()
        try:
            server = Server(
                name=data["name"],
                host=data["host"],
                port=data["port"],
                engine=EngineType(data["engine"]),
                root_username=data["root_username"],
                root_password_encrypted=self._encrypt_password(data["root_password"]),
                ssl_mode=data.get("ssl_mode"),
                notes=data.get("notes"),
                is_active=data.get("is_active", True),
            )
            session.add(server)
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                audit.record(
                    "server.create",
                    status="error",
                    admin=admin,
                    target_type="server",
                    detail="nombre o host:puerto duplicado",
                )
                raise AppHttpException(
                    message="Ya existe un servidor con ese nombre o host:puerto.",
                    status_code=409,
                    context={"name": data.get("name")},
                ) from exc
            session.refresh(server)
            result = self._serialize(server)
            audit.record(
                "server.create",
                admin=admin,
                target_type="server",
                target_id=server.id,
                server_id=server.id,
                detail=f"alta de servidor '{server.name}' ({server.engine.value})",
            )
            return result
        finally:
            session.close()

    # Fuerza de cada ``ssl_mode`` (``None`` = sin TLS). Solo importa el orden.
    _SSL_STRENGTH = {
        None: 0, "disable": 0, "allow": 1, "prefer": 2,
        "require": 3, "verify-ca": 4, "verify-full": 5,
    }

    @classmethod
    def _rebind_fields(cls, server: Server, data: dict) -> list[str]:
        """
        Campos del ``PATCH`` que RE-APUNTAN la credencial guardada: ``host``, ``port`` o
        ``engine`` distintos, o un ``ssl_mode`` más débil que uno que exigía TLS
        (``require`` o más fuerte).

        POR QUÉ. Sin ``root_password`` en el mismo request, el ``root_password_encrypted``
        sobrevivía al re-apuntado, y el próximo test-connection u operación se lo mandaba al
        host NUEVO. Un host controlado por quien edita lo pide en claro sin esfuerzo
        (PostgreSQL ``AuthenticationCleartextPassword``, que libpq contesta por defecto;
        MySQL con auth-switch a ``mysql_clear_password``, que PyMySQL contesta), y bajar
        ``ssl_mode`` en el mismo PATCH le saca además el TLS. ``validate_remote_host`` limita
        A DÓNDE se apunta, no esto. ``root_username`` no dispara: la credencial sigue yendo
        al mismo destino.
        """
        fields: list[str] = []
        if data.get("host") is not None and (
            str(data["host"]).strip().lower() != (server.host or "").strip().lower()
        ):
            fields.append("host")
        if data.get("port") is not None and int(data["port"]) != int(server.port):
            fields.append("port")
        if data.get("engine") is not None and EngineType(data["engine"]) != server.engine:
            fields.append("engine")
        if "ssl_mode" in data:
            before = cls._SSL_STRENGTH.get(server.ssl_mode, 0)
            after = cls._SSL_STRENGTH.get(data["ssl_mode"], 0)
            if before >= cls._SSL_STRENGTH["require"] and after < before:
                fields.append("ssl_mode")
        return fields

    def update_server(self, server_id: int, data: dict, *, admin: "dict | Actor | None" = None) -> dict:
        """
        Edita un servidor del inventario.

        AUDITADO con la lista de campos que cambiaron, porque **editar un servidor puede
        re-apuntar un ``server_id`` existente a un host que el editor controla**: desde ese
        momento toda operación futura sobre ese id se ejecuta contra su máquina. El guard
        anti-SSRF limita a DÓNDE se puede apuntar, no QUIÉN puede hacerlo, así que el rastro
        de qué cambió es el único control que queda hasta que exista autorización por
        capacidad (ver ``docs/plans/13-usuarios-y-autorizacion-del-gateway.md`` §4.6).

        El ``detail`` lista NOMBRES de campo, nunca valores: ``root_password`` aparece como
        "credencial rotada" y jamás su contenido.
        """
        # Anti-SSRF: si cambia el host, validar el nuevo destino.
        if data.get("host") is not None:
            validate_remote_host(data["host"])
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            rebind = self._rebind_fields(server, data)
            if rebind and not data.get("root_password"):
                raise AppHttpException(
                    message=(
                        "Cambiar el host, el puerto o el motor del servidor, o debilitar su TLS, "
                        "exige volver a enviar 'root_password' en el mismo request: la "
                        "credencial guardada no se envía a un destino distinto del que se "
                        "registró."
                    ),
                    status_code=422,
                    context={"server_id": server_id},
                    public_context={
                        "code": CODE_CREDENTIAL_REQUIRED_FOR_REBIND,
                        "fields": rebind,
                    },
                )
            changed: list[str] = []
            # Re-apuntar el servidor DESCARTA la credencial de solo lectura del MCP. Es el mismo
            # agujero que el de ``root_password``: sin esto, la próxima tool del MCP le mandaba
            # esa credencial al host nuevo. Acá no se exige reenviarla en el mismo PATCH porque
            # vive en su propio endpoint; se borra (fail-closed) y el servidor sale del MCP hasta
            # que alguien la vuelva a registrar y verificar contra el destino nuevo.
            if rebind and (server.readonly_username or server.readonly_password_encrypted):
                server.readonly_username = None
                server.readonly_password_encrypted = None
                server.readonly_verified_at = None
                changed.append("credencial de solo lectura descartada por re-apuntado")
            for field in ("name", "host", "port", "notes", "is_active", "root_username", "ssl_mode"):
                if field in data:
                    if getattr(server, field) != data[field]:
                        changed.append(field)
                    setattr(server, field, data[field])
            if data.get("engine") is not None:
                if server.engine != EngineType(data["engine"]):
                    changed.append("engine")
                server.engine = EngineType(data["engine"])
            if data.get("root_password"):
                server.root_password_encrypted = self._encrypt_password(
                    data["root_password"]
                )
                changed.append("credencial rotada")
            try:
                session.commit()
            except IntegrityError as exc:
                session.rollback()
                audit.record(
                    "server.update",
                    status="error",
                    admin=admin,
                    target_type="server",
                    target_id=server_id,
                    server_id=server_id,
                    detail="nombre o host:puerto duplicado",
                )
                raise AppHttpException(
                    message="Ya existe un servidor con ese nombre o host:puerto.",
                    status_code=409,
                    context={"server_id": server_id},
                ) from exc
            session.refresh(server)
            result = self._serialize(server)
        finally:
            session.close()
        audit.record(
            "server.update",
            admin=admin,
            target_type="server",
            target_id=server_id,
            server_id=server_id,
            detail=("cambió: " + ", ".join(changed)) if changed else "sin cambios efectivos",
        )
        # Datos de conexión pudieron cambiar: descartar engines remotos cacheados.
        remote_engine.invalidate_server(server_id)
        return result

    def delete_server(self, server_id: int, *, admin: "dict | Actor | None" = None) -> None:
        """
        Borra un servidor del inventario. NO toca el motor. 409 ``access.scope_has_grants``
        si todavía hay accesos que apuntan a él (ver ``assert_scope_has_no_grants``).

        AUDITADO: el borrado se lleva con él la credencial pseudo-root cifrada y, por
        ``CASCADE``, el inventario de BDs y usuarios que colgaban de ese servidor. Se registra
        el nombre antes de borrar, porque después de la operación la fila ya no existe y el
        ``target_id`` suelto no le dice nada a quien lea el log.
        """
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            from app.models.capability_grant_model import assert_scope_has_no_grants

            assert_scope_has_no_grants(session, "server", server.id)
            name = server.name
            session.delete(server)
            session.commit()
        finally:
            session.close()
        audit.record(
            "server.delete",
            admin=admin,
            target_type="server",
            target_id=server_id,
            server_id=server_id,
            detail=f"baja de servidor '{name}' del inventario",
        )
        remote_engine.invalidate_server(server_id)

    # ------------------------------------------------------------------ #
    # Operaciones contra el servidor destino                              #
    # ------------------------------------------------------------------ #
    def _build_target(self, server_id: int) -> ServerTarget:
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            engine_value = (
                server.engine.value
                if isinstance(server.engine, EngineType)
                else str(server.engine)
            )
            try:
                password = decrypt(server.root_password_encrypted)
            except (CryptoError, CryptoConfigError) as exc:
                raise AppHttpException(
                    message="No se pudo descifrar la credencial del servidor.",
                    status_code=500,
                    context={"server_id": server_id},
                ) from exc
            return ServerTarget(
                server_id=server.id,
                dialect=engine_value,
                host=server.host,
                port=server.port,
                admin_user=server.root_username,
                admin_password=password,
                # TLS por conexión: el del servidor manda; si no tiene, cae al global.
                ssl_mode=server.ssl_mode if server.ssl_mode is not None else REMOTE_SSL_MODE,
            )
        finally:
            session.close()

    def test_connection(
        self,
        server_id: int,
        *,
        credential: str = "root",
        admin: "dict | Actor | None" = None,
    ) -> ConnectionInfo:
        if credential == "readonly":
            return self._verify_readonly(server_id, admin=admin)
        adapter = get_adapter(self._build_target(server_id))
        try:
            info = adapter.test_connection()
        except AppHttpException:
            self._set_status(server_id, ServerStatus.unreachable)
            raise
        self._set_status(server_id, ServerStatus.active)
        return info

    # ------------------------------------------------------------------ #
    # Credencial de SOLO LECTURA del MCP (plan 12 §5.2)                    #
    # ------------------------------------------------------------------ #
    def set_readonly_credential(
        self, server_id: int, data: dict, *, admin: "dict | Actor | None" = None
    ) -> dict:
        """
        Registra o reemplaza la credencial de solo lectura que usa el MCP para leer catálogos.

        **Borra la verificación** (``readonly_verified_at``) en el mismo paso: la observación de
        la sonda describe a la credencial ANTERIOR, y heredarla dejaría al MCP confiando en una
        cuenta que nadie probó. Hasta la próxima ``test-connection?credential=readonly`` el
        servidor queda fuera del MCP — el gate lo niega con ``mcp.readonly_credential_missing``.

        AUDITADO sin el usuario ni la contraseña: solo que la credencial cambió y sobre qué
        servidor.
        """
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            reemplazo = bool(server.readonly_username)
            server.readonly_username = data["username"]
            server.readonly_password_encrypted = self._encrypt_password(data["password"])
            server.readonly_verified_at = None
            session.commit()
            session.refresh(server)
            result = self._serialize(server)
        finally:
            session.close()
        audit.record(
            "server.readonly_credential.set",
            admin=admin,
            target_type="server",
            target_id=server_id,
            server_id=server_id,
            detail=(
                "credencial de solo lectura reemplazada; verificación borrada"
                if reemplazo
                else "credencial de solo lectura registrada; pendiente de verificación"
            ),
        )
        # El cache de engines indexa por usuario y huella de contraseña, así que no reusaría el
        # engine viejo; se invalida igual para no dejar vivo un pool con la credencial anterior.
        remote_engine.invalidate_server(server_id)
        return result

    def provision_readonly_credential(
        self, server_id: int, *, admin: "dict | Actor | None" = None
    ) -> dict:
        """
        Crea (o RE-CONVERGE) la cuenta de solo lectura del MCP con la pseudo-root, la registra
        cifrada y corre la sonda negativa. Un click en lugar de CREATE USER + PUT + verificar.

        SOLO se llega por la API HTTP/SPA. ``app/mcp`` no puede importar este módulo
        (``tests/test_mcp_import_guard.py``) ni existe una tool que lo llame: el MCP no toca la
        pseudo-root.

        El request no trae NADA: lo decide todo el servidor. Usuario y host salen de
        ``MCP_READONLY_ACCOUNT_*``, los grants de ``readonly_probe.MYSQL_READONLY_*`` y la
        contraseña de ``secrets``. La contraseña nunca se devuelve ni se loguea: del adapter al
        ``set_readonly_credential`` viaja en una variable local.

        Alcance (POR SERVIDOR, no por base): ``SELECT`` & co. sobre TODAS las bases no internas
        del servidor. Quedan fuera las del sistema del motor y la base de metadatos del propio
        gateway si está co-alojada (``server_users`` guarda pseudo-roots cifradas). Una base
        creada DESPUÉS no queda cubierta hasta repetir el aprovisionamiento. Nunca ``SELECT ON
        *.*`` ni sobre ``mysql.*``. ``exclude_gateway_internal_tables`` no aplica: un grant a
        nivel base no puede excluir tablas; el MCP filtra las internas al introspectar.

        Orden y recuperación (MySQL/MariaDB no tienen DDL transaccional): 1) se BORRA la
        verificación, porque desde que la contraseña rota la guardada ya no sirve y no puede
        seguir figurando como verificada; 2) cuenta + grants en el motor (idempotente: rota y
        re-aplica si existe); 3) se guarda cifrada; 4) sonda negativa. Cualquier corrida a medias
        se reintenta tal cual. Si la sonda falla, ``_verify_readonly`` ya dejó la verificación
        en ``null`` y devuelve el 422 ``server.readonly_probe_failed``.
        """
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            engine_value = (
                server.engine.value
                if isinstance(server.engine, EngineType)
                else str(server.engine)
            )
            root_username = server.root_username
            host, port = server.host, server.port
        finally:
            session.close()

        username = MCP_READONLY_ACCOUNT_USERNAME
        account_host = MCP_READONLY_ACCOUNT_HOST
        validate_identifier(username, engine_value, "usuario")
        assert_not_protected_by_name(
            dialect=engine_value, username=username, root_username=root_username
        )
        adapter = get_adapter(self._build_target(server_id))
        assert_not_privileged_role(adapter, dialect=engine_value, username=username)

        reserved = reserved_database_names(engine_value)
        databases: list[str] = []
        omitidas = 0
        for name in adapter.list_databases():
            if name.lower() in reserved or is_gateway_metadata_target(
                host=host,
                port=port,
                database=name,
                gateway_host=DB_HOST,
                gateway_port=DB_PORT,
                gateway_database=DB_NAME,
            ):
                continue
            try:
                validate_identifier(name, engine_value, "base de datos", allow_existing=True)
            except AppHttpException:
                omitidas += 1  # un nombre raro no debe bloquear el resto, pero no se interpola
                continue
            databases.append(name)

        password = secrets.token_urlsafe(32)
        audit.record_intent(
            "server.readonly_credential.provision",
            admin=admin,
            target_type="server",
            target_id=server_id,
            server_id=server_id,
            detail=f"aprovisionamiento de la cuenta de solo lectura sobre {len(databases)} bases",
        )
        self._clear_readonly_verification(server_id)
        try:
            existed = adapter.provision_readonly_account(
                username, password, account_host, databases
            )
        except AppHttpException:
            audit.record(
                "server.readonly_credential.provision",
                status="error",
                admin=admin,
                target_type="server",
                target_id=server_id,
                server_id=server_id,
                touched_engine=True,
                detail="fallo al crear la cuenta de solo lectura en el motor; reintentable",
            )
            raise
        self.set_readonly_credential(
            server_id, {"username": username, "password": password}, admin=admin
        )
        audit.record(
            "server.readonly_credential.provision",
            admin=admin,
            target_type="server",
            target_id=server_id,
            server_id=server_id,
            touched_engine=True,
            detail=(
                ("cuenta existente: contraseña rotada y grants re-aplicados"
                 if existed else "cuenta creada")
                + f"; {len(databases)} bases cubiertas, {omitidas} omitidas por nombre"
            ),
        )
        self._verify_readonly(server_id, admin=admin)
        return self.get_server(server_id)

    def _clear_readonly_verification(self, server_id: int) -> None:
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            server.readonly_verified_at = None
            session.commit()
        finally:
            session.close()

    def clear_readonly_credential(
        self, server_id: int, *, admin: "dict | Actor | None" = None
    ) -> dict:
        """
        Quita la credencial de solo lectura: el servidor sale del alcance del MCP de inmediato.

        Es la palanca de emergencia granular del plan 12 §8 —corta a UN servidor sin tocar
        tokens, entornos ni bases— y por eso es idempotente: quitarla dos veces no es un error.
        """
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            tenia = bool(server.readonly_username or server.readonly_password_encrypted)
            server.readonly_username = None
            server.readonly_password_encrypted = None
            server.readonly_verified_at = None
            session.commit()
            session.refresh(server)
            result = self._serialize(server)
        finally:
            session.close()
        audit.record(
            "server.readonly_credential.clear",
            admin=admin,
            target_type="server",
            target_id=server_id,
            server_id=server_id,
            detail="credencial de solo lectura quitada" if tenia else "no tenía credencial",
        )
        remote_engine.invalidate_server(server_id)
        return result

    def _build_readonly_target(self, server_id: int) -> ServerTarget:
        """
        ``ServerTarget`` con la credencial de SOLO LECTURA. **Nunca lee la pseudo-root**: si la
        credencial no está, falla — no hay fallback, con ningún flag.
        """
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            if not (server.readonly_username and server.readonly_password_encrypted):
                raise AppHttpException(
                    message=(
                        "El servidor no tiene credencial de solo lectura registrada. Registrala "
                        "con PUT /servers/{id}/readonly-credential antes de verificarla."
                    ),
                    status_code=409,
                    context={"server_id": server_id},
                    public_context={"code": CODE_READONLY_CREDENTIAL_MISSING},
                )
            try:
                password = decrypt(server.readonly_password_encrypted)
            except (CryptoError, CryptoConfigError) as exc:
                raise AppHttpException(
                    message="No se pudo descifrar la credencial de solo lectura del servidor.",
                    status_code=500,
                    context={"server_id": server_id},
                ) from exc
            engine_value = (
                server.engine.value
                if isinstance(server.engine, EngineType)
                else str(server.engine)
            )
            return ServerTarget(
                server_id=server.id,
                dialect=engine_value,
                host=server.host,
                port=server.port,
                admin_user=server.readonly_username,
                admin_password=password,
                ssl_mode=server.ssl_mode if server.ssl_mode is not None else REMOTE_SSL_MODE,
            )
        finally:
            session.close()

    def _verify_readonly(
        self, server_id: int, *, admin: "dict | Actor | None" = None
    ) -> ConnectionInfo:
        """
        Sonda NEGATIVA (plan 12 §5.2): conecta con la credencial de solo lectura y exige que el
        motor observe que NO puede escribir. Solo si pasa, fija ``readonly_verified_at``.

        Un fallo **borra** la verificación anterior en vez de dejarla: una credencial que hoy
        puede escribir no sigue en el MCP porque hace dos semanas no podía. No toca el ``status``
        del servidor: ese describe la conexión con la pseudo-root, no esta.
        """
        from datetime import UTC, datetime

        target = self._build_readonly_target(server_id)
        adapter = get_adapter(target)
        info = adapter.test_connection()
        violaciones = adapter.readonly_violations()
        session = self._session()
        try:
            server = self._get_or_404(session, server_id)
            if violaciones:
                server.readonly_verified_at = None
            else:
                server.readonly_verified_at = datetime.now(UTC).replace(tzinfo=None)
            session.commit()
            verificada = server.readonly_verified_at
        finally:
            session.close()
        if violaciones:
            audit.record(
                "server.readonly_credential.verify",
                status="failure",
                admin=admin,
                target_type="server",
                target_id=server_id,
                server_id=server_id,
                touched_engine=True,
                detail="la credencial puede escribir: " + ", ".join(violaciones),
            )
            raise AppHttpException(
                message=(
                    "La credencial de solo lectura tiene privilegios de escritura o de más. "
                    "El servidor queda fuera del MCP hasta corregir sus grants."
                ),
                status_code=422,
                context={"server_id": server_id},
                public_context={
                    "code": CODE_READONLY_PROBE_FAILED,
                    "violations": violaciones,
                },
            )
        audit.record(
            "server.readonly_credential.verify",
            admin=admin,
            target_type="server",
            target_id=server_id,
            server_id=server_id,
            touched_engine=True,
            detail="sonda negativa superada: el motor no permite escribir con la credencial",
        )
        return info.model_copy(update={"readonly_verified_at": verificada})

    def list_databases(self, server_id: int) -> list[str]:
        return get_adapter(self._build_target(server_id)).list_databases()

    def list_users(self, server_id: int) -> list[EngineUserInfo]:
        return get_adapter(self._build_target(server_id)).list_users()

    def list_tables(self, server_id: int, database: str) -> list[str]:
        return get_adapter(self._build_target(server_id)).list_tables(database)

    def get_table_schema(
        self, server_id: int, database: str, table: str
    ) -> TableSchema:
        return get_adapter(self._build_target(server_id)).get_table_schema(
            database, table
        )

    # ------------------------------------------------------------------ #
    # Reconciliación (drift) y snapshot — Plan 09                          #
    # ------------------------------------------------------------------ #
    def reconcile(self, server_id: int) -> dict:
        """
        Cruza el plano EN VIVO (motor) con el INVENTARIO (gateway) y clasifica cada
        BD/usuario como ``managed`` (en ambos), ``unmanaged`` (solo en el motor →
        adoptable) u ``orphan`` (solo en el inventario → se borró por fuera).
        Read-only: no muta nada.
        """
        target = self._build_target(server_id)
        adapter = get_adapter(target)
        live_dbs = set(adapter.list_databases())
        live_users = adapter.list_users()
        is_pg = target.dialect == EngineType.postgresql.value

        session = self._session()
        try:
            inv_dbs = (
                session.query(ManagedDatabase)
                .filter(ManagedDatabase.server_id == server_id)
                .all()
            )
            inv_users = (
                session.query(ServerUser)
                .filter(ServerUser.server_id == server_id)
                .all()
            )
            inv_db_by_name = {d.name: d for d in inv_dbs}

            databases: list[dict] = []
            for name in sorted(live_dbs | set(inv_db_by_name)):
                d = inv_db_by_name.get(name)
                if d and name in live_dbs:
                    state = "managed"
                elif d:
                    state = "orphan"
                else:
                    state = "unmanaged"
                databases.append(
                    {
                        "name": name,
                        "state": state,
                        "managed_id": d.id if d else None,
                        "owner_id": d.owner_id if d else None,
                        "status": (d.status.value if hasattr(d.status, "value") else d.status)
                        if d
                        else None,
                    }
                )

            # Usuarios: en PG se matchea por username (no hay host); en MySQL por (user, host).
            def ukey(username: str, host: str | None) -> tuple:
                return (username,) if is_pg else (username, host or "%")

            inv_user_by_key = {ukey(u.username, u.host): u for u in inv_users}
            live_keys = {ukey(u.username, u.host) for u in live_users}
            live_meta = {ukey(u.username, u.host): u for u in live_users}

            users: list[dict] = []
            for key in sorted(live_keys | set(inv_user_by_key)):
                u = inv_user_by_key.get(key)
                in_live = key in live_keys
                if u and in_live:
                    state = "managed"
                elif u:
                    state = "orphan"
                else:
                    state = "unmanaged"
                if u:
                    username, host = u.username, u.host
                else:
                    live = live_meta[key]
                    username, host = live.username, live.host
                users.append(
                    {
                        "username": username,
                        "host": host,
                        "state": state,
                        "managed_id": u.id if u else None,
                    }
                )
        finally:
            session.close()

        return {"server_id": server_id, "databases": databases, "users": users}

    @staticmethod
    def _scoped_target(target: ServerTarget, database: str) -> ServerTarget:
        """
        Rechaza (409) una base de sistema o la de metadatos del gateway ANTES de leerla.

        Estos tres métodos son la puerta de ``POST /database-models/from-snapshot``: sin el
        guard, ``database="mysql"`` + ``data_tables=["user"]`` convertía los hashes de todas
        las cuentas del motor en semillas de un blueprint legible por ``viewer``. Ver
        ``db_admin.database_scope``.
        """
        assert_database_in_scope(
            database, dialect=target.dialect, host=target.host, port=target.port, side="source"
        )
        return target

    def snapshot(self, server_id: int, database: str) -> StructureDump:
        """Dump estructural EN VIVO de una BD (solo estructura, nunca filas)."""
        target = self._scoped_target(self._build_target(server_id), database)
        return get_adapter(target).dump_structure(database)

    def table_stats(self, server_id: int, database: str) -> list[TableStat]:
        """Estimación por tabla (filas + tiene PK) para informar la selección de datos."""
        target = self._scoped_target(self._build_target(server_id), database)
        return get_adapter(target).list_table_stats(database)

    def snapshot_data(
        self,
        server_id: int,
        database: str,
        tables: list[str],
        *,
        modes: dict[str, str],
        max_rows: int,
        max_bytes: int,
        batch_rows: int,
    ) -> list[SeedResult]:
        """
        Extrae datos-semilla de varias tablas reutilizando un solo target (la credencial
        se descifra una vez). Cada tabla se rinde como INSERT idempotente + rollback por PK.
        """
        adapter = get_adapter(self._scoped_target(self._build_target(server_id), database))
        return [
            adapter.dump_table_data(
                database, t, mode=modes.get(t, "upsert"),
                max_rows=max_rows, max_bytes=max_bytes, batch_rows=batch_rows,
            )
            for t in tables
        ]
