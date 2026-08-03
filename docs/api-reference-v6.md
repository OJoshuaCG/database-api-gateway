# API Reference v6 — Consola SQL: ejecutar SQL ad-hoc con el usuario del motor que elijas

> **Guía para el equipo de frontend.** Addendum de [`api-reference.md`](api-reference.md),
> [`api-reference-v2.md`](api-reference-v2.md), [`api-reference-v3.md`](api-reference-v3.md),
> [`api-reference-v4.md`](api-reference-v4.md) y [`api-reference-v5.md`](api-reference-v5.md).
>
> A diferencia de v5 —que corregía supuestos de pantallas ya construidas—, **este documento
> describe un módulo NUEVO que nunca fue expuesto al frontend**: no hay pantalla existente
> que ajustar, hay una que diseñar desde cero. El backend ya está implementado y commiteado.
>
> Mismo formato que v3/v4/v5: **problema → qué debe pasar → escenarios → flujos → endpoints →
> ejemplos → interpretación visual**.
>
> Convenciones (base URL `/api/v1`, envelope `ApiResponse[T]`, auth por cookie, errores,
> paginación) idénticas al documento original ([§3](api-reference.md#3-convenciones-de-la-api)).
>
> Documentación de ingeniería del mismo módulo (más detalle interno del que el frontend
> necesita): [`docs/features/sql-query-console.md`](features/sql-query-console.md).

**Versión de la API:** `v1` · 🔌 = lee/toca el servidor de BD destino · 🔒 = requiere sesión admin

---

## Índice

- [0. La necesidad: por qué existe este módulo](#0-la-necesidad-por-qué-existe-este-módulo)
- [1. Alcance: qué cubre y qué NO cubre](#1-alcance-qué-cubre-y-qué-no-cubre)
- [2. Limitaciones (leer ANTES de diseñar la pantalla)](#2-limitaciones-leer-antes-de-diseñar-la-pantalla)
- [3. Los cuatro niveles de peligro (`danger`)](#3-los-cuatro-niveles-de-peligro-danger)
- [4. Modos de conexión: elegir CON QUÉ USUARIO se ejecuta](#4-modos-de-conexión-elegir-con-qué-usuario-se-ejecuta)
- [5. `POST /servers/{id}/query/preview`](#5-post-serversidquerypreview-)
- [6. `POST /servers/{id}/query/execute`](#6-post-serversidqueryexecute-)
- [7. `GET /servers/{id}/query/history`](#7-get-serversidqueryhistory-)
- [8. Tabla completa de códigos de motivo (`reasons[].code`)](#8-tabla-completa-de-códigos-de-motivo-reasonscode)
- [9. Manejo de errores: "el motor dijo que no" ≠ "la API falló"](#9-manejo-de-errores-el-motor-dijo-que-no--la-api-falló)
- [10. Flujos completos, paso a paso](#10-flujos-completos-paso-a-paso)
  - [10.1 Verificar que un usuario de solo lectura NO puede borrar](#101-verificar-que-un-usuario-de-solo-lectura-no-puede-borrar)
  - [10.2 `UPDATE` masivo: ver cuántas filas antes de confirmar](#102-update-masivo-ver-cuántas-filas-antes-de-confirmar)
  - [10.3 Intento de `GRANT`: la UI lo impide antes de llegar al backend](#103-intento-de-grant-la-ui-lo-impide-antes-de-llegar-al-backend)
  - [10.4 Lote mixto: una sentencia falla y las siguientes no corren](#104-lote-mixto-una-sentencia-falla-y-las-siguientes-no-corren)
  - [10.5 Atajo: consultar permisos SIN confirmación](#105-atajo-consultar-permisos-sin-confirmación)
- [11. Interpretación visual: pantallas, estados y transiciones](#11-interpretación-visual-pantallas-estados-y-transiciones)
- [12. Tipos (referencia rápida)](#12-tipos-referencia-rápida)
- [13. Matriz de errores](#13-matriz-de-errores)
- [14. Checklist de implementación](#14-checklist-de-implementación)

---

## 0. La necesidad: por qué existe este módulo

El gateway ya sabe **otorgar** permisos: hay endpoints de `GRANT`/`REVOKE`, perfiles de
permisos, provisioning de usuarios y bases. Lo que no había hasta ahora es forma de
**verificar en la práctica** que un permiso quedó como se esperaba.

Hoy, cuando el admin le otorga `SELECT` sobre `tienda` al usuario `app_ro` y quiere
comprobar que efectivamente puede leer `pedidos` pero **no** puede borrar nada, tiene que:

1. Salir del gateway.
2. Abrir un cliente SQL propio (o entrar por SSH al servidor).
3. Conseguir la contraseña de ese usuario por fuera.
4. Conectarse a mano y probar.

Es decir: el gateway administra los permisos pero no puede *demostrar* su efecto. La
Consola SQL cierra ese circuito sin salir de la aplicación:

> El admin elige **servidor → base de datos → usuario del motor**, escribe SQL, ejecuta, y ve
> el resultado **real** del motor — incluido el rechazo por falta de permisos, que es
> justamente lo que se quiere comprobar.

La pieza clave, y lo que diferencia esto de "una consola SQL más": **con qué usuario se
ejecuta es parte del request**, no una configuración del sistema. Sin eso, todo correría con
la credencial pseudo-root del gateway y daría verde siempre.

### Por qué tiene barandas tan fuertes

El gateway se conecta a cada servidor destino con una credencial **pseudo-root**. Eso
significa que el motor *sí permitiría* cosas que ninguna consola de administración debería
ofrecer: leer archivos del host, cargar librerías nativas, apagar el servidor, alterar la
tabla de usuarios del motor. Por eso el módulo no es un pasamanos de SQL: clasifica cada
sentencia, exige confirmación de doble factor para todo lo que no sea lectura, y mantiene una
lista de operaciones **prohibidas incluso confirmando**.

**Actor:** el admin único del gateway (sesión admin por cookie). No hay roles ni multi-tenant
en este módulo.

---

## 1. Alcance: qué cubre y qué NO cubre

### Cubre

- Ejecutar SQL arbitrario contra **cualquier base de datos** de **cualquier servidor del
  inventario** — la base no necesita estar adoptada/gestionada por el gateway.
- Elegir la identidad del motor con la que se ejecuta: pseudo-root, un usuario del inventario
  con contraseña guardada, un usuario cualquiera con contraseña provista en el request, o un
  rol de PostgreSQL adoptado con `SET ROLE`.
- Clasificación de peligrosidad **por sentencia** y por lote, con motivos legibles.
- Estimación de impacto (`SELECT COUNT(*)`) antes de confirmar un `UPDATE`/`DELETE`.
- Confirmación de doble factor (nombre de la base + token firmado con TTL) para todo lo que
  no sea lectura pura.
- `dry_run` (ejecutar y revertir) para ver el efecto real sin persistir.
- Historial paginado de ejecuciones (metadatos, no datos).

### NO cubre

- **No reemplaza la gestión de permisos del gateway.** `GRANT`/`REVOKE` están *bloqueados*
  desde acá a propósito: existen endpoints dedicados con guards anti-lockout y auditoría
  estructurada. Esta consola es para **verificar** el resultado de esos endpoints, no para
  sustituirlos.
- **No reemplaza el ciclo de vida de bases de datos.** `CREATE/DROP DATABASE` también están
  bloqueados: hay endpoints propios con confirmación por nombre + token y guard de BDs de
  sistema.
- **No reemplaza la gestión de usuarios del motor.** `CREATE/ALTER/DROP USER|ROLE`,
  `SET PASSWORD` y `RENAME USER` están bloqueados por el mismo motivo.
- **No es un explorador de esquema.** No hay listado de tablas/columnas en esta API; para eso
  ya existen otras pantallas del gateway (introspección de servidores y bases).
- **No es un cliente SQL de trabajo diario.** No hay autocompletado, ni historial de sesión
  local, ni formateo, ni resultados persistidos.
- **No es para usuarios finales.** Es administración interna: requiere sesión admin del
  gateway.

---

## 2. Limitaciones (leer ANTES de diseñar la pantalla)

Estas no son "detalles a pulir después": condicionan qué pantallas y qué avisos hacen falta.

### 2.1 No hay segundo factor todavía

Hoy **cualquier** sentencia `write`/`ddl` se puede confirmar tipeando el nombre de la base de
datos. No hay 2FA real, ni OTP, ni re-autenticación. La única baranda dura es que el nivel
`blocked` es **innegociable**: no existe forma de confirmarlo desde la API.

El contrato está diseñado para que el día que exista un segundo factor se pueda enchufar sin
romper nada (el `confirm_token` ya viaja como campo opaco). **Implicación para la UI:** la
confirmación por nombre no es un trámite — es *la* protección. No la hagas fácil de saltar
(nada de recordar el nombre tipeado, ni de pre-rellenarlo, ni de habilitar el botón con el
campo vacío).

### 2.2 No es un cliente SQL completo

Sin autocompletado de tablas/columnas, sin explorador de esquema integrado en esta API. Si la
pantalla necesita mostrar qué tablas hay, tiene que consumir los endpoints de introspección ya
existentes del gateway ([§6 del doc original](api-reference.md#6-servidores-servers)).

### 2.3 Topes de filas, celdas, tamaño de SQL y timeout

Todos configurables **a nivel de despliegue**, no por request (salvo `max_rows`/`timeout_ms`,
y solo dentro de los topes globales):

| Tope | Default | Qué acota | ¿El request puede cambiarlo? |
|---|---|---|---|
| `QUERY_MAX_ROWS` | `1000` | Filas devueltas por sentencia | `max_rows` puede **bajarlo**, nunca subirlo |
| `QUERY_TIMEOUT_MS` | `30000` | Timeout por sentencia | `timeout_ms` puede subirlo hasta el techo |
| `QUERY_MAX_TIMEOUT_MS` | `300000` | Techo de `timeout_ms` | No |
| `QUERY_MAX_SQL_BYTES` | `262144` | Tamaño del SQL enviado | No → 422 si se excede |
| `QUERY_MAX_CELL_CHARS` | `4096` | Tamaño de cada celda devuelta | No |
| `QUERY_HISTORY_SQL_MAX_CHARS` | `16384` | SQL persistido en el historial | No |

Cuando se llega al tope de filas, la sentencia vuelve con `truncated: true`. **La UI debe
mostrarlo explícitamente** ("mostrando las primeras 1000 filas; hay más"), porque el usuario
podría sacar conclusiones equivocadas de un resultado recortado en silencio.

### 2.4 El historial NO guarda datos, solo metadatos

`GET .../query/history` devuelve quién ejecutó qué, cuándo, con qué usuario, cuántas filas
devolvió/afectó y cuánto tardó — **pero no las filas**. El gateway no es custodio de los datos
del usuario final.

**Implicación:** no se puede construir una pantalla de "ver el resultado de la ejecución del
martes". Lo que sí se puede es **re-cargar el `sql_text` en el editor y volver a ejecutar**.
Diseñá el historial como bitácora + atajo de re-ejecución, nunca como caché de resultados.

### 2.5 `dry_run` no es confiable para DDL en MySQL/MariaDB

`dry_run: true` ejecuta el lote dentro de una transacción y hace `ROLLBACK` al final. Eso
funciona para DML (`INSERT`/`UPDATE`/`DELETE`) en los tres motores, y para DDL **solo en
PostgreSQL** (que tiene DDL transaccional).

En MySQL/MariaDB cada sentencia DDL hace **COMMIT implícito** apenas se ejecuta: el `ROLLBACK`
posterior no la deshace. La respuesta lo informa con `ddl_persisted: true` (junto a
`rolled_back: true`, que por sí solo sería engañoso) y con un `warning` explícito.

**Implicación para la UI:** si el usuario tiene `dry_run` activado y el preview clasificó el
lote como `ddl` en un servidor MySQL/MariaDB, mostrar una advertencia **antes** de ejecutar,
no después. Y si la respuesta trae `ddl_persisted: true`, ese es el dato más importante de
toda la pantalla de resultados.

### 2.6 `impersonate` no existe en MySQL/MariaDB

Es una limitación del motor, no del gateway: en MySQL/MariaDB un usuario solo puede adoptar
roles que ya le fueron otorgados, así que no hay forma de "convertirse" en una identidad
arbitraria sin su contraseña. PostgreSQL sí lo permite con `SET ROLE`.

**Implicación:** la opción debe **ocultarse o deshabilitarse** cuando el servidor elegido es
MySQL/MariaDB. Si se envía igual, el backend responde `422`.

### 2.7 Es administración interna

Requiere sesión de admin del gateway. No está pensada para exponerse a usuarios finales de
las aplicaciones que consumen las BDs gestionadas.

### 2.8 Estado del backend: falta la validación contra motores reales

El módulo fue implementado y sometido a una revisión de seguridad exhaustiva (que encontró y
corrigió 4 bloqueantes), pero **la corrida e2e contra MySQL/MariaDB/PostgreSQL reales todavía
no se ejecutó**. Es una nota de estado del backend, no algo que la UI deba comunicar al
usuario final — pero es relevante para el equipo de frontend: **el contrato puede tener
ajustes menores hasta esa validación**. Conviene aislar el mapeo de la respuesta en un solo
lugar del código de UI.

---

## 3. Los cuatro niveles de peligro (`danger`)

La política clasifica **cada sentencia** del lote, y el `danger` del lote es el **máximo** de
sus sentencias. Una confirmación cubre el lote entero, nunca sentencia por sentencia.

| Nivel | Qué incluye | Qué debe hacer la UI |
|---|---|---|
| `read` | `SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN` (sin `ANALYZE`) | Ejecutar **directo** al tocar el botón. Sin preview obligatorio, sin diálogo, sin token. |
| `write` | `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `SELECT … FOR UPDATE`, `SET` de sesión | Diálogo de confirmación con **tipeo del nombre de la base** antes de habilitar el botón de ejecutar. |
| `ddl` | `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `RENAME`, `ANALYZE`, **y todo lo que el backend no supo reconocer con certeza** | Mismo flujo de confirmación que `write`. Redacción del aviso distinta (afecta estructura, no filas). |
| `blocked` | Ver [§8](#8-tabla-completa-de-códigos-de-motivo-reasonscode) | **No ofrecer confirmación.** Mostrar el motivo como "esto no se puede hacer desde acá", no como error transitorio. |

### Tres cosas que suelen sorprender

1. **`write` pide confirmación tenga o no `WHERE`.** Un `UPDATE t SET x=1 WHERE id=5` y un
   `UPDATE t SET x=1` piden exactamente lo mismo. No hay atajo para el caso "chiquito".
2. **Lo desconocido cae en `ddl`, no en `read`.** Si sqlglot no puede parsear la sentencia, si
   el tipo no está mapeado, o si es opaca (`CALL sp_algo()`, `DO`, bloque anónimo), el
   resultado es `ddl` por política *fail-closed* — **no porque sea necesariamente destructiva**.
   El `reasons[]` lo explica (`opaque_statement`, `unparseable`, `unmapped_statement`) y la UI
   debería reflejar ese matiz: *"no se pudo determinar qué hace esta sentencia, así que se
   trata como peligrosa"*, no *"esta sentencia destruye datos"*.
3. **`read` no se confía al parser.** Todo lo clasificado como `read` se ejecuta dentro de una
   transacción de **solo lectura** real del motor. Si la clasificación se equivocó, el motor
   aborta la sentencia y la respuesta llega con `policy_miss: true` (ver [§6](#6-post-serversidqueryexecute-)).

---

## 4. Modos de conexión: elegir CON QUÉ USUARIO se ejecuta

`connection.mode` es parte del request porque **elegir el usuario *es* la funcionalidad**.

```jsonc
// QueryConnectionIn
{
  "mode": "admin" | "stored" | "provided" | "impersonate",  // default: "admin"
  "username": "app_ro",          // stored | provided
  "host": "%",                   // stored, solo MySQL/MariaDB
  "password": "…",               // provided (NUNCA se persiste)
  "role": "reportes_ro"          // impersonate (solo PostgreSQL)
}
```

| Modo | Cómo conecta | Requiere | Cuándo usarlo |
|---|---|---|---|
| `admin` (default) | Credencial **pseudo-root** del servidor | nada | Operar, **no** probar permisos |
| `stored` | Usuario del inventario cuya contraseña fijó el gateway (cifrada) | `username` (+ `host` en MySQL/MariaDB) | El gateway creó o rotó esa contraseña |
| `provided` | Contraseña enviada en este request | `username` + `password` | Un usuario que el gateway no administra (el caso más común) |
| `impersonate` | Conecta como pseudo-root y emite `SET ROLE` | `role` | **Solo PostgreSQL**: probar un rol sin conocer su contraseña |

### Reglas por modo

**`admin`** — La UI **debe marcarlo visualmente como el modo más peligroso**: banner rojo
persistente mientras esté seleccionado. La respuesta trae siempre este `warning`:

> *"Se ejecutará con la credencial pseudo-root del servidor: los permisos NO se están
> probando, se están evitando. Elegí un usuario concreto para verificar permisos."*

Es el default del schema, pero **no debería ser el default de la pantalla**: si el propósito
del módulo es verificar permisos, arrancar en `admin` invita al error exacto que el módulo
existe para evitar. Sugerencia: que el selector arranque vacío y obligue a elegir.

**`stored`** — El `host` importa: en MySQL/MariaDB `'app'@'localhost'` y `'app'@'%'` son
cuentas **separadas**, con contraseñas y privilegios distintos. Si no se envía, el backend
asume `%`. En PostgreSQL el `host` se ignora (los roles no tienen host).
- Usuario no encontrado en el inventario → **404** (mensaje sugiere usar `provided`).
- Usuario en el inventario pero sin contraseña guardada (fue *adoptado*, no creado por el
  gateway) → **409** (mensaje sugiere usar `provided`). El motor solo guarda un hash
  irreversible: una contraseña que el gateway nunca fijó no se puede recuperar.

**`provided`** — La contraseña **nunca** se persiste: ni en el historial, ni en la auditoría,
ni en logs. Sin `password` → **422**.

**`impersonate`** — Solo PostgreSQL; en MySQL/MariaDB → **422** siempre. Sin `role` → **422**.
Trae su propio `warning`:

> *"SET ROLE reproduce los permisos del rol para esta sesión, pero es una herramienta de
> prueba, no una frontera de seguridad."*

### ⚠️ La identidad forma parte del token de confirmación

El `confirm_token` del preview se ata a `(hash del SQL, modo, usuario, rol, host)`. Si el
usuario cambia **cualquiera** de esos datos entre el preview y el execute —incluido cambiar de
`provided` a `stored` con el mismo username—, el token deja de ser válido y el backend
responde **422**. La UI debe **invalidar el token guardado** ante cualquier edición del SQL o
del selector de conexión, y volver a pedir el preview.

---

## 5. `POST /servers/{id}/query/preview` 🔌🔒

Clasifica el SQL, estima el impacto y emite el `confirm_token`. **No ejecuta el SQL del
usuario.**

> 🔌 **Toca el motor solo si hay algo que estimar.** Con `estimate_impact: true` (default) y al
> menos un `UPDATE`/`DELETE` de una sola tabla, el backend abre una conexión con la credencial
> elegida y corre los `SELECT COUNT(*)` derivados. En cualquier otro caso el preview es
> puramente local (clasificación estática) y no abre conexión.

**Rate limit:** `30/minute`.

### Request

```jsonc
{
  "database": "tienda",                    // requerido, 1..128 chars
  "sql": "UPDATE pedidos SET estado = 'cancelado' WHERE creado_en < '2025-01-01'",
  "connection": {                          // opcional; default { "mode": "admin" }
    "mode": "provided",
    "username": "app_rw",
    "password": "…"
  },
  "estimate_impact": true                  // default true
}
```

`sql` puede ser un **lote** de varias sentencias separadas por `;`. El backend las separa,
clasifica cada una y devuelve una entrada por sentencia.

### Response — `data: QueryPreviewOut`

```jsonc
{
  "server_id": 2,
  "database": "tienda",
  "engine": "mysql",                       // mysql | mariadb | postgresql
  "run_as": "app_rw",                      // usuario del motor con el que se ejecutaría
  "connection_mode": "provided",
  "danger": "write",                       // read | write | ddl | blocked (máximo del lote)
  "requires_confirmation": true,
  "blocked": false,                        // true → el execute devolverá 403 seguro
  "statements": [
    {
      "seq": 0,
      "sql": "UPDATE pedidos SET estado = 'cancelado' WHERE creado_en < '2025-01-01'",
      "kind": "update",
      "danger": "write",
      "reasons": [],
      "estimated_rows": 2481902
    }
  ],
  "reasons": [],                           // motivos agregados del LOTE
  "warnings": [],
  "confirm_token": "1754131200.9f3c…",     // null si no hace falta confirmar, o si blocked
  "expires_at": "2026-08-02T12:02:00Z"     // TTL 2 minutos
}
```

### Campos que necesitan explicación

| Campo | Nota |
|---|---|
| `statements[].sql` | Aquí es la sentencia **tal como quedó al separar el lote** (sin el `;`). En el `execute` este mismo campo puede diferir: a un `SELECT` se le empuja un `LIMIT` al motor y ahí se informa el SQL realmente ejecutado. |
| `statements[].kind` | `select`, `insert`, `update`, `delete`, `merge`, `create`, `alter`, `drop`, `truncate`, `analyze`, `show`, `describe`, `set`, `copy`, `use`, `kill`, `unknown`, `blocked`, `empty`. Útil para iconografía; **el que manda para las decisiones de UI es `danger`**. |
| `statements[].estimated_rows` | Filas que afectaría un `UPDATE`/`DELETE`, contadas con **la misma credencial** que ejecutaría la sentencia. Solo se calcula para `danger: "write"` de **una sola tabla**. |
| `estimated_rows: null` | **No relaja nada.** Significa "no hay cifra exacta que mostrar", no "no afecta filas". Motivos típicos: el `WHERE` cruza varias tablas (`JOIN`, `USING`, `FROM` de PostgreSQL), o el usuario elegido no tiene permiso de leer esa tabla. La confirmación se exige igual. |
| `confirm_token` | Presente solo si hay algo que confirmar y el lote no está bloqueado. Se reenvía **tal cual** al execute. |
| `expires_at` | 2 minutos. Pasado eso, el execute responde **410** y hay que repetir el preview. |
| `blocked` | Si es `true`, `confirm_token` es `null` y el execute responderá **403**. La UI no debe ofrecer el camino de confirmación. |

### 🚨 `blocked` MANDA sobre `requires_confirmation`

**Un lote bloqueado vuelve con `blocked: true` Y `requires_confirmation: true` a la vez.** No es
un error del contrato: el nivel `blocked` es el más severo de la escala, así que "requiere
confirmación" también se cumple formalmente. Pero **no hay nada que confirmar**:
`confirm_token` viene en `null` y el execute responde 403 sin tocar el motor.

```jsonc
// GRANT — así vuelve realmente el preview
{ "danger": "blocked", "blocked": true, "requires_confirmation": true, "confirm_token": null }
```

**Regla de decisión para la UI, en este orden exacto:**

```
if (blocked)                    → camino "prohibido": sin botón de confirmar, mostrar reasons
else if (requires_confirmation) → camino "confirmar": diálogo + confirm_token
else                            → camino "directo": ejecutar
```

Chequear `requires_confirmation` primero llevaría a abrir un diálogo de confirmación con un
token nulo, que termina en un 403 después de hacerle tipear el nombre de la base al usuario.

### ⚠️ `requires_confirmation` vs. `confirm_token`

`requires_confirmation` refleja además el flag de despliegue `QUERY_SAFE_MODE`. En un
despliegue con el modo seguro **apagado**, un lote `write` puede volver con
`requires_confirmation: false` pero **con `confirm_token` presente** (el token se emite igual).

**Regla para la UI:** decidir el flujo de confirmación por `requires_confirmation`, y enviar
`confirm_token`/`confirm_target_name` **siempre que el preview los haya producido**. Mandar
una confirmación que no hacía falta es inofensivo; omitirla cuando hacía falta es un 422.

### `warnings[]` — strings listos para mostrar

Textos exactos que puede devolver el preview:

- Modo `admin`: *"Se ejecutará con la credencial pseudo-root del servidor: los permisos NO se
  están probando, se están evitando. Elegí un usuario concreto para verificar permisos."*
- Modo `impersonate`: *"SET ROLE reproduce los permisos del rol para esta sesión, pero es una
  herramienta de prueba, no una frontera de seguridad."*
- MySQL/MariaDB + lote `ddl`: *"MySQL/MariaDB hacen COMMIT implícito en cada sentencia DDL: si
  el lote falla a mitad, lo ya ejecutado NO se revierte."*
- Lote de más de una sentencia: *"El lote tiene N sentencias y se ejecuta en orden,
  deteniéndose en el primer error."*

Son avisos **no bloqueantes**: banner informativo, no modal.

---

## 6. `POST /servers/{id}/query/execute` 🔌🔒

Ejecuta el lote de verdad. **Rate limit:** `30/minute`.

### Request

```jsonc
{
  "database": "tienda",
  "sql": "UPDATE pedidos SET estado = 'cancelado' WHERE creado_en < '2025-01-01'",
  "connection": { "mode": "provided", "username": "app_rw", "password": "…" },
  "confirm_token": "1754131200.9f3c…",
  "confirm_target_name": "tienda",
  "dry_run": false,
  "max_rows": null,
  "timeout_ms": null
}
```

**Tres reglas de oro:**

1. **`sql` debe ser EXACTAMENTE el mismo texto que se envió al preview.** El token está atado
   a un hash del SQL (solo se normalizan los espacios de los extremos). Un espacio de más en
   el medio, un salto de línea agregado, un `;` final borrado → **422**.
2. **`connection` debe ser la MISMA identidad que en el preview** (modo, usuario, host, rol).
   Cambiar cualquiera de ellos invalida el token → **422**.
3. **Un `SELECT` puro (`danger: "read"`) no necesita nada de esto**: se ejecuta mandando solo
   `database` + `sql` + `connection`.

| Campo | Nota |
|---|---|
| `confirm_token` | Obligatorio si el preview dijo `requires_confirmation: true`. |
| `confirm_target_name` | Obligatorio en el mismo caso. Debe ser **exactamente igual** al valor de `database`. Es el mismo patrón de doble factor que ya usa el flujo de "borrar base de datos". |
| `dry_run` | Ejecuta y revierte. Ver [§2.5](#25-dry_run-no-es-confiable-para-ddl-en-mysqlmariadb). |
| `max_rows` | Tope de filas por sentencia. Solo puede **bajar** el tope global (default 1000). |
| `timeout_ms` | Timeout por sentencia (mínimo 100). Puede subir por encima del default (30 s) hasta el techo global (300 s). |

### Response — `data: QueryExecuteOut`

```jsonc
{
  "server_id": 2,
  "database": "tienda",
  "engine": "mysql",
  "run_as": "app_rw",
  "connection_mode": "provided",
  "danger": "write",
  "success": true,
  "read_only": false,
  "dry_run": false,
  "committed": true,
  "rolled_back": false,
  "ddl_persisted": false,
  "statements": [
    {
      "seq": 0,
      "sql": "UPDATE pedidos SET estado = 'cancelado' WHERE creado_en < '2025-01-01'",
      "kind": "update",
      "danger": "write",
      "executed": true,
      "success": true,
      "duration_ms": 4173,
      "columns": [],
      "rows": [],
      "row_count": 0,
      "rows_affected": 2481902,
      "truncated": false,
      "policy_miss": false,
      "error": null
    }
  ],
  "connection_error": null,
  "warnings": [],
  "execution_id": 918
}
```

### Campos críticos para la UI

| Campo | Qué significa realmente |
|---|---|
| `success: false` | **El MOTOR rechazó alguna sentencia** (típicamente por permisos). La respuesta HTTP sigue siendo **200**. Ver [§9](#9-manejo-de-errores-el-motor-dijo-que-no--la-api-falló). |
| `read_only` | El lote corrió dentro de una transacción de solo lectura (siempre que `danger` sea `read`). |
| `committed` / `rolled_back` | Todo el lote va en **una** transacción. `read_only: true`, `dry_run: true` o cualquier fallo → `ROLLBACK`. |
| `ddl_persisted` | **El caso más confuso del módulo.** `true` = a pesar de `rolled_back: true`, quedaron cambios de **esquema** aplicados (commit implícito del DDL en MySQL/MariaDB). Mostrarlo de forma muy visible: sin esto, la UI diría "se revirtió todo" y sería mentira. |
| `statements[].executed: false` | Esa sentencia **no llegó a correr**: una anterior del lote falló y la ejecución se detuvo ahí. El lote corre en orden y para en el primer error. Mostrarlas atenuadas / "no ejecutada", no como fallidas. |
| `statements[].sql` | El SQL **realmente ejecutado**. Puede diferir del enviado: a un `SELECT` sin `LIMIT` propio se le empuja `LIMIT <max_rows + 1>` al motor (es la única forma de que el servidor deje de mandar filas; recortar del lado del gateway no evita la transferencia). Mostrar este texto, no el original, cuando difieran. |
| `statements[].rows` | Filas ya normalizadas a JSON: fechas/horas como ISO string, `TIME` como `HH:MM:SS` (admite negativos y >24 h), `Decimal` como string (para no perder precisión), binarios como hex con prefijo `0x` y marca de recorte, `UUID` como string, `NaN`/`Infinity` como string. |
| `truncated: true` | Había más filas de las mostradas. Avisarlo siempre. |
| `policy_miss: true` | **Caso raro y notable.** El motor rechazó la sentencia por la garantía de solo-lectura: el gateway la clasificó como lectura y en realidad escribía. Es un **bug del gateway**, no del usuario. Destacarlo con un texto tipo *"clasificación incorrecta detectada — por favor reportá esta consulta"*. |
| `connection_error` | No se pudo **ni conectar** con la credencial elegida (contraseña rechazada, rol inexistente, sin acceso a esa base). En los casos que son *resultado de la prueba* llega con HTTP 200 — ver [§9](#9-manejo-de-errores-el-motor-dijo-que-no--la-api-falló). |
| `execution_id` | Id de la fila del historial. Puede ser `null`: el historial es *best-effort* y nunca tira abajo una operación que ya se ejecutó en el motor. |

### `error` (por sentencia y en `connection_error`)

```jsonc
{ "code": "1142", "sqlstate": null, "message": "SELECT command denied to user 'app_ro'@'%' for table 'pagos'" }
```

Es el error **nativo del motor**, sin traducir a un mensaje genérico — justamente el texto que
se quiere leer al probar permisos. `code` es el errno de MySQL/MariaDB o el SQLSTATE de
PostgreSQL. **Mostralo tal cual**, en monoespaciado.

### `warnings[]` adicionales del execute

Además de los del preview, el execute puede devolver:

- *"MySQL/MariaDB hacen COMMIT implícito en cada sentencia DDL: las sentencias de esquema que
  ya se ejecutaron quedaron aplicadas y el ROLLBACK no las deshace."* (acompaña a
  `ddl_persisted: true`)
- *"No se pudo cerrar la transacción: el estado final del lote es INCIERTO y hay que
  verificarlo en el motor."* → este merece tratamiento de **alerta**, no de aviso.

### ⚠️ Gotcha: escritura sobre una base de datos de SISTEMA

Hay un guard que **solo corre en el `execute`**, no en el `preview`: si la base elegida es una
BD de sistema del motor (`mysql`, `information_schema`, `performance_schema`, `sys` /
`postgres`, `template0`, `template1`) y el lote no es de solo lectura, la respuesta es **403**
con `reasons: [{ "code": "system_database_write", … }]`.

Existe porque la base se elige **fuera** de la sentencia: `UPDATE user SET …` conectado a
`mysql` no menciona ningún esquema, así que el guard textual no lo ve.

**Implicación:** un preview sobre `mysql` con un `UPDATE` puede devolver `danger: "write"` y un
`confirm_token` válido, y aun así el execute responder 403. Para que la UI no lleve al usuario
por un callejón sin salida, conviene deshabilitar la escritura del lado del cliente cuando la
base seleccionada es una de esas (leerlas sí está permitido).

---

## 7. `GET /servers/{id}/query/history` 🔒

Bitácora paginada de ejecuciones del servidor. **No toca el motor.** **Rate limit:**
`60/minute`.

```
GET /api/v1/servers/2/query/history?page=1&size=20&database=tienda
```

| Param | Requerido | Descripción |
|---|---|---|
| `page` / `size` | no | Paginación estándar del gateway (`meta.total`, `meta.page`, `meta.size`). |
| `database` | no | Filtra por nombre exacto de base de datos. |

Ordenado por id **descendente** (lo más reciente primero).

### Response — `data: QueryHistoryOut[]`

```jsonc
{
  "data": [
    {
      "id": 918,
      "server_id": 2,
      "database_name": "tienda",
      "engine": "mysql",
      "admin_username": "ocarrasco",
      "connection_mode": "provided",
      "run_as_username": "app_rw",
      "impersonated_role": null,
      "sql_text": "UPDATE pedidos SET estado = 'cancelado' WHERE creado_en < '2025-01-01'",
      "danger_level": "write",
      "statement_count": 1,
      "status": "success",
      "read_only": false,
      "dry_run": false,
      "committed": true,
      "rows_returned": 0,
      "rows_affected": 2481902,
      "duration_ms": 4173,
      "error_code": null,
      "error_message": null,
      "created_at": "2026-08-02T12:01:07Z"
    }
  ],
  "meta": { "total": 143, "page": 1, "size": 20 }
}
```

| Campo | Nota |
|---|---|
| `status` | `success` (todo corrió sin error) · `error` (el motor rechazó alguna sentencia, **incluye falta de permisos**) · `blocked` (la política lo rechazó, **nunca se tocó el motor**) · `preview` (definido en el modelo, hoy no se escribe). |
| `sql_text` | El lote completo, con cualquier literal de contraseña reemplazado por `'***'`, recortado a 16 KB. Es lo que la UI vuelve a cargar en el editor. |
| `run_as_username` / `connection_mode` | La identidad con la que se ejecutó. Es la columna más valiosa de la tabla: responde "¿con qué usuario probamos esto?". |
| `admin_username` | Quién lo corrió desde el gateway. Puede ser `null`. |
| `error_code` / `error_message` | Primer error del lote (o el de conexión). Para las filas `blocked`, `error_message` trae los motivos concatenados. |

**Recordatorio:** no hay filas de resultado acá. Ver [§2.4](#24-el-historial-no-guarda-datos-solo-metadatos).

---

## 8. Tabla completa de códigos de motivo (`reasons[].code`)

Los `reasons` aparecen a dos niveles: por sentencia (`statements[].reasons`) y agregados del
lote (`reasons`). El `code` es **estable** — mapealo a icono/color/copy propio; el `message`
viene listo para mostrar si preferís no traducir.

### 8.1 Motivos que SIEMPRE implican `blocked`

Icono/color de **prohibido**, no de advertencia. Nunca ofrecer un botón de "confirmar igual".

| `code` | `message` (texto exacto del backend) | SQL de ejemplo que lo dispara |
|---|---|---|
| `dcl_grant_revoke` | GRANT/REVOKE no se ejecutan desde la consola: usa los endpoints de privilegios, que aplican los guards anti-lockout y dejan auditoría estructurada. | `GRANT SELECT ON tienda.* TO 'app'@'%'` · `REVOKE ALL ON t FROM r` |
| `dcl_user_role` | La gestión de usuarios/roles no se ejecuta desde la consola: usa los endpoints de usuarios del motor (cifran la credencial y auditan la operación). | `CREATE USER 'x'@'%' IDENTIFIED BY '…'` · `ALTER ROLE r …` · `DROP USER x` · `RENAME USER a TO b` · `SET PASSWORD = …` |
| `server_file_access` | La sentencia accede al sistema de archivos del servidor de base de datos. El gateway conecta con una credencial pseudo-root, así que el motor SÍ lo permitiría; está prohibido por política. | `SELECT … INTO OUTFILE '/tmp/x'` · `LOAD DATA INFILE …` · `SELECT LOAD_FILE('/etc/passwd')` · `SELECT pg_read_file('…')` · `lo_import()` / `lo_export()` · `COPY … FROM PROGRAM 'sh'` |
| `copy_statement` | COPY lee/escribe archivos en el host del servidor. Para mover datos entre bases usa el módulo de clonado, que lo hace por streaming y con auditoría. | `COPY t FROM '/tmp/x.csv'` |
| `extension_or_untrusted_language` | Instalar extensiones o crear rutinas en un lenguaje no confiable permite ejecutar código arbitrario en el host del servidor. | `CREATE EXTENSION dblink` · `CREATE FUNCTION f() … LANGUAGE plpython3u` |
| `native_code_load` | Cargar una librería nativa (UDF/plugin) o registrar un lenguaje procedural ejecuta código arbitrario en el host del servidor de base de datos. | `CREATE FUNCTION f RETURNS INT SONAME 'lib.so'` · `INSTALL SONAME 'x'` · `CREATE LANGUAGE plperlu` |
| `outbound_connection` | La sentencia hace que el SERVIDOR de base de datos abra una conexión saliente. Esa conexión la inicia el motor, así que NO pasa por el guard anti-SSRF del gateway. | `CREATE SERVER …` · `CREATE FOREIGN DATA WRAPPER …` · `IMPORT FOREIGN SCHEMA …` · `CREATE PUBLICATION/SUBSCRIPTION …` · `SELECT dblink('…')` · `CREATE TABLE … ENGINE=FEDERATED` |
| `server_control_function` | La función administra el servidor entero (mata sesiones, recarga configuración, promueve el standby). No es una lectura por más que viaje dentro de un SELECT, y una transacción de solo lectura no la frena. | `SELECT pg_terminate_backend(123)` · `SELECT pg_reload_conf()` · `SELECT pg_promote()` · `pg_create_physical_replication_slot(…)` |
| `server_global_state` | La sentencia modifica el estado GLOBAL del servidor y afectaría a todas sus bases de datos, no solo a la seleccionada. | `SET GLOBAL max_connections = 500` · `SET @@GLOBAL.x = 1` · `ALTER SYSTEM SET …` · `FLUSH PRIVILEGES` · `KILL 42` · `SHUTDOWN` · `RESET …` · `START/STOP REPLICA` · `INSTALL PLUGIN …` · `CREATE TABLESPACE …` |
| `database_lifecycle` | Crear, modificar o eliminar bases de datos/esquemas tiene endpoints dedicados con confirmación por nombre, token firmado y guard de BDs de sistema. | `CREATE DATABASE nueva` · `DROP SCHEMA public` · `ALTER DATABASE x …` |
| `session_guarantee_override` | La sentencia cambia parámetros de sesión que sostienen las garantías de la consola (timeout, solo lectura, o el esquema con el que se resuelven los nombres sin calificar). | `SET search_path = otro` · `SET statement_timeout = 0` · `SET FOREIGN_KEY_CHECKS = 0` · `SET sql_mode = …` · `SET STATEMENT … FOR …` · `XA START …` |
| `session_control` | La consola administra su propia sesión y transacción; las sentencias de control de sesión/transacción romperían esa garantía (incluida la de solo lectura). | `BEGIN` · `COMMIT` · `ROLLBACK` · `SAVEPOINT s` · `SET TRANSACTION …` · `SET autocommit = 0` · `LOCK TABLES t WRITE` · `USE otra_db` · `HANDLER t OPEN` |
| `role_switch` | Cambiar de rol dentro de la consola anularía el usuario elegido para la prueba de permisos y devolvería la sesión a la credencial pseudo-root. Elegí el usuario en el propio request. | `SET ROLE admin` · `RESET ROLE` · `SET SESSION AUTHORIZATION postgres` · `SELECT set_config('role','postgres',false)` · `DISCARD ALL` |
| `delimiter_directive` | La directiva DELIMITER no se admite en la consola: es del cliente mysql, no del motor, y agrupar sentencias con ella impediría clasificarlas una por una. Enviá las sentencias separadas por ';' — los cuerpos BEGIN…END se reconocen solos. | `DELIMITER //` |
| `dynamic_sql` | Las sentencias preparadas ejecutan SQL que esta política no puede clasificar de antemano. | `PREPARE s FROM '…'` · `EXECUTE s` · `DEALLOCATE PREPARE s` |
| `gateway_internal_table` | La sentencia nombra tablas de contabilidad interna del gateway (*lista de tablas*); modificarlas dejaría la base sin puntero de versión de migraciones. | `SELECT * FROM _gw_v_ecommerce` · `DROP TABLE _gw_stg_x` |
| `system_schema_write` | La sentencia modifica un esquema del sistema del motor. Leerlos está permitido; escribirlos corrompería el propio servidor. | `UPDATE mysql.user SET …` · `` DELETE FROM `mysql`.`db` `` · `ALTER TABLE pg_catalog.pg_class …` |
| `system_database_write` ⚠️ | Modificar una base de datos de sistema corrompería el propio servidor. | Cualquier `write`/`ddl` con `database` = `mysql`, `information_schema`, `performance_schema`, `sys`, `postgres`, `template0`, `template1`. **Solo lo emite el `execute`**, no el preview (ver [§6](#6-post-serversidqueryexecute-)). |

> **Nota para el copy de la UI.** Si hace falta un tooltip de ayuda que explique *por qué*
> existe esta lista, la razón de fondo es una sola: **el gateway se conecta con una credencial
> pseudo-root, así que el motor SÍ permitiría todo esto**. No están prohibidas porque el motor
> las rechace — están prohibidas porque el gateway decide no ofrecerlas nunca desde acá. Varias
> tienen un endpoint dedicado en el propio gateway, con guards y auditoría propios; el mensaje
> lo dice y conviene enlazarlo desde la UI.

### 8.2 Motivos informativos (pueden aparecer en cualquier nivel)

Explican **por qué se clasificó así**. No implican bloqueo por sí solos. Tratalos como
"detalle de la clasificación" (colapsable, tooltip, o lista secundaria), no como error.

| `code` | Nivel al que eleva | `message` |
|---|---|---|
| `nested_dml` | `write` | La sentencia contiene un *(insert/update/delete/merge)* anidado (CTE o subconsulta) que modifica datos. |
| `row_locking_read` | `write` | La consulta bloquea filas (FOR UPDATE / FOR SHARE); no es una lectura pura. |
| `select_into` | `ddl` | SELECT … INTO materializa el resultado (crea una tabla o asigna una variable de sesión); no es una lectura pura. |
| `explain_analyze_executes` | `ddl` | EXPLAIN ANALYZE EJECUTA la sentencia analizada para medirla; no es una lectura. Usá EXPLAIN a secas para ver el plan sin ejecutar. |
| `opaque_statement` | `ddl` | El parser no reconoce la estructura de la sentencia; se trata como peligrosa por política fail-closed. |
| `unparseable` | `ddl` | No se pudo analizar la sentencia; se trata como peligrosa por política fail-closed. |
| `unmapped_statement` | `ddl` | Tipo de sentencia no contemplado por la política; se trata como peligrosa por política fail-closed. |
| `read_by_leading_keyword` | `read` | El parser no reconoce la sentencia, pero su forma es de lectura. Se ejecuta igualmente dentro de una transacción de solo lectura. |

Los tres *fail-closed* (`opaque_statement`, `unparseable`, `unmapped_statement`) son los que
más se van a ver en la práctica con SQL legítimo (un `CALL` a un procedimiento, por ejemplo).
Vale la pena una redacción propia que no alarme de más:

> *"No se pudo determinar con certeza qué hace esta sentencia, así que se trata como
> peligrosa y pide confirmación."*

---

## 9. Manejo de errores: "el motor dijo que no" ≠ "la API falló"

Esta es la decisión de diseño más importante del módulo para el frontend. La API separa dos
cosas que la mayoría de las UIs mezclan.

### 9.1 Rechazo del MOTOR → HTTP **200**, `success: false`

Un rechazo por permisos **es el resultado que se estaba buscando**. Confirma que el permiso no
está. Llega así:

```jsonc
// HTTP 200
{
  "data": {
    "success": false,
    "danger": "write",
    "run_as": "app_ro",
    "statements": [{
      "seq": 0, "kind": "delete", "danger": "write",
      "executed": true, "success": false, "duration_ms": 12,
      "rows_affected": null, "policy_miss": false,
      "error": {
        "code": "1142", "sqlstate": null,
        "message": "DELETE command denied to user 'app_ro'@'%' for table 'pedidos'"
      }
    }],
    "connection_error": null
  }
}
```

**Cómo mostrarlo:** con tono **neutro/informativo**, no con el rojo de error de sistema. Desde
la perspectiva del usuario esto es una prueba **exitosa**. Sugerencia de copy:

> ✅ *Prueba completada — el motor rechazó la operación.*
> `app_ro` **no puede** ejecutar `DELETE` sobre `pedidos`.
> `1142: DELETE command denied to user 'app_ro'@'%' for table 'pedidos'`

Lo mismo aplica a `connection_error`: una contraseña incorrecta o un rol inexistente son
resultados de la prueba, no caídas de la API.

#### Qué errores de conexión llegan con 200 (y cuáles no)

Depende del **modo**: con `admin`/`impersonate` la conexión usa la credencial pseudo-root del
gateway, así que un "access denied" ahí **no es el resultado de nada** — es el gateway mal
configurado, y sale como error HTTP.

| Situación | `stored` / `provided` | `admin` / `impersonate` |
|---|---|---|
| Contraseña incorrecta (`1045`, `28P01`) | **200** + `connection_error` | **502** (mala config del gateway) |
| Host no autorizado (`1130`, `28000`) | **200** + `connection_error` | 502 |
| Límite de conexiones del usuario (`1226`, `1203`) | **200** + `connection_error` | 502 |
| Sin acceso a la base (`1044`) | **200** + `connection_error` | **200** + `connection_error` |
| Base inexistente (`1049`, `3D000`) | **200** + `connection_error` | **200** + `connection_error` |
| Rol inexistente en `SET ROLE` (`42704`, `22023`) | — | **200** + `connection_error` |
| Privilegio insuficiente (`42501`) | **200** + `connection_error` | **200** + `connection_error` |
| Host inalcanzable / red caída | **502** | **502** |
| Timeout de conexión | **504** | **504** |

### 9.2 Errores HTTP reales — la operación falló

Todos usan el envelope de error estándar del gateway:

```jsonc
{ "detail": { "msg": "…", "type": "AppHttpException", "public_context": { … } } }
```

`public_context` está presente en **todos los ambientes** (es info destinada al operador).
`context` y `loc` solo aparecen en desarrollo — **no dependas de ellos**.

#### `403` — sentencia prohibida (nivel `blocked`)

No se tocó el motor. Se auditó y quedó registrado en el historial con `status: "blocked"`.

```jsonc
// HTTP 403
{
  "detail": {
    "msg": "La consulta contiene sentencias prohibidas por la política del modo seguro y no se ejecuta ni con confirmación.",
    "type": "AppHttpException",
    "public_context": {
      "database": "tienda",
      "blocked_statements": [
        { "seq": 1, "sql": "GRANT SELECT ON tienda.* TO 'app_ro'@'%'" }
      ],
      "reasons": [
        {
          "code": "dcl_grant_revoke",
          "message": "GRANT/REVOKE no se ejecutan desde la consola: usa los endpoints de privilegios, que aplican los guards anti-lockout y dejan auditoría estructurada."
        }
      ]
    }
  }
}
```

**Idealmente este 403 nunca se ve**: el preview ya devolvió `blocked: true` y la UI no debería
haber ofrecido el botón. Es una segunda barrera (y cubre el caso `system_database_write`, que
el preview no detecta).

#### `422` — falta algo o no corresponde

Cinco causas distintas, todas con el mismo status:

| Causa | `msg` |
|---|---|
| Falta un campo del modo de conexión | *"El modo provided requiere 'connection.password'…"* / *"El modo stored requiere 'connection.username'."* |
| `impersonate` en MySQL/MariaDB | *"La impersonación con SET ROLE solo existe en PostgreSQL…"* |
| Falta `confirm_target_name` o no coincide | *"Esta consulta modifica datos o estructura: 'confirm_target_name' debe coincidir exactamente con el nombre de la base de datos."* |
| Falta `confirm_token` | *"Esta consulta modifica datos o estructura: solicitá el preview y enviá su 'confirm_token'."* |
| Token que no corresponde (cambió el SQL, la base o el usuario) | *"El token de confirmación no corresponde a esta operación. Si cambiaste el SQL, la base de datos o el usuario, volvé a solicitar el preview."* |
| SQL vacío o por encima de `QUERY_MAX_SQL_BYTES` | *"El SQL supera el tope de 262144 bytes (N enviados)."* |

El último caso de token es el que más va a aparecer en producción: **la UI debe volver a pedir
el preview automáticamente** cuando lo reciba, en vez de mostrar un error críptico.

#### `404` — usuario `stored` inexistente

```jsonc
{ "detail": { "msg": "El usuario no está en el inventario del gateway, así que no hay contraseña almacenada. Usá el modo 'provided' con la contraseña.", "type": "AppHttpException" } }
```

El mensaje ya sugiere la salida: ofrecer un botón *"Probar con contraseña"* que cambie el modo
a `provided` conservando el username tipeado.

#### `409` — dos causas

1. **Usuario `stored` sin contraseña guardada** (fue adoptado, no creado por el gateway):
   *"El usuario está en el inventario pero el gateway nunca fijó su contraseña (el motor solo
   guarda un hash irreversible). Usá el modo 'provided'."* → misma salida que el 404.
2. **El destino es la propia base de metadatos del gateway**: *"El destino es la propia base de
   metadatos del gateway. La consola no puede operar sobre ella."* Es un bloqueo **por
   diseño** y no tiene salida: si la base del gateway vive en un servidor del inventario, un
   `DROP` desde acá se llevaría el inventario, la auditoría, el historial y las credenciales
   pseudo-root cifradas de todos los servidores. El chequeo compara **resolviendo los hosts a
   IPs**, así que apuntar a la misma máquina por otra grafía tampoco funciona.

#### `410` — el `confirm_token` expiró

```jsonc
{ "detail": { "msg": "El token de confirmación expiró; vuelve a solicitar el preview.", "type": "AppHttpException" } }
```

Pasaron más de 2 minutos desde el preview. **La UI debería re-pedir el preview
automáticamente** y volver a mostrar la confirmación (con la estimación de filas actualizada,
que además puede haber cambiado). Nunca mostrar esto como un fallo del usuario.

#### `429` — rate limit

`30/minute` en preview y execute, `60/minute` en history. Por IP.

```jsonc
{ "detail": { "msg": "Demasiadas solicitudes. Límite: 30 per 1 minute", "type": "RateLimitExceeded" } }
```

Sugerencia: deshabilitar el botón unos segundos y mostrar el texto del límite tal cual. Ojo con
llamar al preview **en cada tecla** del editor: se agota el límite en segundos. Ver
[§14](#14-checklist-de-implementación).

#### `5xx` — fallo real de infraestructura

- **502** — *"No se pudo conectar al servidor de base de datos destino."*
- **504** — *"La operación en el servidor destino excedió el tiempo de espera."*
- **500** — error inesperado.

Estos **sí** son errores de sistema: rojo, opción de reintentar, y el `request_id` visible para
soporte.

---

## 10. Flujos completos, paso a paso

### 10.1 Verificar que un usuario de solo lectura NO puede borrar

*El caso de uso que justifica todo el módulo.* El admin acaba de otorgarle `SELECT` a
`app_ro` y quiere comprobar que no puede borrar.

**Paso 1 — el usuario escribe el `DELETE` y la UI pide el preview.**

```http
POST /api/v1/servers/2/query/preview
```
```json
{
  "database": "tienda",
  "sql": "DELETE FROM pedidos WHERE id = 1",
  "connection": { "mode": "provided", "username": "app_ro", "password": "s3cr3t" }
}
```

```jsonc
// 200
{
  "data": {
    "server_id": 2, "database": "tienda", "engine": "mysql",
    "run_as": "app_ro", "connection_mode": "provided",
    "danger": "write", "requires_confirmation": true, "blocked": false,
    "statements": [
      { "seq": 0, "sql": "DELETE FROM pedidos WHERE id = 1", "kind": "delete",
        "danger": "write", "reasons": [], "estimated_rows": null }
    ],
    "reasons": [], "warnings": [],
    "confirm_token": "1754131320.4be1…",
    "expires_at": "2026-08-02T12:02:00Z"
  }
}
```

> `estimated_rows: null` aquí es **el primer indicio del resultado**: el `COUNT` corrió con la
> misma credencial y `app_ro` no pudo leer la tabla, o el conteo no era exacto. La UI no debe
> interpretarlo como "no afecta filas".

**Paso 2 — la UI muestra el diálogo de confirmación**, el admin tipea `tienda`.

**Paso 3 — execute.**

```http
POST /api/v1/servers/2/query/execute
```
```json
{
  "database": "tienda",
  "sql": "DELETE FROM pedidos WHERE id = 1",
  "connection": { "mode": "provided", "username": "app_ro", "password": "s3cr3t" },
  "confirm_token": "1754131320.4be1…",
  "confirm_target_name": "tienda"
}
```

```jsonc
// 200  ← ¡200, no 403!
{
  "data": {
    "run_as": "app_ro", "connection_mode": "provided",
    "danger": "write", "success": false,
    "read_only": false, "dry_run": false,
    "committed": false, "rolled_back": true, "ddl_persisted": false,
    "statements": [{
      "seq": 0, "sql": "DELETE FROM pedidos WHERE id = 1", "kind": "delete", "danger": "write",
      "executed": true, "success": false, "duration_ms": 8,
      "columns": [], "rows": [], "row_count": 0, "rows_affected": null,
      "truncated": false, "policy_miss": false,
      "error": { "code": "1142", "sqlstate": null,
                 "message": "DELETE command denied to user 'app_ro'@'%' for table 'pedidos'" }
    }],
    "connection_error": null, "warnings": [], "execution_id": 921
  }
}
```

**Paso 4 — la UI muestra el resultado en tono de éxito**, porque lo es:

> ✅ **Permiso verificado** — `app_ro` **no puede** borrar de `pedidos`.
> `1142: DELETE command denied to user 'app_ro'@'%' for table 'pedidos'`

---

### 10.2 `UPDATE` masivo: ver cuántas filas antes de confirmar

**Paso 1 — preview.**

```http
POST /api/v1/servers/2/query/preview
```
```json
{
  "database": "tienda",
  "sql": "UPDATE pedidos SET estado = 'cancelado' WHERE creado_en < '2025-01-01'",
  "connection": { "mode": "stored", "username": "app_rw", "host": "%" }
}
```

```jsonc
// 200
{
  "data": {
    "danger": "write", "requires_confirmation": true, "blocked": false,
    "run_as": "app_rw", "connection_mode": "stored",
    "statements": [
      { "seq": 0, "kind": "update", "danger": "write",
        "sql": "UPDATE pedidos SET estado = 'cancelado' WHERE creado_en < '2025-01-01'",
        "reasons": [], "estimated_rows": 2481902 }
    ],
    "reasons": [], "warnings": [],
    "confirm_token": "1754131200.9f3c…", "expires_at": "2026-08-02T12:02:00Z"
  }
}
```

**Paso 2 — el diálogo de confirmación pone la cifra en el centro**, no en letra chica:

> ⚠️ Esta operación va a modificar **2 481 902 filas** de `pedidos`.
> Escribí `tienda` para confirmar. `[ ______ ]`
> ☐ Ejecutar en modo de prueba (`dry_run`) y revertir al final

**Paso 3 — el admin duda y activa `dry_run`.**

```json
{ "…": "…", "dry_run": true, "confirm_token": "1754131200.9f3c…", "confirm_target_name": "tienda" }
```

```jsonc
// 200
{
  "data": {
    "success": true, "dry_run": true,
    "committed": false, "rolled_back": true, "ddl_persisted": false,
    "statements": [{ "seq": 0, "kind": "update", "executed": true, "success": true,
                     "rows_affected": 2481902, "duration_ms": 51204 }],
    "warnings": [], "execution_id": 922
  }
}
```

> `rows_affected` es el número **real** del motor (no la estimación) y `rolled_back: true`
> confirma que nada quedó aplicado. `ddl_persisted: false` cierra la duda: no había DDL.

**Paso 4 — el admin repite sin `dry_run`.** ⚠️ Si pasaron más de 2 minutos desde el preview,
el token ya expiró (**410**) y hay que repetirlo. Es el caso más frecuente de este flujo:
**la UI debería re-pedir el preview de forma transparente** justo antes del execute definitivo.

---

### 10.3 Intento de `GRANT`: la UI lo impide antes de llegar al backend

**Paso 1 — preview.**

```http
POST /api/v1/servers/2/query/preview
```
```json
{
  "database": "tienda",
  "sql": "GRANT SELECT ON tienda.* TO 'app_ro'@'%'",
  "connection": { "mode": "admin" }
}
```

```jsonc
// 200 — el preview NO falla: informa
{
  "data": {
    "danger": "blocked",
    "requires_confirmation": true,   // ← ver el 🚨 de §5: `blocked` MANDA sobre este campo
    "blocked": true,
    "run_as": "root_gw", "connection_mode": "admin",
    "statements": [
      { "seq": 0, "sql": "GRANT SELECT ON tienda.* TO 'app_ro'@'%'",
        "kind": "blocked", "danger": "blocked",
        "reasons": [{ "code": "dcl_grant_revoke",
                      "message": "GRANT/REVOKE no se ejecutan desde la consola: usa los endpoints de privilegios, que aplican los guards anti-lockout y dejan auditoría estructurada." }],
        "estimated_rows": null }
    ],
    "reasons": [{ "code": "dcl_grant_revoke", "message": "…" }],
    "warnings": ["Se ejecutará con la credencial pseudo-root del servidor: los permisos NO se están probando, se están evitando. Elegí un usuario concreto para verificar permisos."],
    "confirm_token": null,
    "expires_at": null
  }
}
```

**Paso 2 — la UI bloquea el botón de ejecutar** y muestra:

> 🚫 **Esta operación no se puede ejecutar desde la consola.**
> GRANT/REVOKE no se ejecutan desde la consola: usá los endpoints de privilegios, que aplican
> los guards anti-lockout y dejan auditoría estructurada.
> → **[Ir a Permisos del servidor]**

El enlace a la pantalla correspondiente del gateway es lo que convierte un bloqueo frustrante
en una redirección útil. Cada código de [§8.1](#81-motivos-que-siempre-implican-blocked) que
menciona un endpoint dedicado (`dcl_grant_revoke`, `dcl_user_role`, `database_lifecycle`,
`copy_statement`) merece ese enlace.

**Si aun así se llama al execute** (por ejemplo desde un cliente propio), la respuesta es el
**403** de [§9.2](#403--sentencia-prohibida-nivel-blocked), y queda registrado en el historial
con `status: "blocked"` sin haber tocado el motor.

---

### 10.4 Lote mixto: una sentencia falla y las siguientes no corren

```json
{
  "database": "tienda",
  "sql": "ALTER TABLE pedidos ADD COLUMN nota TEXT;\nALTER TABLE pedidoss ADD COLUMN otra INT;\nALTER TABLE clientes ADD COLUMN vip TINYINT;",
  "connection": { "mode": "admin" },
  "confirm_token": "…", "confirm_target_name": "tienda", "dry_run": true
}
```

```jsonc
// 200
{
  "data": {
    "engine": "mariadb", "danger": "ddl", "success": false,
    "dry_run": true, "committed": false, "rolled_back": true,
    "ddl_persisted": true,                       // ← EL DATO IMPORTANTE
    "statements": [
      { "seq": 0, "kind": "alter", "executed": true,  "success": true,  "duration_ms": 310 },
      { "seq": 1, "kind": "alter", "executed": true,  "success": false, "duration_ms": 6,
        "error": { "code": "1146", "message": "Table 'tienda.pedidoss' doesn't exist" } },
      { "seq": 2, "kind": "alter", "executed": false, "success": false, "duration_ms": 0 }
    ],
    "warnings": [
      "MySQL/MariaDB hacen COMMIT implícito en cada sentencia DDL: las sentencias de esquema que ya se ejecutaron quedaron aplicadas y el ROLLBACK no las deshace.",
      "MySQL/MariaDB hacen COMMIT implícito en cada sentencia DDL: si el lote falla a mitad, lo ya ejecutado NO se revierte.",
      "El lote tiene 3 sentencias y se ejecuta en orden, deteniéndose en el primer error."
    ],
    "execution_id": 925
  }
}
```

**Qué debe comunicar la pantalla, en este orden:**

1. 🔴 **`ddl_persisted: true` arriba de todo:** *"A pesar del modo de prueba, la columna `nota`
   quedó creada en `pedidos`. MySQL/MariaDB no pueden revertir cambios de esquema."*
2. La sentencia 1 falló, con el error nativo.
3. La sentencia 2 **no se ejecutó** (atenuada, badge "no ejecutada" — no roja).

En **PostgreSQL** el mismo lote habría vuelto con `ddl_persisted: false` y nada aplicado.

---

### 10.5 Atajo: consultar permisos SIN confirmación

La blocklist de `dcl_grant_revoke` está anclada a `^GRANT|^REVOKE`, así que **`SHOW GRANTS` no
está bloqueado**: es una lectura y se ejecuta directo, sin preview obligatorio, sin token, sin
tipear el nombre de la base. Lo mismo vale para leer los catálogos del sistema (leerlos está
permitido; escribirlos es lo que se bloquea).

Verificado contra la política real — todas estas vuelven `danger: "read"`:

| SQL | Motor | Nota |
|---|---|---|
| `SHOW GRANTS FOR 'app_ro'@'%'` | MySQL/MariaDB | Trae el motivo informativo `read_by_leading_keyword` (el parser no la reconoce, pero su forma es de lectura). |
| `SELECT * FROM mysql.db WHERE User = 'app_ro'` | MySQL/MariaDB | Leer un esquema de sistema está permitido. |
| `SELECT * FROM information_schema.table_privileges WHERE grantee = 'app_ro'` | PostgreSQL | |
| `SELECT has_table_privilege('app_ro', 'pedidos', 'SELECT')` | PostgreSQL | Respuesta booleana directa. |

**Idea de UI:** una fila de *acciones rápidas* sobre el editor —"¿Qué permisos tiene este
usuario?"— que precargue una de estas consultas según el motor. Es el camino más corto del
módulo: un clic, sin confirmación, y responde la pregunta que trajo al admin a esta pantalla.
Ojo con la diferencia conceptual: `SHOW GRANTS` dice **qué permisos están otorgados en el
papel**; ejecutar la operación real como ese usuario ([§10.1](#101-verificar-que-un-usuario-de-solo-lectura-no-puede-borrar))
dice **qué pasa de verdad**. Las dos cosas son útiles y no siempre coinciden.

---

## 11. Interpretación visual: pantallas, estados y transiciones

Conceptual — sin tecnología ni implementación. Tres pantallas y un diálogo.

### 11.1 Pantalla principal: Consola

```
[Barra superior]
  Servidor: [selector ▾ srv-prod-mysql]   Base de datos: [selector ▾ tienda]
  Ejecutar como: [selector de identidad ▾]              [Historial]

[Banner de identidad]   ← persistente, cambia de color según el modo
  🔴 admin        "Operando como administrador total del servidor. No estás probando
                   permisos: los estás evitando."
  🔵 stored/provided  "Ejecutando como app_ro@% — este es el usuario que se está probando."
  🟣 impersonate  "Rol adoptado con SET ROLE (solo PostgreSQL). Herramienta de prueba,
                   no frontera de seguridad."

[Editor SQL]
  área de texto multilínea · contador de bytes (tope 256 KB)
  Opciones: ☐ modo de prueba (dry_run)   Tope de filas: [1000]   Timeout: [30 s]

[Barra de clasificación]   ← se llena con el resultado del preview
  Badge de nivel: read / write / ddl / blocked
  Lista por sentencia: #seq · kind · nivel · filas estimadas · motivos (colapsable)

[Botón principal]
  danger=read     → "Ejecutar"                    (directo)
  danger=write    → "Revisar y confirmar…"        (abre el diálogo)
  danger=ddl      → "Revisar y confirmar…"        (abre el diálogo)
  danger=blocked  → deshabilitado + motivo + enlace al endpoint correcto

[Panel de resultados]   ← pestaña por sentencia si el lote tiene más de una
  Tabla de filas (columns/rows) · badge "recortado a N filas" si truncated
  o bien: filas afectadas · duración · estado transaccional
```

**Estados de la pantalla:**

| Estado | Qué se muestra |
|---|---|
| `inicial` | Editor vacío, selector de identidad sin elegir, botón deshabilitado. |
| `clasificando` | Spinner sobre la barra de clasificación mientras corre el preview. El editor **no** se bloquea. |
| `clasificado` | Badge de nivel + lista por sentencia + warnings. Botón habilitado según el nivel. |
| `bloqueado` | Botón deshabilitado con el motivo y el enlace al módulo correspondiente. |
| `ejecutando` | Botón en carga, editor y selectores **bloqueados** (no se puede cambiar el SQL a mitad). Cancelar no está soportado por la API: comunicar que hay que esperar al timeout. |
| `resultado ok` | Tabla de filas o conteo de afectadas, en verde/neutro. |
| `resultado rechazado por el motor` | **Tono neutro/informativo**, no rojo. Error nativo en monoespaciado. `success: false`. |
| `error de sistema` | Rojo, `request_id` visible, botón de reintentar. Solo para 5xx / 502 / 504. |
| `token expirado` | Transición automática de vuelta a `clasificando` (re-preview) + aviso discreto. |

### 11.2 Diálogo de confirmación (`write` / `ddl`)

```
[Modal — no descartable con clic afuera]
  Título: ⚠️ Confirmar operación de escritura   /   ⚠️ Confirmar cambio de estructura

  Resumen:
    Servidor srv-prod-mysql · Base "tienda" · Ejecutando como app_rw@%
    Nivel: write · 1 sentencia
    → Afectará aproximadamente 2 481 902 filas de "pedidos"
      (o bien: "No se pudo estimar cuántas filas afectará" — NO significa que sean pocas)

  Lista de sentencias (el SQL exacto que se va a ejecutar, monoespaciado)

  Avisos (warnings del preview)

  Confirmación:
    "Escribí el nombre de la base de datos para confirmar"
    [ _________ ]     ← el botón se habilita solo con coincidencia EXACTA

  Contador: "Esta confirmación vence en 1:47"     ← desde expires_at

  [Cancelar]   [Ejecutar]
```

**Reglas del diálogo:**

- El campo de confirmación **no** se pre-rellena, **no** se autocompleta y **no** se recuerda.
- Comparación exacta, sensible a mayúsculas (el backend compara `confirm_target_name ==
  database` con igualdad estricta).
- El contador de vencimiento sale de `expires_at`. Al llegar a cero: re-pedir el preview de
  forma transparente y refrescar la estimación, o cerrar el diálogo con un aviso claro.
- Si `dry_run` está activo **y** el nivel es `ddl` **y** el motor es MySQL/MariaDB, agregar un
  aviso destacado: *"El modo de prueba NO puede revertir cambios de estructura en este motor."*

### 11.3 Panel de resultados

Un bloque por sentencia, en orden de `seq`:

| Situación | Presentación |
|---|---|
| `executed: true, success: true` con filas | Tabla `columns`/`rows`. Badge de duración. Badge **"recortado — mostrando las primeras N filas"** si `truncated`. |
| `executed: true, success: true` sin filas | *"N filas afectadas · 4.2 s"*. |
| `executed: true, success: false` | Bloque neutro con el `error.code` y `error.message` nativos. |
| `executed: false` | Atenuado, badge *"no ejecutada — el lote se detuvo antes"*. |
| `policy_miss: true` | Bloque **distinto de los demás**: *"El gateway clasificó mal esta consulta (la trató como lectura y escribe). Por favor reportá esta consulta."* Es un bug del gateway, no del usuario. |

**Encabezado del panel**, siempre visible cuando aplique:

- `ddl_persisted: true` → **la alerta más prominente de la pantalla**.
- `dry_run: true` + `rolled_back: true` → *"Modo de prueba: nada se guardó."*
- `connection_error` → *"No se pudo conectar como `app_ro`"* + error nativo, en tono neutro
  (es resultado de la prueba, no una caída).
- `warnings[]` → lista informativa al pie.

### 11.4 Pantalla de historial

```
[Filtros]   Base de datos: [selector ▾]   (paginación estándar page/size)

[Tabla]
  Fecha · Admin · Ejecutado como (usuario + modo) · Base · Nivel · Estado ·
  Sentencias · Filas dev. / afect. · Duración · Acciones

  Estado:  success (verde) · error (neutro: el motor rechazó) · blocked (gris/prohibido)

  Acciones por fila: [Ver SQL]  [Cargar en el editor]

[Estado vacío]
  "Todavía no se ejecutó ninguna consulta en este servidor."

[Aviso permanente al pie]
  "El historial guarda qué se ejecutó, no los resultados. Para volver a ver los datos hay
   que ejecutar la consulta de nuevo."
```

`[Cargar en el editor]` copia `sql_text` a la consola y **preselecciona la misma identidad**
(`connection_mode` + `run_as_username`) — salvo `provided`, donde la contraseña no existe y hay
que volver a pedirla. Ese es el valor real del historial.

### 11.5 Transiciones

```
Consola (editor)
  → [el usuario deja de escribir / toca "Analizar"] → preview
      → danger=read     → [clic "Ejecutar"] → execute → Resultados
      → danger=write|ddl→ [clic "Revisar y confirmar…"] → Diálogo de confirmación
            → [nombre correcto + clic "Ejecutar"] → execute → Resultados
                  → 410 token expirado → preview de nuevo → Diálogo (reabierto, cifra actualizada)
                  → 422 token no corresponde → preview de nuevo → Diálogo
            → [cancelar] → Consola (sin cambios, token descartado)
      → blocked=true    → Consola con el botón deshabilitado + motivo + enlace al módulo correcto

  → [cualquier edición del SQL o del selector de identidad]
      → descartar confirm_token y volver al estado "sin clasificar"

Consola → [clic "Historial"] → Historial
  → [clic "Cargar en el editor"] → Consola (SQL + identidad precargados, sin clasificar)
```

---

## 12. Tipos (referencia rápida)

```ts
type DangerLevel = "read" | "write" | "ddl" | "blocked";
type ConnectionMode = "admin" | "stored" | "provided" | "impersonate";
type HistoryStatus = "success" | "error" | "blocked" | "preview";

interface QueryConnectionIn {
  mode?: ConnectionMode;          // default "admin"
  username?: string | null;       // stored | provided
  host?: string | null;           // stored, MySQL/MariaDB
  password?: string | null;       // provided — nunca se persiste
  role?: string | null;           // impersonate, solo PostgreSQL
}

interface QueryReasonOut { code: string; message: string; }

interface QueryPreviewIn {
  database: string;               // 1..128
  sql: string;                    // 1..QUERY_MAX_SQL_BYTES
  connection?: QueryConnectionIn;
  estimate_impact?: boolean;      // default true
}

interface QueryStatementPlanOut {
  seq: number;
  sql: string;
  kind: string;
  danger: DangerLevel;
  reasons: QueryReasonOut[];
  estimated_rows: number | null;  // null ≠ "no afecta filas"
}

interface QueryPreviewOut {
  server_id: number; database: string; engine: string;
  run_as: string; connection_mode: ConnectionMode;
  danger: DangerLevel;
  requires_confirmation: boolean;
  blocked: boolean;
  statements: QueryStatementPlanOut[];
  reasons: QueryReasonOut[];
  warnings: string[];
  confirm_token: string | null;   // TTL 2 min
  expires_at: string | null;      // ISO 8601
}

interface QueryExecuteIn {
  database: string;
  sql: string;                    // EXACTAMENTE el del preview
  connection?: QueryConnectionIn; // MISMA identidad que en el preview
  confirm_token?: string | null;
  confirm_target_name?: string | null;   // === database
  dry_run?: boolean;              // default false
  max_rows?: number | null;       // >= 1, solo puede bajar el tope global
  timeout_ms?: number | null;     // >= 100, techo QUERY_MAX_TIMEOUT_MS
}

interface QueryErrorOut {
  code: string | null;            // errno MySQL/MariaDB o SQLSTATE
  sqlstate: string | null;
  message: string;                // mensaje NATIVO del motor
}

interface QueryStatementResultOut {
  seq: number; sql: string; kind: string; danger: DangerLevel;
  executed: boolean;              // false = el lote se detuvo antes
  success: boolean;
  duration_ms: number;
  columns: string[];
  rows: unknown[][];              // valores ya normalizados a JSON
  row_count: number;
  rows_affected: number | null;
  truncated: boolean;
  policy_miss: boolean;           // bug de clasificación del gateway
  error: QueryErrorOut | null;
}

interface QueryExecuteOut {
  server_id: number; database: string; engine: string;
  run_as: string; connection_mode: ConnectionMode;
  danger: DangerLevel;
  success: boolean;               // false = el MOTOR rechazó algo (HTTP sigue siendo 200)
  read_only: boolean;
  dry_run: boolean;
  committed: boolean;
  rolled_back: boolean;
  ddl_persisted: boolean;         // rolled_back pero el esquema quedó cambiado igual
  statements: QueryStatementResultOut[];
  connection_error: QueryErrorOut | null;
  warnings: string[];
  execution_id: number | null;    // historial best-effort
}

interface QueryHistoryOut {
  id: number; server_id: number; database_name: string; engine: string;
  admin_username: string | null;
  connection_mode: ConnectionMode;
  run_as_username: string;
  impersonated_role: string | null;
  sql_text: string;               // contraseñas reemplazadas por '***'
  danger_level: DangerLevel;
  statement_count: number;
  status: HistoryStatus;
  read_only: boolean; dry_run: boolean; committed: boolean;
  rows_returned: number; rows_affected: number;
  duration_ms: number;
  error_code: string | null; error_message: string | null;
  created_at: string;
}

// Envelope de error del gateway
interface ApiErrorBody {
  detail: {
    msg: string;
    type: string;
    public_context?: {            // presente en TODOS los ambientes
      database?: string;
      blocked_statements?: { seq: number; sql: string }[];
      reasons?: QueryReasonOut[];
    };
    context?: unknown;            // SOLO en desarrollo — no dependas de esto
    loc?: unknown;                // SOLO en desarrollo
  };
}
```

---

## 13. Matriz de errores

```
200 + success:false            — el MOTOR rechazó una sentencia (permisos, tabla inexistente,
                                 sintaxis). ES UN RESULTADO, no un error. Detalle en
                                 statements[].error
200 + connection_error         — no se pudo conectar con la credencial PROBADA (stored/
                                 provided), o la base no existe / no hay acceso. También
                                 resultado de la prueba
403 — sentencia de nivel `blocked` (no confirmable, no se tocó el motor)
      public_context: { database, blocked_statements: [{seq, sql}], reasons: [{code, message}] }
403 — write/ddl sobre una BD de SISTEMA del motor (code: system_database_write) — solo en
      execute, el preview no lo detecta
404 — connection.mode="stored": el usuario no está en el inventario del servidor
409 — connection.mode="stored": el usuario existe pero el gateway nunca fijó su contraseña
409 — el destino resuelve a la propia base de metadatos del gateway (bloqueo por diseño)
410 — confirm_token expirado (> 2 min desde el preview) → repetir el preview
422 — falta un campo del modo de conexión (username / password / role)
422 — connection.mode="impersonate" en MySQL/MariaDB (no existe en ese motor)
422 — falta confirm_target_name, o no coincide EXACTO con `database`
422 — falta confirm_token cuando el lote no es de solo lectura
422 — confirm_token no corresponde (cambió el SQL, la base, el modo, el usuario, el host o
      el rol entre preview y execute)
422 — SQL vacío o por encima de QUERY_MAX_SQL_BYTES (262144 por default)
429 — rate limit: 30/minute en preview y execute, 60/minute en history
502 — el servidor destino no es alcanzable (o "access denied" en modo admin/impersonate:
      configuración del gateway)
504 — timeout contra el servidor destino
500 — error inesperado del gateway
```

---

## 14. Checklist de implementación

- [ ] **No llamar al preview en cada tecla.** El rate limit es `30/minute` y el preview puede
      tocar el motor (los `COUNT` de estimación). Disparalo con *debounce* generoso o con un
      botón explícito de "Analizar".
- [ ] **Evaluar `blocked` ANTES que `requires_confirmation`.** Un lote bloqueado trae los dos
      en `true` y el token en `null` (ver el 🚨 de [§5](#-blocked-manda-sobre-requires_confirmation)).
- [ ] **Descartar el `confirm_token` ante cualquier cambio** del SQL o del selector de
      identidad. El token está atado a `(hash del SQL, modo, usuario, rol, host)`; conservarlo
      solo produce 422 confusos.
- [ ] **Reenviar el `sql` byte a byte** tal como se envió al preview. No re-formatear, no
      normalizar saltos de línea, no agregar/quitar `;` entre las dos llamadas.
- [ ] **`success: false` no es rojo.** Reservá el rojo de error de sistema para 5xx/502/504.
      Un rechazo del motor es el resultado que el usuario vino a buscar.
- [ ] **`ddl_persisted: true` es la alerta más importante de la pantalla de resultados.** Más
      que `success`, más que `rolled_back`.
- [ ] **`estimated_rows: null` nunca se muestra como "0 filas"** ni habilita un camino más
      corto. Copy sugerido: *"No se pudo estimar cuántas filas afectará."*
- [ ] **`truncated: true` siempre visible.** Un resultado recortado en silencio lleva a
      conclusiones falsas.
- [ ] **`blocked` no se reintenta.** Nada de botón "reintentar" ni de "confirmar igual":
      enlazá al módulo del gateway que sí hace esa operación.
- [ ] **`policy_miss: true` se destaca y se reporta.** Es un bug del gateway y la señal de
      telemetría más valiosa que produce este módulo.
- [ ] **Ocultar `impersonate` cuando el motor no es PostgreSQL** (usá `engine` del preview o
      del servidor seleccionado).
- [ ] **No arrancar en modo `admin`.** Es el default del schema, no debería ser el de la
      pantalla: contradice el propósito del módulo.
- [ ] **Nunca guardar la contraseña del modo `provided`** en almacenamiento del cliente, ni
      recuperarla al "cargar del historial" (el backend tampoco la tiene).
- [ ] **Mostrar el `request_id`** en los errores 5xx, para soporte.
- [ ] **Aislar el mapeo de la respuesta en un solo lugar**: el contrato puede tener ajustes
      menores hasta la validación e2e contra motores reales (ver [§2.8](#28-estado-del-backend-falta-la-validación-contra-motores-reales)).
