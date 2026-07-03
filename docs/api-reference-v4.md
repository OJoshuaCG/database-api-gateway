# API Reference v4 — Revisión y aprobación de baselines de snapshot (R1)

> **Guía para el equipo de frontend.** Addendum de [`api-reference.md`](api-reference.md),
> [`api-reference-v2.md`](api-reference-v2.md) y [`api-reference-v3.md`](api-reference-v3.md).
> Documenta el **flujo de revisión/aprobación** de un *baseline de snapshot* antes de poder
> aplicar migraciones de ese blueprint. No hay endpoints nuevos: el control reutiliza el
> `PATCH` de migraciones ya existente; esta guía explica **cuándo aparece el bloqueo y cómo
> resolverlo** en la UI.
>
> Mismo formato que la v3: **problema → qué debe pasar → escenarios → flujos → endpoint →
> casos de uso → ejemplos → interpretación visual**.
>
> Convenciones (base URL `/api/v1`, envelope `ApiResponse[T]`, auth por cookie, errores,
> paginación) idénticas al documento original ([§3](api-reference.md#3-convenciones-de-la-api)).

**Versión de la API:** `v1` · 🔌 = lee/toca el servidor de BD destino · 🔒 = requiere sesión admin

---

## Índice

- [0. El problema (por qué te pide "aprobar" la primera versión)](#0-el-problema)
- [1. Concepto: el gate `reviewed` (R1)](#1-concepto-el-gate-reviewed-r1)
- [2. Escenario completo (el que dispara el bloqueo)](#2-escenario-completo)
- [3. Flujo de resolución](#3-flujo-de-resolución)
- [4. Endpoints involucrados](#4-endpoints-involucrados)
- [5. Cómo saber QUÉ versión hay que aprobar](#5-cómo-saber-qué-versión-hay-que-aprobar)
- [6. Ejemplos](#6-ejemplos)
- [7. Matriz de errores](#7-matriz-de-errores)
- [8. Interpretación visual sugerida](#8-interpretación-visual-sugerida)
- [9. Recomendaciones de UX](#9-recomendaciones-de-ux)

---

## 0. El problema

Cuando creas un blueprint **desde un snapshot** (`POST /database-models/from-snapshot`), su
migración baseline (`0001`) contiene **DDL capturado del motor real** — estructura que el gateway
no generó y que, por tanto, trata como **potencialmente no confiable** (puede traer vistas,
rutinas o triggers con lógica arbitraria). Por seguridad (control **R1**), ese baseline nace
**sin aprobar** (`reviewed: false`) y el gateway **bloquea cualquier `apply`** de ese blueprint
hasta que un admin lo revise y lo apruebe.

El detalle que confunde: el bloqueo es **a nivel de blueprint**, no de versión. Aunque crees una
**nueva versión** (`0002`, escrita a mano y ya `reviewed: true`) e intentes actualizar la BD a
ella, el `apply` **sigue bloqueado** porque el baseline `0001` —base de todo el esquema— continúa
sin revisar. Por eso te pide aprobar la **primera** versión (la del snapshot), no la nueva.

```
409 Conflict
{ "detail": { "msg": "El blueprint tiene un baseline de snapshot SIN revisar (0001).
   Contiene DDL capturado del motor: revísalo y apruébalo (PATCH reviewed=true en esa
   versión) antes de aplicar." } }
```

## 1. Concepto: el gate `reviewed` (R1)

- Toda migración tiene un campo **`reviewed: bool`**.
- Las migraciones **escritas a mano** (`POST .../migrations`) nacen `reviewed: true` (las escribió
  el admin → ya están "revisadas").
- El **baseline de snapshot** (`is_baseline: true`, creado por `from-snapshot`) nace
  `reviewed: false`.
- `POST .../migrations/apply` y `.../apply-all` **rechazan con `409`** si el blueprint tiene
  **algún** baseline con `reviewed: false`. `stamp` **no** se bloquea (no ejecuta SQL).
- Aprobar es **una sola vez por baseline**: una vez `reviewed: true`, el blueprint queda
  desbloqueado para siempre.

## 2. Escenario completo

```
1) POST /database-models/from-snapshot          → blueprint + baseline 0001 (reviewed=false)
2) POST /managed-databases/adopt                 → adoptas la BD (origin=adopted)
3) PATCH /managed-databases/{id} {model_id}       → (si hace falta) vinculas la BD al blueprint
4) POST .../migrations  {name, up_sql}            → creas la versión 0002 (reviewed=true)
5) POST .../{db_id}/migrations/apply              → ❌ 409: "baseline 0001 sin revisar"
   ─────────────────────────────────────────────────────────────────────────────
6) GET  .../migrations/0001                        → revisas el DDL capturado
7) PATCH .../migrations/0001  {"reviewed": true}   → ✅ apruebas el baseline
8) POST .../{db_id}/migrations/apply               → ✅ ahora aplica (0001 + 0002…)
```

> **⚠ Nota clave para BDs ADOPTADAS (tu caso).** Si la BD se **adoptó** de la misma estructura
> que se fotografió, esa BD **ya tiene** el esquema del baseline `0001`. NO hagas `apply` de `0001`
> sobre ella (intentaría `CREATE TABLE` de tablas que ya existen → fallaría). El camino correcto:
> **`stamp` la BD en `0001`** (marca la versión SIN ejecutar SQL — *no* está bloqueado por el gate
> `reviewed`) y luego `apply` para las versiones nuevas (`0002`…). Aun así, para poder aplicar
> `0002` debes **aprobar** el baseline `0001` una vez (el gate es a nivel de blueprint). Orden
> recomendado: `PATCH 0001 {reviewed:true}` → `stamp ?version=0001` → `apply` (aplica `0002`+).

## 3. Flujo de resolución

```
[Intento de aplicar] ──▶ 409 "baseline 0001 sin revisar"
        │
        ▼
[Abrir la versión 0001]  GET .../migrations/0001   → mostrar up_sql / translated
        │  (el admin revisa el DDL: tablas, vistas, triggers…)
        ▼
[Aprobar]  PATCH .../migrations/0001 {"reviewed": true}
        │
        ▼
[Reintentar apply]  POST .../{db_id}/migrations/apply  → 200 (aplica secuencialmente)
```

## 4. Endpoints involucrados

> Todos 🔒. **GW** = solo inventario (no toca el motor).

| Método | Ruta | Rol en este flujo |
|---|---|---|
| `POST` | `/api/v1/managed-databases/{db_id}/migrations/apply` 🔌 | Dispara el `409` si el baseline no está aprobado. |
| `GET` | `/api/v1/database-models/{model_id}/migrations/{version}` (GW) | Trae el `up_sql`/`translated` del baseline para **revisarlo**. |
| `PATCH` | `/api/v1/database-models/{model_id}/migrations/{version}` (GW) | **Aprueba** el baseline: body `{ "reviewed": true }`. |
| `GET` | `/api/v1/database-models/{model_id}/migrations` (GW) | Lista las versiones con `is_baseline`/`reviewed` para saber cuál aprobar. |

### `PATCH /api/v1/database-models/{model_id}/migrations/{version}` 🔒 (GW)

Aprueba el baseline (o confirma `down_sql`/añade overrides). **Body** (`ModelMigrationPatch`):

| Campo | Tipo | Detalle |
|---|---|---|
| `reviewed` | bool | `true` aprueba el baseline → habilita el `apply` del blueprint |
| `name` | string \| null | (opcional) renombrar |
| `down_sql` | string \| null | (opcional) confirmar rollback |
| `up_sql_mysql` / `up_sql_postgresql` | string \| null | (opcional) overrides — **bloqueado `409`** si ya se aplicó en alguna BD |

`reviewed`, `name` y `down_sql` se pueden ajustar **aunque** la migración ya esté aplicada; el
**SQL efectivo** no.

## 5. Cómo saber QUÉ versión hay que aprobar

No hace falta adivinar — hay dos fuentes:

1. **El propio error `409`** nombra la(s) versión(es) sin revisar (`unreviewed_baseline` en el
   `context` cuando `APP_ENV=development`; y el `msg` las lista).
2. **El listado/detalle** exponen los flags: cada item de `GET .../migrations` trae
   `is_baseline` y `reviewed`. Filtra por `is_baseline == true && reviewed == false` → esa es la
   que bloquea.

## 6. Ejemplos

**1) El `apply` bloqueado (la señal de que hay que aprobar):**
```bash
curl -X POST "https://<host>/api/v1/managed-databases/11/migrations/apply" -b cookies.txt
```
```json
{ "detail": { "msg": "El blueprint tiene un baseline de snapshot SIN revisar (0001). Contiene DDL capturado del motor: revísalo y apruébalo (PATCH reviewed=true en esa versión) antes de aplicar.",
              "type": "AppHttpException",
              "context": { "model_id": 2, "unreviewed_baseline": ["0001"] } } }
```

**2) Identificar la versión a aprobar (listado con flags):**
```bash
curl "https://<host>/api/v1/database-models/2/migrations?page=1&size=50" -b cookies.txt
```
```json
{ "data": [
    { "version": "0001", "name": "Snapshot baseline", "is_baseline": true,  "reviewed": false, "has_rollback": false },
    { "version": "0002", "name": "Add índice",        "is_baseline": false, "reviewed": true,  "has_rollback": true }
  ], "pagination": { "total": 2 } }
```

**3) Revisar el DDL del baseline ANTES de aprobar:**
```bash
curl "https://<host>/api/v1/database-models/2/migrations/0001" -b cookies.txt
# → { "version": "0001", "is_baseline": true, "reviewed": false, "source_engine": "mysql",
#     "has_non_portable": true, "up_sql": "CREATE TABLE `campaigns` (...); CREATE TRIGGER ...",
#     "translated": { "mysql": "…" } }
```

**4) Aprobar el baseline:**
```bash
curl -X PATCH "https://<host>/api/v1/database-models/2/migrations/0001" -b cookies.txt \
  -H "Content-Type: application/json" -d '{ "reviewed": true }'
```
```json
{ "data": { "version": "0001", "is_baseline": true, "reviewed": true }, "message": "Migración actualizada." }
```

**5) Reintentar el `apply` (ya desbloqueado):**
```bash
curl -X POST "https://<host>/api/v1/managed-databases/11/migrations/apply" -b cookies.txt
```
```json
{ "data": { "from_version": null, "to_version": "0002", "applied_count": 2, "no_op": false,
            "pending_versions": ["0001","0002"] },
  "message": "Aplicadas 2 migración(es): ∅ → 0002." }
```

## 7. Matriz de errores

| Situación | Código | Qué mostrar |
|---|---|---|
| `apply`/`apply-all` con baseline sin revisar | `409` | Banner "baseline pendiente de aprobación" + CTA "Revisar y aprobar" |
| Aplicar un baseline no portable a **otro motor** que el de origen | `422` | "Este blueprint (snapshot de mysql) no es aplicable a postgresql." |
| Intentar cambiar el **SQL** de una versión ya aplicada | `409` | "No se puede editar el SQL ya aplicado; crea una versión nueva." |
| `version` objetivo inexistente en `apply` | `422` | "Esa versión no existe en el blueprint." |
| Sin sesión | `401` | Redirigir a login |

> El `stamp` **no** se bloquea por el gate `reviewed` (no ejecuta SQL): sirve para *marcar* que
> una BD ya está en una versión sin ejecutarla.

## 8. Interpretación visual sugerida

En la pantalla del blueprint / al intentar aplicar:

```
┌─ Blueprint: CRM Legacy (snapshot de mysql) ───────────────────────────┐
│  ⚠ Baseline 0001 PENDIENTE DE REVISIÓN — el apply está bloqueado       │
│                                                                        │
│  Versión   Tipo         Estado            Acción                       │
│  0001      📸 baseline  🟠 sin revisar    [ Revisar y aprobar ]        │
│  0002      🔧 manual    🟢 revisada                                    │
│                                                                        │
│  [ Aplicar a la BD ]  ← deshabilitado mientras 0001 no esté aprobada   │
└────────────────────────────────────────────────────────────────────────┘
        │  click "Revisar y aprobar"
        ▼
┌─ Revisar baseline 0001 ────────────────────────────────────────────────┐
│  ⚠ DDL capturado del motor (mysql) — revisa antes de aprobar.          │
│  [ pestaña SQL ]  CREATE TABLE `campaigns` ( … );                       │
│                   CREATE TRIGGER … BEGIN … END;                         │
│  ⚠ Contiene objetos no portables (triggers): atado a MySQL.            │
│                              [ Cancelar ]   [ Aprobar (reviewed=true) ] │
└────────────────────────────────────────────────────────────────────────┘
```

- Resalta el baseline `is_baseline && !reviewed` con un badge **🟠 "sin revisar"** y deshabilita
  el botón **"Aplicar"** del blueprint/BD (con tooltip que explica el `409`).
- El botón **"Revisar y aprobar"** abre el detalle (`GET .../{version}`) mostrando el `up_sql`
  (y `translated` por motor); al confirmar, hace el `PATCH {reviewed:true}` y reintenta el apply.
- Si `has_non_portable: true`, muestra el aviso de que el baseline queda **atado al motor de
  origen** (`source_engine`) — no aplicable cross-engine.
- Tras aprobar, el badge pasa a **🟢 "revisada"** y el botón "Aplicar" se habilita.

## 9. Recomendaciones de UX

1. **Aprobación = acto deliberado.** No auto-aprobar: el sentido del gate es que un humano mire el
   DDL capturado antes de propagarlo a N bases de datos.
2. **Una vez por baseline.** Tras aprobar `0001`, no se vuelve a pedir; futuras versiones
   (`0002`, `0003`…) ya nacen revisadas y aplican sin fricción.
3. **Muestra el porqué.** El `409` no es un error del usuario: explica que es un control de
   seguridad sobre DDL no confiable, con el botón de acción a mano.
4. **Revisa, no solo apruebes.** Presenta el SQL (idealmente con resaltado y separando
   tablas/vistas/triggers) para que la aprobación sea informada — sobre todo si
   `has_non_portable: true`.

---

> **Resumen:** el endpoint para aprobar **ya existe** (`PATCH .../migrations/{version}` con
> `{"reviewed": true}`). El bloqueo que viste es intencional (control R1): un baseline de snapshot
> es DDL no confiable y debe aprobarse una vez antes de que el blueprint pueda aplicarse —
> incluido el camino para llegar a una versión nueva. Detalle del módulo de adopción/snapshot en
> [`api-reference-v3.md`](api-reference-v3.md).
