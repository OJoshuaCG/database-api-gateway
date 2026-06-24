# 08 — Production readiness: estado y bloqueantes

**Estado global:** 🔴 **NO listo para producción.** Veredicto consolidado de las revisiones
de seguridad, despliegue/observabilidad y testing/QA (2026-06-23). El núcleo
(anti-inyección, cifrado Fernet, auth de sesión, inventario) es sólido; faltan
bloqueantes concretos antes de exponer el gateway.

Leyenda: ✅ hecho · 🟡 parcial · ⛔ pendiente.

---

## P0 — Bloqueantes antes de CUALQUIER exposición

### #1 ⛔ Verificación contra motores reales = CERO
Toda la suite (201 tests) corre sobre **SQLite**, que no soporta GRANT/REVOKE/CREATE
USER ni la introspección por dialecto. **Ninguna** operación DDL/DCL se ha ejecutado
contra MySQL/MariaDB/PostgreSQL reales, ni las migraciones, ni el stack Docker.
- **Validar:** levantar `docker compose --profile test up --build`; confirmar
  `alembic upgrade head` en MariaDB real; batería de contrato (create/drop db/user,
  introspección) contra MariaDB 11 y PostgreSQL 17.
- **Bloqueado por:** Docker Desktop sin integración WSL en el entorno actual.

### #2 🟡 Menor privilegio en la ruta de GRANT
- ✅ **Hecho:** eliminado el `GRANT ALL PRIVILEGES` automático al crear una managed
  database. El propietario **no recibe privilegios por defecto** (en PostgreSQL queda
  como OWNER nativo; en MySQL/MariaDB sin privilegios). Solo el pseudo-root de conexión
  tiene todo.
- ⛔ **Falta:** el método `grant_database` (usado por `reassign_owner`) aún valida con el
  regex laxo de `identifiers.py` y su default es `ALL PRIVILEGES`. El catálogo cerrado
  `app/services/db_admin/privileges.py` **no está cableado** a ningún flujo de escritura.
  Falta construir los adapters granulares (`grant_object`/`revoke_object`/`can_grant`) y
  los endpoints `/grants` (incrementos pendientes del Plan 07).

### #3 🟡 TLS hacia los motores destino
- ✅ **Hecho:** TLS **por servidor** (`ssl_mode` en el `Server`), opcional. `require`
  **cifra** el transporte. En **PostgreSQL**, `require` **rechaza** la conexión si el
  servidor no tiene TLS (comportamiento correcto de libpq).
- ⛔ **Falta:**
  - **MySQL/MariaDB con `require` NO rechaza**: si el servidor no soporta TLS, pymysql
    **cae a texto plano en silencio** (downgrade). Hoy `require` se comporta como
    `PREFERRED`. Hay que **imponer** el cifrado en nuestra capa (verificar tras conectar
    y abortar si no quedó cifrado).
  - **Verificación de CA** (`verify-ca`/`verify-full`) no está modelada: no hay campo para
    el material de CA por servidor. En MySQL/MariaDB no se verifica el certificado; en
    PostgreSQL depende de ubicaciones por defecto del host. → protege de sniffing pasivo,
    **no** de MitM activo.
- **Decisión:** NO se añadió guard que obligue TLS en producción (es opcional por
  conexión, por decisión de producto).

### #4 ✅ SSRF — allowlist de destino (IMPLEMENTADO)
`app/core/net_guard.py` valida el host al registrar/editar un `Server`: rechaza
**loopback, link-local/metadata (`169.254.169.254`), multicast, no especificados y
reservados**; resuelve hostnames por DNS. Los rangos **privados se permiten por defecto**
(las BD destino suelen ser internas); allowlist estricta **opcional** vía
`REMOTE_ALLOWED_CIDRS`. Conmutable con `REMOTE_SSRF_GUARD_ENABLED` (default True).
Tests: `tests/test_ssrf_guard.py`.
- ⚠️ **Caveat:** valida en el REGISTRO, no protege de DNS-rebinding (revalidar la IP
  justo antes de conectar = mejora futura).

