# 00 — Deuda técnica y pendientes de la Iteración 1

**Estado:** 🟡 Parcial · **Depende de:** — · **Esfuerzo:** bajo

Cierra los cabos sueltos detectados en la revisión de la Iteración 1 antes de
construir sobre ella. Ninguno bloquea avanzar, pero conviene resolverlos pronto.

> **Avance a 2026-06-12:** resueltos el ítem 4 (CORS, ✅) y el ítem 5 `SESSION_SECRET`
> (parcial). Además, fuera de este doc se endurecieron bloqueantes de la revisión de
> producción: TLS hacia los motores (`REMOTE_SSL_MODE`), doble confirmación en DROP
> DATABASE/USER, compensación de fallo parcial en `create_database`, readiness honesto
> (`/health/ready`) y quoting de `'user'@'host'` en MySQL. Ítem 2 (regenerar migración
> contra MySQL) sigue pendiente. Ítem 5c/filtros de `GET /servers` sigue sin abordarse.
>
> **Avance a 2026-06-24:** ítem 1 (verificación contra motores reales) ✅ resuelto —
> stack Docker levantado, 22 checks MariaDB + 16 PostgreSQL OK, 251/251 tests en verde.
> Ítem 6 (GRANT/REVOKE/Plan 07) ✅ resuelto — endpoints GRANT/REVOKE/LIST/GRANTABLE,
> provisión unificada y `apply-profile` implementados y verificados contra motores reales.

## Tareas

### 1. Verificar introspección contra motores reales (ALTA) — ✅ RESUELTO (2026-06-24)
El SQL específico de dialecto (`list_databases`, `list_users`) y la semántica de
schema de MySQL/PostgreSQL **no se han ejecutado contra un servidor vivo** (el
entorno de desarrollo no tenía Docker/MySQL/PG). El parsing genérico vía `Inspector`
sí está cubierto por tests sobre SQLite.

- **Acción:** levantar un MySQL/MariaDB y un PostgreSQL reales (Docker o servidores
  de prueba), registrar cada uno, y ejecutar `test-connection`, `/databases`,
  `/users`, `/databases/{db}/tables`, `/.../schema`.
- **Verificación:** estructura correcta por dialecto; **nunca** filas de datos; los
  usuarios/BDs de sistema quedan excluidos.
- **Hecho:** Verificado 2026-06-24: stack Docker levantado, 22 checks MariaDB + 16 PostgreSQL OK, 251/251 tests en verde.

### 2. Regenerar la migración inicial contra MySQL (ALTA)
La migración `alembic/versions/*inventory_servers*` se autogeneró sobre SQLite (usa
`batch_alter_table`; funcional en MySQL pero no idiomático).

- **Acción:** con `.env` apuntando a tu MySQL/MariaDB real (`DB_ENGINE=mysql+pymysql`),
  borrar la migración actual y `uv run alembic revision --autogenerate -m "inventory: servers"`.
- **Cuidado:** si la BD ya tiene una tabla `users` previa, revisar el diff para no
  recrearla.

### 3. Política de identificadores para BDs preexistentes (MEDIA)
La whitelist actual (`^[A-Za-z_][A-Za-z0-9_]*`) rechaza nombres con `-`, `.` o
no-ASCII. Si se necesita introspeccionar BDs existentes con esos nombres, devuelve 422.

- **Opción A (recomendada):** mantener whitelist estricta para objetos que el gateway
  *crea*, y para *introspección* de objetos preexistentes ampliar la whitelist de forma
  controlada (permitir `-`, `.` y dígitos iniciales) manteniendo el quoting por dialecto.
- **Decisión a confirmar:** ¿los nombres de BD/usuario los controla siempre el gateway?
  Si es así, no hace falta tocar nada.

### 4. CORS + cookies de sesión (MEDIA) — ✅ RESUELTO (2026-06-12)
`allow_credentials=True` es incompatible con `CORS_ORIGINS="*"` en navegadores.

- **Hecho:** `versioned_app.cors_allow_credentials()` desactiva `allow_credentials`
  cuando los orígenes incluyen `*` (o están vacíos), y `environments.py` **aborta el
  arranque en producción** si `CORS_ORIGINS` contiene `*`. Cubierto por `test_hardening`.

### 5. Endurecimientos menores (BAJA)
- ✅ `SESSION_SECRET` ya es una variable propia (`environments.py`); sigue cayendo a `SECRET_KEY` si está vacío, así que en producción **debe** definirse por separado.
- Considerar tokens CSRF para las operaciones mutantes (hoy mitigado con `same_site=lax`).
- Filtros de listado en `GET /servers` (`?engine=&status=&is_active=`) — quedaron diferidos.

### 6. Endpoints granulares GRANT/REVOKE / módulo de permisos (Plan 07) — ✅ RESUELTO (2026-06-24)
Gestión granular de permisos cross-engine (GRANT/REVOKE/LIST/GRANTABLE) y aprovisionamiento por perfil estaban pendientes como trabajo propio del plan 07.

- **Hecho:** Completado 2026-06-24: `list_grants`, `can_grant`, endpoints GRANT/REVOKE/LIST/GRANTABLE, provisión unificada y `apply-profile` implementados y verificados contra motores reales.

## Verificación
- Tests existentes siguen en verde (`uv run pytest`).
- Checklist 1 y 2 ejecutados contra motores reales y documentados.
