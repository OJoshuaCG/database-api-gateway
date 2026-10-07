# API v41 — Tres particiones de capacidades: `audit.read` + `crypto.rotate`, `engine_users.grant_admin` y `schema.definitions`

Addendum de [v40](api-reference-v40.md). Cada partición separa un deber que una capacidad reunía.
**No hay rutas nuevas**; cambian el guard de algunas rutas, el contenido de algunas respuestas y el
vocabulario que publican `GET /authz/catalog` y `GET /auth/me`. Por qué de cada una, y quién hereda
qué, en `docs/development/decisiones-e-incidentes.md`.

| Parte | Capacidad nueva | Quién la tiene | Cambio visible |
|---|---|---|---|
| A | `audit.read`, `crypto.rotate` (retiran `policy.admin`) | Global `security_officer` | Ninguno para nadie, salvo el enmascarado del SQL en la auditoría |
| C | `engine_users.grant_admin` | Solo `owner` (sensible, step-up) | `operator` pierde WITH GRANT OPTION / privilegios sensibles / `provision` al reasignar dueño |
| B | `schema.definitions` | `operator` y `owner` | `viewer` deja de recibir el código de vistas, rutinas, triggers y eventos |

## A. `policy.admin` se parte en `audit.read` + `crypto.rotate`

`policy.admin` cubría dos cosas que no son el mismo deber: **hacer** (rotar la clave de cifrado) y
**revisar** (leer el rastro). Ahora hay una capacidad por deber, ambas globales y solo de
`security_officer`: **ningún usuario gana ni pierde acceso**. Lo que cambia es que cada una se puede
separar después sin otro cambio de catálogo.

| Campo del catálogo | `audit.read` | `crypto.rotate` |
|---|---|---|
| `scope_axis` | `global` | `global` |
| `mutates` | `false` | `true` |
| `discloses` | `false` (ver el enmascarado) | `false` |
| `requires_step_up` | `false` (un `GET` no lo pide; no divulga) | `true` |
| `agent_allowed` / otorgable / sensible | No / No / No | No / No / No |
| Global que la tiene | `security_officer` | `security_officer` |

| Ruta | Antes | Ahora |
|---|---|---|
| `GET /audit-log`, `GET /audit-log/{id}` | `policy.admin` | `audit.read` |
| `POST /admin/crypto/rotate` | `policy.admin` | `crypto.rotate` |

### `policy.admin` queda retirada

Entra en `RETIRED_CAPABILITIES` junto a `gateway.admin` (invariante 12 del catálogo): no existe en el
enum, ninguna ruta la puede declarar y el catálogo falla al importar si alguien la reintroduce. Es una
capacidad **global**, así que nunca fue otorgable y no puede haber filas en `capability_grants` que la
nombren; una fila legada en `api_tokens.scopes` o un `capability_grants` editado a mano lo descartan los
lectores (capacidad desconocida ⇒ se ignora), igual que a `gateway.admin`.

### `GET /audit-log`: el SQL de la consola sale enmascarado

Las filas de acciones `query_console.*` (hoy, `query_console.execute` con el SQL de la intención previa
al motor) guardan en `detail` hasta 500 caracteres del lote **con sus literales**: datos de negocio del
tercero. El historial de la consola SQL ya los enmascara para quien no tiene `sql_console.execute`;
la auditoría los dejaba salir tal cual, así que `security_officer` leía por ahí lo que el historial le
esconde.

- Para el lector sin `sql_console.execute` **en el destino de la fila** (misma regla de alcance que el
  historial, `can_at`), el SQL de `detail` pasa por `sql_masking.mask_literals` (cada literal → `?`,
  comentarios fuera). Una fila sin servidor se enmascara. `security_officer` nunca ejecuta SQL
  (`sql_console.execute` es de `owner`), así que siempre ve el SQL enmascarado.
- Solo se conserva tal cual el prefijo estructurado de la intención (`<bd> as <usuario> (<modo>) [<peligro>]: `);
  cualquier otro formato de una acción `query_console.*` se enmascara entero (fail-closed).