### #5 ⛔ Gestión y rotación de secretos
- La clave Fernet de `.env.docker` (real, ya tocó disco) debe tratarse como **quemada**;
  generar `SECRET_KEY`/`CRYPTO_KEY_SALT` nuevos e independientes desde un gestor de
  secretos (no `.env` en disco).
- **No existe** mecanismo de **rotación Fernet** (MultiFernet + re-cifrado). Hoy rotar la
  clave invalidaría todas las credenciales cifradas. Diseñarlo antes de cargar
  credenciales de producción reales.

---

## P1 — Antes de HA / exposición externa

- **CI inexistente** ⛔: sin `.github/workflows`, **ruff ni instalado/configurado**, sin
  marcadores `@pytest.mark.integration`, sin gate de migración (MariaDB efímera) ni de
  secretos (detect-secrets). Mínimo: lint + pytest + `alembic upgrade head` en CI.
- **Despliegue** ⛔:
  - Migraciones inline en el arranque (`entrypoint.sh`) sin estrategia de rollback ni
    job separado (en HA deberían correr como step de deploy, no por réplica).
  - `--forwarded-allow-ips "*"` en uvicorn → permite spoof de `X-Forwarded-For` y
    **evasión del rate limit**; restringir a la IP de nginx.
  - `HEALTHCHECK` del Dockerfile apunta a `/health` (liveness); debería usar
    `/health/ready`.
- **Rate limit con backend compartido** ✅: **Valkey** (fork OSI de Redis, wire-compatible)
  en `docker-compose.yml`; el `api` usa `RATE_LIMIT_REDIS_ENABLED=True` →
  `redis://valkey:6379`. Resuelve el bypass multi-worker. (Sigue pendiente subir
  `WORKERS` y desplegar el propio Valkey en HA.)
- **Plan 07 incompleto** 🟡: solo catálogo/validación; faltan los endpoints reales de
  GRANT/REVOKE granular. No anunciar "gestión de permisos" como lista.

---

## P2 — Endurecimiento

- **Observabilidad** ⛔: logs no estructurados (JSON), sin métricas (Prometheus/OTel),
  sin circuit breakers hacia motores remotos, sin SLOs/alertas.
- **Auditoría** 🟡: `audit_log` es best-effort y no append-only; considerar sink externo
  / tabla INSERT-only para operaciones con credencial raíz.
- **Varios:** rotación de session id al login; `DOCS_ENABLED=False` en prod; backups
  automatizados de la BD de metadatos; mover `detect-secrets` a deps de dev.

---

## Lo que está SÓLIDO (no re-litigar)
Anti-inyección de identificadores (doble capa); no fuga de credenciales
(`map_driver_error`, `ServerOut`); Argon2 + login `5/minute` + 401 genérico; guards de
arranque en prod para SECRET_KEY/ADMIN_PASSWORD/CORS; DROP con doble confirmación;
readiness probe `/health/ready`; catálogo de privilegios validado (whitelist cerrada);
201 tests verdes en la capa de inventario/lógica pura; cadena Alembic con una sola cabeza.

---

## Deuda de documentación detectada
- `docs/features/remote-connections.md`: **no** documenta `ssl_mode` por servidor ni el
  caveat del downgrade silencioso en MySQL/MariaDB.
- `docs/features/database-management.md`: actualizar a la política "sin privilegios por
  defecto" (ya no se otorga ALL al crear).
- Este documento (08) consolida el estado; mantenerlo al cerrar cada bloqueante.

## Camino mínimo a un primer deploy interno controlado
1. Levantar Docker y verificar stack + migraciones + contrato contra motores reales (#1).
2. Cablear el catálogo cerrado a `grant_database` y eliminar el default ALL (#2).
3. Imponer TLS real en MySQL/MariaDB (`require` que rechace) (#3).
4. Allowlist de destino (#4) + secreto nuevo desde gestor (#5).
5. CI mínimo (ruff + pytest + migración en MariaDB efímera).
