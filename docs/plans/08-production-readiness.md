# 08 — Production readiness: estado y bloqueantes

**Estado global:** 🔴 **NO listo para producción** (P0 #1/#2 cerrados; quedan TLS MySQL,
secretos, CI/CD). Veredicto consolidado de las revisiones de seguridad,
despliegue/observabilidad y testing/QA (2026-06-23, actualizado 2026-06-24). El núcleo
(anti-inyección, cifrado Fernet, auth de sesión, inventario) es sólido; faltan
bloqueantes concretos antes de exponer el gateway.

Leyenda: ✅ hecho · 🟡 parcial · ⛔ pendiente.

---

## P0 — Bloqueantes antes de CUALQUIER exposición

### #1 ✅ Verificación contra motores reales = DONE
Stack Docker levantado; migraciones Alembic verificadas en MariaDB 11.8.6;
grants/revokes/introspección de grants contra MariaDB y PostgreSQL reales
(22 checks MariaDB + 16 checks PostgreSQL — todos OK). 251/251 tests pytest en verde.
Scripts de verificación end-to-end corridos contra `gw-it-mariadb` (127.0.0.1:33061)
y `gw-it-postgres` (127.0.0.1:54321). Operaciones DDL/DCL (create/drop db/user,
GRANT/REVOKE/LIST) ejecutadas y verificadas contra ambos motores reales.

### #2 ✅ Menor privilegio en la ruta de GRANT
- ✅ **Hecho:** eliminado el `GRANT ALL PRIVILEGES` automático al crear una managed
  database. El propietario **no recibe privilegios por defecto** (en PostgreSQL queda
  como OWNER nativo; en MySQL/MariaDB sin privilegios). Solo el pseudo-root de conexión
  tiene todo.
- ✅ **Completado (2026-06-24):** el catálogo cerrado `app/services/db_admin/privileges.py`
  está cableado a **todos** los flujos de escritura DCL (`grant_object`/`revoke_object`/
  `list_grants`/`can_grant`). Los endpoints `/grants` y `/grantable` están construidos y
  verificados contra motores reales. Pre-check `can_grant` con fail-fast 403 antes de
  ejecutar cualquier GRANT. `grant_database`/`revoke_database` reimplementados como caso
  particular de `grant_object` (retrocompat). Catálogo cerrado cableado a todos los flujos
  DCL — el regex laxo ya no participa en ninguna ruta de escritura.

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
- **Plan 07 Fase 1** 🟡: endpoints GRANT/REVOKE/LIST granulares implementados y
  verificados contra motores reales (MariaDB + PostgreSQL). `AuditLog` ampliado
  (campos DCL granulares) y tests de integración formales (`@pytest.mark.integration`)
  pendientes. No anunciar "gestión de permisos" como completamente lista (Fases 2/3
  y deuda de Fase 1 pendientes).

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
251 tests verdes; cadena Alembic con una sola cabeza. **Grants granulares verificados
contra motores reales (MariaDB + PostgreSQL); catálogo cerrado cableado a todos los flujos
DCL; pre-check `can_grant` con fail-fast 403.**

---

## Deuda de documentación detectada
- `docs/features/remote-connections.md`: **no** documenta `ssl_mode` por servidor ni el
  caveat del downgrade silencioso en MySQL/MariaDB.
- `docs/features/database-management.md`: actualizar a la política "sin privilegios por
  defecto" (ya no se otorga ALL al crear).
- `docs/features/permissions.md`: documentación del módulo de permisos granulares (Plan
  07 Fase 1) — endpoints, esquemas Pydantic, flujos GRANT/REVOKE/LIST/GRANTABLE/PROVISION,
  semántica de catálogo cerrado y pre-check `can_grant`. **Nuevo archivo, creado.**
- Este documento (08) consolida el estado; mantenerlo al cerrar cada bloqueante.

## Camino mínimo a un primer deploy interno controlado
1. ✅ Levantar Docker y verificar stack + migraciones + contrato contra motores reales (#1).
2. ✅ Cablear el catálogo cerrado a los flujos DCL y construir endpoints granulares (#2).
3. Imponer TLS real en MySQL/MariaDB (`require` que rechace) (#3).
4. ✅ Allowlist de destino (#4) + secreto nuevo desde gestor (#5 — pendiente).
5. CI mínimo (ruff + pytest + migración en MariaDB efímera).