- `detail_json` se recalcula sobre el texto ya enmascarado.
- Campo nuevo **`detail_masked: boolean`** en cada fila (`false` para todo lo que no es `query_console.*`).
  El backend anterior no lo mandaba: la SPA lo trata como `false` si falta.
- La capacidad **sigue sin divulgar** (`discloses=false`): con el enmascarado no entrega datos del tercero,
  y un `GET` sigue sin pedir step-up.

## Para la SPA (parte A)

- `CAPABILITIES.auditRead` y `CAPABILITIES.cryptoRotate` reemplazan a `policyAdmin`. «Auditoría» (entrada
  de menú y página) se habilita con `audit.read`; la pestaña «Cifrado» de Administración, con `crypto.rotate`.
- `detail_masked` es opcional en el contrato (por defecto `false`); la entrada de auditoría muestra un
  aviso cuando es `true`.

## Código y casos (parte A)

Sin códigos nuevos. Un actor sin la capacidad recibe el `403 access.forbidden` de siempre.

## C. `engine_users.grant_admin`: delegar privilegios del motor

`engine_users.write` cubría el GRANT de rutina y también el que convierte a la cuenta beneficiaria en un
punto de escalada: re-otorgar lo suyo a terceros por fuera del gateway (`WITH GRANT OPTION`) o recibir
privilegios del set GATE. Esa parte pasa a `engine_users.grant_admin`, que se exige **además** de la
capacidad base cuando el payload delega.

| Campo del catálogo | Valor | Por qué |
|---|---|---|
| `module` / `level` | `engine_users` / `grant_admin` | `modulo.accion` (invariante 6) |
| `scope_axis` | `server` | Como el resto de `engine_users.*`; otorgable suelta sobre un entorno o servidor |
| `mutates` / `discloses` / `destructive` | `true` / `false` / `false` | Cambia privilegios del motor; no entrega datos ni borra nada |
| `requires_step_up` | `true` | Abre acceso a terceros |
| Roles | Solo `owner` | `operator` **pierde** estos grants (restricción intencional) |
| Sensible | Sí | Es `owner − operator` y otorgable: segundo aprobador al otorgarla suelta. `_SENSITIVE_POLICY` pasa de 14 a 15 |
| `agent_allowed` | No | Un token no delega privilegios |

### Dónde se exige

| Ruta | Condición | Exige |
|---|---|---|
| `POST /server-users/{id}/grants` | `with_grant_option: true` **o** algún privilegio del set GATE (`ALL PRIVILEGES`, `GRANT OPTION`, `MAINTAIN` en PG…) | `engine_users.write` (guard) + `engine_users.grant_admin` en el servidor del usuario (capa 2) y step-up |
| `POST /server-users/provision` | `initial_grants` con la misma condición | `engine_users.credentials` (guard) + `engine_users.grant_admin` en el servidor. Se verifica **antes** de crear la cuenta |
| `POST /managed-databases/{id}/reassign-owner?provision=true` | siempre que `provision=true` | `databases.write` (guard) + `databases.drop` (como antes) + `engine_users.grant_admin` en la base |

Es la misma condición que ya disparaba la **intención auditada** (`server_user.grant_object` con estado
`attempt`): lo que se audita fail-closed es exactamente lo que ahora exige la capacidad. La verificación va
**antes** de tocar el motor (`can_grant` abre una conexión). Un grant sin `with_grant_option` y sin privilegios
sensibles (`SELECT`, `INSERT`…) no cambia para nadie.

Los endpoints de **perfiles** (`apply-profile`, `apply-profile/.../bulk`) no escalan: las plantillas son datos
de política (`catalogs.write`, `security_officer`), no un payload elegido por quien aplica.

### El 403

`403` con código cerrado **`engine_user.grant_admin_required`** (`app/services/engine_user_catalog.py`), en
`public_context`:

```json
{ "detail": { "msg": "Otorgar con WITH GRANT OPTION requiere la capacidad 'engine_users.grant_admin', además de 'engine_users.write'.",
  "public_context": { "code": "engine_user.grant_admin_required",
                      "required_capability": "engine_users.grant_admin",
                      "reason": "with_grant_option" } } }
```

