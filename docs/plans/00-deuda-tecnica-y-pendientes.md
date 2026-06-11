# 00 — Deuda técnica y pendientes de la Iteración 1

**Estado:** Pendiente · **Depende de:** — · **Esfuerzo:** bajo

Cierra los cabos sueltos detectados en la revisión de la Iteración 1 antes de
construir sobre ella. Ninguno bloquea avanzar, pero conviene resolverlos pronto.

## Tareas

### 1. Verificar introspección contra motores reales (ALTA)
El SQL específico de dialecto (`list_databases`, `list_users`) y la semántica de
schema de MySQL/PostgreSQL **no se han ejecutado contra un servidor vivo** (el
entorno de desarrollo no tenía Docker/MySQL/PG). El parsing genérico vía `Inspector`
sí está cubierto por tests sobre SQLite.

- **Acción:** levantar un MySQL/MariaDB y un PostgreSQL reales (Docker o servidores
  de prueba), registrar cada uno, y ejecutar `test-connection`, `/databases`,
  `/users`, `/databases/{db}/tables`, `/.../schema`.
- **Verificación:** estructura correcta por dialecto; **nunca** filas de datos; los
  usuarios/BDs de sistema quedan excluidos.

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

### 4. CORS + cookies de sesión (MEDIA)
`allow_credentials=True` es incompatible con `CORS_ORIGINS="*"` en navegadores.

- **Acción:** documentar/forzar que, con un frontend en navegador, `CORS_ORIGINS`
  liste orígenes específicos (no `*`).

### 5. Endurecimientos menores (BAJA)
- `SESSION_SECRET` idealmente distinto de `SECRET_KEY` (hoy cae a `SECRET_KEY` si está vacío).
- Considerar tokens CSRF para las operaciones mutantes (hoy mitigado con `same_site=lax`).
- Filtros de listado en `GET /servers` (`?engine=&status=&is_active=`) — quedaron diferidos.

## Verificación
- Tests existentes siguen en verde (`uv run pytest`).
- Checklist 1 y 2 ejecutados contra motores reales y documentados.
