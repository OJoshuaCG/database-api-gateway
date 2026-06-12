# 06 — Operación: seguridad, auditoría y observabilidad (transversal)

**Estado:** Continuo · **Depende de:** transversal a 01–05 · **Esfuerzo:** medio/alto

Capacidades de plataforma que sostienen a las demás iteraciones. Conviene introducir
las primeras dos (auditoría y jobs) **junto con** la Iteración 2, porque 02–05 las necesitan.

## 1. Auditoría (prioridad ALTA — junto con Iter. 2) — ✅ IMPLEMENTADA (base)

> **Hecho (2026-06-12):** `app/models/audit_log.py` (tabla `audit_log`) + `app/services/audit.py`
> (helper `record(...)`, best-effort: un fallo al auditar nunca rompe la operación; toma
> Request ID e IP de los ContextVars; `detail` sin secretos). Integrada en los controllers
> mutantes (p. ej. `managed_database.create/update/delete/reassign_owner`). **Pendiente:**
> ampliar cobertura a todas las acciones futuras (migraciones, instalación, clonado) y
> evaluar inmutabilidad reforzada (append-only / retención).

Toda operación sensible (crear/borrar usuario o BD, grants, migraciones, instalar motor,
aprovisionar, clonar) debe quedar registrada de forma inmutable.

### `AuditLog` (`audit_logs`)
| Campo | Notas |
|---|---|
| `id`, `created_at` | |
| `actor` | admin que ejecutó (de `get_current_admin`) |
| `action` | `server.create`, `db.drop`, `user.grant`, `migration.apply`, ... |
| `target_type` / `target_id` | recurso afectado |
| `server_id` | servidor destino (si aplica) |
| `request_id` | correlación con `current_http_identifier` |
| `status` | `success\|error` |
| `detail` | JSON saneado (**sin** credenciales) |

- Implementar como helper/decorador en los controllers, o un middleware que registre
  las mutaciones. Reusar `_sanitize_dict` para no filtrar secretos.

## 2. Jobs en background (prioridad ALTA — requerido por 02–05)

Las operaciones largas (migraciones masivas, aprovisionamiento, instalación SSH, clonado)
**no pueden** correr dentro del request.

- **Opción ligera:** `FastAPI BackgroundTasks` / `run_in_threadpool` + tabla de estado de job.
  Suficiente para un solo proceso/worker.
- **Opción robusta (recomendada al crecer):** una cola — **ARQ** (async, Redis) o **Celery/RQ**.
  Permite reintentos, concurrencia controlada y persistencia.
- Patrón común: endpoint crea un `*Job` (`pending`) y encola → worker ejecuta y actualiza
  estado/reporte → endpoint `GET /...-jobs/{id}` para seguimiento.
- **Decisión a confirmar:** ¿un solo proceso (BackgroundTasks) o cola con Redis (ARQ)?
  Ya existe la variable `RATE_LIMIT_REDIS_*`; Redis podría reutilizarse.

## 3. Autenticación: migración a OIDC/SSO (cuando la empresa lo requiera)

La auth actual (sesión + admin único) está detrás de `get_current_admin`. Para SSO:
- Integrar **Authlib** con el IdP corporativo (Google Workspace / Entra ID / Authentik / Keycloak).
- Mapear identidades del IdP a la sesión; conservar `get_current_admin` como punto único.
- Añadir **roles** (admin/operador/lectura) si hace falta granularidad. Hoy hay un solo admin.

## 4. Gestión de secretos

Hoy: Fernet derivado de `SECRET_KEY` (bien para arrancar). Al escalar (claves SSH,
credenciales de proveedor cloud, passwords pseudo-root de muchos servidores):
- Evaluar **Vault / AWS Secrets Manager / similar**.
- Soportar **rotación** de la clave Fernet con `MultiFernet` (el `info=...-v1` ya versiona la derivación).
- `SESSION_SECRET` distinto de `SECRET_KEY`.

## 5. Observabilidad

- **Logs estructurados** (ya hay logger + request id). Considerar formato JSON para ingestión.
- **Métricas** (Prometheus): latencia por endpoint, fallos por servidor destino, jobs en cola.
- ✅ **Health/readiness** (2026-06-12): `/health` = liveness; `/health/ready` = readiness
  que hace `SELECT 1` contra la BD de metadatos y devuelve 503 si no es alcanzable
  (endpoint `def`, corre en threadpool). Falta: métricas Prometheus.
- **Sentry** ya está disponible como dependencia transitiva; evaluar activarlo para errores.

## 6. Reconciliación inventario ↔ realidad

Endpoint/job que compara el inventario del gateway con la introspección real del motor
y reporta *drift* (BDs/usuarios que existen en el motor pero no en el inventario, o viceversa),
sin mutar sin confirmación. Útil tras adopciones manuales o fallos parciales (`status=error`).

`POST /servers/{id}/reconcile` → reporte de diferencias.

## 7. Endurecimiento de seguridad (continuo)

- Rate limiting por ruta en operaciones sensibles (no solo login).
- CSRF tokens para mutaciones si hay frontend en navegador (hoy `same_site=lax`).
- ✅ Confirmación explícita (doble intención) para DROP DATABASE/USER (2026-06-12):
  `drop_remote=true` exige `confirm_name`/`confirm_username` == nombre exacto del objeto,
  validado antes de tocar el motor. Pendiente: `destroy server` (roadmap 3+).
- Revisión de permisos del usuario pseudo-root (mínimo privilegio necesario por operación).

## Orden sugerido de adopción

1. Auditoría + jobs en background → **con la Iteración 2** (las necesitan 02–05).
2. Reconciliación → tras Iteración 2.
3. Observabilidad/health → continuo.
4. OIDC/SSO + secret manager → cuando el despliegue/escala lo pida.