`reason` es uno de `with_grant_option` | `sensitive_privilege` | `provision_reassign_owner`. **A diferencia del
`access.forbidden` opaco, este 403 nombra la capacidad**: quien llega acá ya pasó el guard de la ruta y eligió
el payload que escala, así que el mensaje no le revela nada nuevo y la SPA puede explicar qué falta. La
denegación deja el mismo rastro agregado (`access.denied`) que el resto. Si en cambio la persona no tiene ni
la capacidad base, el guard de la ruta responde el `access.forbidden` de siempre. El `409`/`422` de siempre
(cuenta protegida, privilegio inválido) no cambian. Con la ventana de step-up cerrada, `403 access.step_up_required`.

### Consecuencias

- **`operator` pierde** `WITH GRANT OPTION` y los privilegios sensibles. En `reassign-owner?provision=true`
  no pierde nada nuevo (ya necesitaba `databases.drop`, solo de `owner`), pero una persona con `databases.drop`
  otorgada suelta ahora necesita también `engine_users.grant_admin`.
- **`owner` conserva todo.** Para que un `operator` lo siga haciendo se le otorga `engine_users.grant_admin`
  suelta sobre un servidor o entorno, con segundo aprobador.

## Para la SPA (parte C)

- `CAPABILITIES.engineUsersGrantAdmin`; `CAPABILITY_ESCALATIONS.grantWithGrantOption`, `grantSensitivePrivilege`
  y `reassignOwnerProvision` (ahora `[databases.drop, engine_users.grant_admin]`).
- `GrantPanel` y los permisos iniciales de `ServerUserForm`: el interruptor `WITH GRANT OPTION` sale
  deshabilitado y los privilegios con `is_sensitive` salen de las opciones, con el motivo a la vista.
- `engine_user.grant_admin_required` se traduce por `reason` (`engine-user-messages.ts`).

## Código y casos (parte C)

| Código | Status | Cuándo |
|---|---|---|
| `engine_user.grant_admin_required` | 403 | El payload delega privilegios y falta `engine_users.grant_admin` |
| `access.step_up_required` | 403 | Ventana de step-up cerrada en un grant que delega |

## B. `schema.definitions`: el código de los objetos de esquema

El snapshot de una base y las comparaciones de esquema devolvían, con la estructura, el CUERPO de vistas,
vistas materializadas, rutinas, triggers y eventos, a cualquiera con `databases.read` / `schema_diff.read`
(o sea, `viewer`). Ese texto es de un tercero (reglas de negocio, literales, a veces secretos). Ahora se
entrega solo con `schema.definitions` **en el destino**; al resto el objeto sale igual, sin cuerpo y con una
marca explícita.

| Campo del catálogo | Valor | Por qué |
|---|---|---|
| `module` / `level` | `schema` / `definitions` | `modulo.accion` (invariante 6) |
| `scope_axis` | `environment` | Como `databases.read`; otorgable suelta sobre un entorno o servidor |
| `mutates` / `destructive` | `false` / `false` | Solo lee |
| `discloses` | **`false`** | Ver abajo: lo fijan los invariantes, no es una afirmación sobre el riesgo |
| `requires_step_up` | `false` | El catálogo solo admite step-up en lo que muta o divulga |
| Roles | `operator`, `owner` | **`viewer` la pierde** (restricción intencional) |
| Sensible | No | Está en `operator`: no es `owner − operator`, así que otorgarla suelta no pide segundo aprobador |
| `agent_allowed` | No | El scope del MCP es `data.definitions`, que **no cambia** |

> **Por qué `discloses=false` aunque el código sea divulgante.** El reparto pedido (`operator` y `owner` sí,
> `viewer` no) choca con el invariante 7b (`operator` no puede tener una capacidad que divulga) y, si se
> marcara `discloses=true`, el 4 exigiría step-up en cada `GET`. Es una decisión forzada y declarada, no una
> clasificación de riesgo: la protección real es la redacción de abajo. Consecuencia que importa: otorgarla
> suelta a un `viewer` **no** pide segundo aprobador (a diferencia de `data.definitions`, que sí).

