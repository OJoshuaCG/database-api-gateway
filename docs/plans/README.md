# Roadmap — Sistema gestor de bases de datos (Gateway)

Planes a futuro del proyecto. Cada documento es autocontenido: contexto, alcance,
modelo de datos/endpoints, decisiones, riesgos, pasos y verificación.

> Estado a **2026-06-11**: **Iteración 1 completada** (infra, cifrado, capa de
> conexión remota multi-motor, adaptadores, modelo `Server`, auth de sesión + admin,
> API de servers + introspección). Suite de 85 tests en verde. Ver `CLAUDE.md` y el
> plan original en `~/.claude/plans/`.

## Estado actual (Iteración 1 — hecho)

- `app/core/crypto.py` — cifrado Fernet de credenciales (derivado de `SECRET_KEY`).
- `app/core/remote_engine.py` — conexión dinámica por servidor (NullPool, cache, timeouts, mapeo de errores).
- `app/services/db_admin/` — `ServerAdapter` + `MySQLAdapter`/`PostgresAdapter` + `identifiers` (anti-inyección). **Los métodos de escritura (create/drop user/db, grants) YA están implementados**, sin endpoint todavía.
- `app/models/server.py` + migración Alembic + auth (`app/core/auth.py`) + API (`/api/v1/servers`, `/auth`).

## Orden recomendado

| # | Plan | Depende de | Estado |
|---|------|-----------|--------|
| 00 | [Deuda técnica y pendientes de Iteración 1](00-deuda-tecnica-y-pendientes.md) | — | Pendiente |
| 01 | [Iteración 2 — Inventario completo y aprovisionamiento de usuarios/BDs](01-iteracion-2-inventario-y-aprovisionamiento.md) | 00 (parcial) | Pendiente |
| 02 | [Migraciones de modelos (blueprints versionados)](02-migraciones-de-modelos.md) | 01 | Pendiente |
| 03 | [Aprovisionamiento de servidores (API/Terraform)](03-aprovisionamiento-servidores.md) | 01 | Pendiente |
| 04 | [Instalación de motor vía SSH](04-instalacion-motor-ssh.md) | 03 | Pendiente |
| 05 | [Clonado de bases de datos entre servidores](05-clonado-de-bases-de-datos.md) | 01, 02 | Pendiente |
| 06 | [Operación: seguridad, auditoría y observabilidad](06-operacion-seguridad-observabilidad.md) | transversal | Continuo |

## Diagrama de dependencias

```
00 (deuda técnica)
      │
      ▼
01 (inventario: ServerUser, DatabaseModel, ManagedDatabase + crear users/BDs/grants)
      ├──────────────► 02 (migraciones de modelos)
      ├──────────────► 03 (aprovisionar servidores) ──► 04 (instalar motor por SSH)
      └──────────────► 05 (clonado de BDs)   ◄── también usa 02

06 (auditoría, SSO, jobs, observabilidad) — transversal a todo
```

## Principios que todo plan respeta

- Formato `ApiResponse[T]` + helpers; errores con `AppHttpException`.
- Toda operación que toca un motor pasa por un `ServerAdapter` (nunca SQL crudo desde el controller).
- Identificadores SQL siempre validados/quoteados; valores parametrizados o escapados.
- Credenciales cifradas en reposo; nunca en respuestas, logs ni contexto de error.
- Endpoints con I/O bloqueante o remoto se declaran `def` (no `async def`) para no bloquear el event loop.
- Cada operación destructiva o de cara al exterior debe quedar auditada (ver plan 06).