### Dónde aplica

| Ruta | Guard (sin cambios) | Qué cambia sin `schema.definitions` |
|---|---|---|
| `GET /servers/{id}/databases/{db}/snapshot` | `databases.read` | `statements[*].ddl = ""` y `redacted = true` para `view`, `materialized_view`, `routine`, `trigger`, `event`. Las tablas y el resto salen completos |
| `GET /schema-comparisons/{id}/items` | `schema_diff.read` | Los ítems de esos tipos salen con `sql = ""`, `down_sql = ""` (o `null` si no había) y `redacted = true` |
| `GET /schema-comparisons/{id}/export` | `schema_diff.read` | En el `.sql`, esos objetos llevan la línea `-- [contenido oculto: tu rol no ve el código de este objeto (schema.definitions)]` y no aportan rollback; la auditoría anota cuántos |
| `POST /schema-comparisons/{id}/resolve-selection` | `schema_diff.read` | `added[*].sql = ""` y `redacted = true` para esos tipos |

- **Se evalúa en el origen Y en el destino** de la comparación (los cuerpos salen de los dos lados: `sql` de
  lo nuevo o modificado, del origen; `down_sql`, del destino). Cada lado se resuelve como una base
  (inventariada o cruda) con la capa 2 de siempre: un `operator` solo en desarrollo no ve el código del lado
  de producción.
- **El flag nuevo**: `DumpStatement.redacted` y `SchemaComparisonItemOut.redacted` /
  `ResolveSelectionAddedOut.redacted`, booleanos, `false` por defecto. Un `ddl`/`sql` vacío **nunca** viaja
  sin la marca.
- **Sin cambios**: `execute-preview`, `adopt` y `execute` (son de `schema_diff.execute`: quien puede aplicar
  el DDL ve las sentencias exactas que aplica; hoy `owner` tiene las dos capacidades, y solo una capacidad
  puntual `schema_diff.execute` a un `viewer` abre ese caso), el scope `data.definitions` del MCP y la
  tool `get_definition`.

### Consumidores internos

| Consumidor | Qué recibe | ¿Se rompe? |
|---|---|---|
| `POST /database-models/from-snapshot` (blueprint desde snapshot) | El dump **completo** (`ServerController.snapshot` no redacta; la redacción vive en la capa de ruta) | No. Lo ejecuta quien tiene `blueprints.write`: `operator` y `owner`, que tienen `schema.definitions`. Un `viewer` con `blueprints.write` suelto lo ejecutaría igual y persistiría los cuerpos (ver abajo) |
| Clones, exportaciones, migraciones | No leen el snapshot HTTP ni el diff (usan los adapters) | No |
| El asistente de snapshot / comparación de la SPA | El snapshot y los ítems por HTTP | Un `viewer` ve «Contenido oculto…» en esos objetos; no puede crear blueprints de todos modos (`blueprints.write`) |

### Lo que esta capacidad NO cierra

- Las **versiones de blueprint** (`blueprints.read`, `viewer`) guardan el DDL completo que alguien creó desde
  un snapshot o adoptó de un diff: un `viewer` las lee. Cerrar eso es otro corte (la autoría de blueprints),
  no este.
- `execute-preview`/`adopt` de quien tiene `schema_diff.execute` sin `schema.definitions` (solo posible con
  capacidades puntuales) ven los cuerpos que van a aplicar.

## Para la SPA (parte B)

- `CAPABILITIES.schemaDefinitions`; los contratos `DumpStatement`, `SchemaComparisonItem` y
  `ResolveSelectionAdded` aceptan `redacted` opcional (falso por defecto).
- El snapshot y las comparaciones muestran «Contenido oculto: tu rol no ve el código de este objeto» en los
  objetos con `redacted = true`; el `.sql` exportado trae la línea de comentario equivalente.

## Código y casos (parte B)

Sin códigos nuevos: no hay un 403 (el guard de las rutas es el de estructura). La ausencia de la capacidad se
expresa en el payload (`redacted`).
