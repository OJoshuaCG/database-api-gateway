# API v39 — Tools de definiciones y estadísticas del MCP: `get_definition`, `get_table_stats`

Addendum de [v38](api-reference-v38.md). Cambio `mcp-schema-definitions`, slices S1 a S8. Las tools
(`get_definition`, `get_table_stats`) son del endpoint `/mcp/`. **La única ruta HTTP nueva** es
`PUT /servers/{id}/readonly-credential/routine-bodies` (S6, ver [su sección](#put-serversidreadonly-credentialroutine-bodies-s6)),
y `ServerOut` gana `readonly_proc_grant`. La SPA (S8) agrega `data.definitions` al selector de scopes
de token y el control de esa bandera en el panel de la credencial de solo lectura.

## Invariante

**El MCP nunca ejecuta un procedimiento, una función, un trigger, un evento ni una vista, y ninguna de
estas tools acepta SQL.** Reciben identificadores (`database_id`, `kind`, `name`), leen el código con
`SHOW CREATE` o `pg_get_*` y lo entregan como texto. Lo que rige para `run_select` (v38) no cambia:
solo `run_select` ejecuta, y solo `SELECT` validados.

## Scope `data.definitions` y kill switch

| Aspecto | Comportamiento implementado |
|---|---|
| Capacidad | `data.definitions`, tercer miembro de la excepción cerrada `AGENT_DATA_EXCEPTIONS` (junto a `data.read` y `data.query`; invariante 13 del catálogo). Nunca se agrega una capacidad que mute |
| Quién la tiene | Solo `owner` (no `operator`: el invariante 7b prohíbe que `operator` divulgue) |
| Sensible | Sí: segundo aprobador al otorgarla suelta (el conjunto sensible pasó de 13 a 14) |
| Step-up | Lo cumple el **emisor humano** al crear o editar el token (`api_token_controller._validate_scopes`); el token no tiene contraseña que reconfirmar |
| Rastro | `api_token.data_scope_grant` con `record_intent` fail-closed, igual que los otros scopes de datos |
| TTL del token | Rige el tope propio `MCP_DATA_TOKEN_MAX_TTL_DAYS` (default `0` = desactivado, el token vive hasta `MCP_TOKEN_MAX_TTL_DAYS`; con `>= 1` se acota, error `422` con `max_days`). Se aplica porque el scope pertenece a `AGENT_DATA_EXCEPTIONS` |
| Agregar a un token ya emitido | Solo quien lo emitió o quien ya tiene hoy el permiso (`_require_editor_may_add_data_scopes`) |
| Kill switch | `MCP_SCHEMA_DEFINITIONS_ENABLED`, **default `false`**, independiente de `MCP_DATA_READ_ENABLED` y `MCP_DATA_QUERY_ENABLED` |

Con el kill switch apagado: el scope queda inerte (`parse_scopes` lo descarta), **`get_definition` no
aparece en `tools/list`** y, si igual se la invoca (el registro se arma al importar y el handler vuelve a
mirar el switch en cada llamada), responde `403` con código `mcp.definitions_disabled`. Es el primer paso:
no valida argumentos ni lee el inventario, así que no se distingue por tiempo ni por efectos de una tool
inexistente. Cambiar el switch exige reiniciar el proceso para que `tools/list` cambie.

## `get_definition`

| Tool | Argumentos | Qué hace |
|---|---|---|
| `get_definition` | `database_id`, `objects: [{kind, name, routine_kind?}]` | Devuelve el texto de vistas, triggers, eventos y rutinas pedidos por nombre |

`kind` admite `view`, `trigger`, `event`, `routine`. `routine_kind` (`PROCEDURE` | `FUNCTION`) solo aplica a
`routine` y desambigua dos rutinas con el mismo nombre. Cada elemento admite únicamente esas tres claves.

**Topes**

- **5 objetos por llamada** (`maxItems` del schema publicado; se vuelve a exigir en el handler y en
  `read_definitions`). Más es `422 mcp.invalid_argument`. Los duplicados se colapsan.
- `name` de 1 a 128 caracteres. No se rechaza por sus caracteres: un nombre con comilla o prefijo de otra
  base no está en el índice y vuelve en `missing`, sin que se emita SQL con ese texto.
- **64 KiB por cuerpo**, medidos sobre su codificación JSON y **después de redactar**. Un cuerpo mayor
  **se rechaza, no se trunca**: vuelve `body_available=false`, `unavailable_reason=too_large` y su
  `size_bytes`. 5 x 64 KiB = 320 KiB entran en el presupuesto de 512 KiB del dispatcher.
- Lo que no está en el índice de la base vuelve en `missing[]` (`{kind, name, routine_kind}`) y **no se
  consulta al motor**. Nunca hay un éxito vacío: o hay objetos, o hay `missing`, o la llamada falló con su
  código.

**Orden del gate** (cada paso corta el siguiente)

1. Kill switch (`mcp.definitions_disabled`, 403).
2. Argumentos (sin conectar; `422 mcp.invalid_argument`).
3. Scope `data.definitions` del token (`mcp.scope_denied`).
4. Proyecto del token (`mcp.not_found`).
5. Credencial de **estructura** (la misma de `list_objects`) fresca, y entorno, opt-in y veto de la base.
   **No pasa por `_data_gate`**: no exige la credencial de datos por base ni su opt-in; el control de acceso
   es el scope más el kill switch.
6. Índice de objetos: lo ausente va a `missing`.
7. Auditoría de intención `mcp.get_definition` con `record_intent` **fail-closed**: si no se puede
   escribir, no se lee ningún cuerpo y el error es `AUDIT_UNAVAILABLE` (503).
8. Lectura por objeto, redacción, medición y huella. Al final, una fila de resultado con conteos por razón.

**Motivos de no disponibilidad** (`unavailable_reason`, vocabulario cerrado; `not_found` no existe a
propósito, eso es `missing`): `insufficient_privilege`, `engine_unsupported`, `scope_disabled`, `flag_off`,
`too_large`. Un cuerpo NULL, vacío o en blanco, o que el saneado deja vacío, es `insufficient_privilege`:
nunca `body_available=true` sin texto (el modelo lo valida al construirse).

**Códigos de error**: `mcp.definitions_disabled` (403), `mcp.invalid_argument` (422), `AUDIT_UNAVAILABLE`
(503), `mcp.scope_denied`, `mcp.not_found`, `mcp.environment_denies_agents` y los demás del gate de
estructura; un timeout de la sesión es el código de sesión del MCP (504, "pedí menos objetos").

### Respuesta

Envelope habitual (`notice`, `data`, `warnings`, `untrusted_fields`, `untrusted_content`, `source`,
`database`). `data` es `{objects[], missing[]}`; cada objeto trae:

`kind`, `name`, `routine_kind`, `identity_arguments` (PostgreSQL: una entrada por sobrecarga),
`body_available`, `unavailable_reason`, `body`, `size_bytes`, `body_fingerprint`, `security`
(`definer` | `invoker`, **solo el modo; la cuenta del DEFINER nunca sale**), `check_option` (vistas),
`trigger {table, timing, events[]}`, `event {schedule, status}`, `redactions[]` y `flagged[]`
(`{category, count}`, jamás el valor).

`body_fingerprint` es SHA-256 del cuerpo redactado y normalizado con la misma `normalize_body` que usa el
diff (sin DEFINER, espacios colapsados, sin `;` final). Advertencias: las de `list_objects` (cuarentena,
estructura no atómica, etc.) más `mcp.warn.bodies_redacted` si se enmascaró algo.

### Contenido no confiable y redacción

Cada `body` va en `untrusted_fields` y bajo el `notice`: es código de un tercero y puede contener texto
que parezca una instrucción ("ignorá lo anterior…"). El control real es que ninguna tool escribe. **La
redacción de credenciales es best effort y NO es una frontera de seguridad.** Enmascara: `IDENTIFIED BY`,
URIs con usuario y clave, asignaciones con nombre de secreto, JWT, llaves de AWS, tokens de GitHub y Slack,
API keys, literales largos tipo token y bloques PEM. Emails y hosts internos solo se **cuentan**
(`flagged`), no se enmascaran. Lo que no matchea un patrón sale en claro: la frontera es el scope.

### Auditoría

`mcp.get_definition` registra la **intención antes de leer** (fail-closed) con `token_id` y la lista
`tipo:nombre` de los objetos que **sí están en el índice** (tope de 2048 bytes). Nunca un cuerpo ni los
nombres ausentes (son texto del agente). La fila de resultado lleva solo conteos: disponibles, ausentes,
redactados y por motivo.

## `list_objects`: `body_available` ahora dice la verdad

Antes `body_available` era siempre `false`. Ahora se calcula con el scope del token y el motor/versión, sin
leer ningún cuerpo ni emitir `SHOW CREATE`:

- Sin `data.definitions` (o con el kill switch apagado): `body_available=false`,
  `unavailable_reason=scope_disabled` en vistas, rutinas, triggers y eventos.
- Con el scope: vistas, triggers y eventos salen disponibles; las rutinas, según la matriz de abajo
  (`flag_off` o `engine_unsupported`). "Disponible" no promete un cuerpo: lo confirma `get_definition` por
  objeto.
- Tablas y secuencias no llevan los campos (`null`).
- Aviso `mcp.warn.routines_not_visible` cuando el motor puede ocultar rutinas al índice: que no aparezcan no
  prueba que no existan.
- **Nuevo tipo `event`** en `kinds` (MySQL y MariaDB; en PostgreSQL no existe y la lista es vacía).
  `get_schema` conserva sus tipos y no lee eventos ni cuerpos.
- Listar nombres de rutinas y triggers ya no hace un `SHOW CREATE` por objeto.

## `get_table_stats`

| Tool | Argumentos | Qué hace |
|---|---|---|
| `get_table_stats` | `database_id`, `tables: [nombre]` | Estadísticas de almacenamiento de tablas pedidas por nombre |

Scope **`databases.read`**, credencial de estructura, **sin kill switch propio** y sin SQL ni lectura de
filas. Lee `information_schema.TABLES` (MySQL/MariaDB) o `pg_class` (PostgreSQL) con consultas
parametrizadas, solo para nombres que están en el índice y acotadas a la base fijada. Hasta
`MCP_MAX_OBJECTS_PER_CALL` tablas por llamada (más es `413 mcp.too_many_objects`, antes de conectar);
`missing[]` para lo que no es una tabla del índice (incluida la contabilidad `_gw_v_`/`_gw_stg_`).

Cada tabla trae `name`, `engine`, `collation`, `data_bytes`, `index_bytes`, `created_at`, `updated_at`.
En PostgreSQL `engine`, `collation` y las fechas son `null` (el motor no los guarda).

**`row_estimate` y `auto_increment` solo salen si el token también tiene `data.read` con
`MCP_DATA_READ_ENABLED` encendido**, porque aproximan `count_rows` y `AUTO_INCREMENT` delata cuántas filas
se insertaron. Para los demás **no existen como claves** (un `null` sería ambiguo con "el motor no lo
sabe"), y el adapter ni siquiera selecciona esas columnas. La respuesta lo dice con
`row_estimates_included` y `row_estimates_omitted_reason: "requires_data_read_scope"`. No exige la
credencial de datos por base: el estimado se lee del catálogo con la de estructura.

## Credencial de estructura y matriz de motor/versión

El código de una **rutina** necesita un privilegio que depende del motor. El aprovisionamiento de la
credencial de estructura agrega, **solo en MariaDB 11.3 o superior** (versión leída en runtime), el
privilegio **`SHOW CREATE ROUTINE` a nivel de base** (MDEV-29167); la sonda lo acepta solo en MariaDB y solo
a nivel base (global o por tabla sigue siendo violación; `SELECT` sobre `mysql.*` sigue prohibido).

| Motor / versión | Cuerpo de rutinas | `unavailable_reason` si falta |
|---|---|---|
| MariaDB 11.3+ | Con `SHOW CREATE ROUTINE` por base (otorgado al aprovisionar) | `insufficient_privilege` |
| MariaDB < 11.3 | Solo con `SELECT ON mysql.proc` (alcance servidor, bandera `readonly_proc_grant`, ver S6) | `flag_off` |
| MySQL 8.0.20+ | `SHOW_ROUTINE` global (ya existente) | `insufficient_privilege` |
| MySQL 8.0.0 a 8.0.19 | No hay grant posible | `engine_unsupported` |
| MySQL 5.7 | Solo con `SELECT ON mysql.proc` (bandera `readonly_proc_grant`, S6) | `flag_off` |
| PostgreSQL | `pg_get_functiondef` y `pg_get_viewdef`, una lectura por sobrecarga; no tiene eventos | `engine_unsupported` (eventos) |

Vistas, triggers y eventos se leen con los grants por base ya existentes (`SELECT`, `SHOW VIEW`,
`TRIGGER`, `EVENT`).

**SIN VERIFICAR EN STAGING**: el nombre exacto y la sintaxis del `GRANT SHOW CREATE ROUTINE` salen de la
documentación de MariaDB, no de un servidor 11.3+ probado. El literal vive en una sola constante
(`MARIADB_SHOW_CREATE_ROUTINE_PRIVILEGE`, `readonly_probe.py`) marcada para confirmar. Ningún test de esta
entrega corrió contra un motor real. Confirmar el `GRANT` y la salida de `SHOW GRANTS` antes de habilitar
`MCP_SCHEMA_DEFINITIONS_ENABLED` en producción. Una credencial ya aprovisionada no recibe el privilegio
hasta que se vuelve a aprovisionar.

## `PUT /servers/{id}/readonly-credential/routine-bodies` (S6)

Enciende o apaga `SELECT ON mysql.proc` para la credencial de solo lectura del MCP en **MariaDB < 11.3 y
MySQL 5.7**, el único camino para leer el código de las rutinas ahí. **Es SERVER-WIDE**: `mysql.proc` no se
puede acotar a una base, así que la credencial lee las rutinas de TODAS las bases del servidor, también las
que están fuera del proyecto o excluidas; solo el filtrado del gateway lo contiene.

| Aspecto | Comportamiento |
|---|---|
| Autorización | `servers.admin` (la dependencia `ServersAdmin`) con **step-up** (lo exige a todo `PUT`; sin él, `403 access.step_up_required`) |
| Límite | `3/minute` por cliente (`429` al excederlo) |
| Cuerpo | `{"enabled": bool, "acknowledgement": str \| null}`; `extra="forbid"` |
| Acknowledgement | Al **habilitar** (`enabled=true`) tiene que ser igual, carácter por carácter y sin `strip`, a `READONLY_PROC_ACK_TEXT` (`app/services/server_catalog.py`). Al deshabilitar se ignora: cortar nunca pide más fricción que abrir |
| Respuesta `200` | `{"server": ServerOut, "engine_grant": "converged" \| "not_alterable" \| "no_credential"}` |

Texto de acknowledgement vigente (la SPA lo duplica literal; si cambia, cambia en los dos lados):

> Entiendo que SELECT ON mysql.proc es server-wide: expone el código de las rutinas de TODAS las bases de
> datos de este servidor, incluidas las que están fuera del proyecto o excluidas, y que solo el filtrado del
> gateway lo contiene.

**Errores (códigos cerrados en `public_context.code`).** La bandera no cambia en ninguno.

| Estado | Código | Cuándo |
|---|---|---|
| `422` | `server.readonly_proc_grant.ack_mismatch` | `enabled=true` sin el texto exacto. No se toca el motor |
| `422` | `server.readonly_proc_grant.engine_unsupported` | `enabled=true` en PostgreSQL, MySQL >= 8.0, MariaDB >= 11.3 o con versión ilegible (se lee con la pseudo-root, sin mutar) |
| `409` | `readonly_provision.in_progress` | Hay un aprovisionamiento de este servidor en curso (el mismo lock por servidor, solo dentro del proceso) |

**`engine_grant`: qué pasó en el motor.**

| Valor | Significa |
|---|---|
| `converged` | La credencial es **propia** del gateway: se re-aprovisionó la cuenta (`REVOKE ALL` y re-grant, con `mysql.proc` solo si la bandera quedó encendida) y la sonda pasó. La bandera de la fila cambia **después** de que el motor confirmó y la sonda pasó |
| `not_alterable` | La credencial se cargó **a mano** (`PUT /servers/{id}/readonly-credential`): el gateway no altera grants de un tercero. Se persiste la bandera y solo se re-corre la sonda. Si el DBA no ajustó los grants en el motor, la sonda falla **sin error HTTP** y el servidor queda sin verificar (`readonly_verified_at` nulo): el MCP cierra |
| `no_credential` | El servidor no tiene credencial de solo lectura: solo cambia la bandera, que aplicará el próximo aprovisionamiento |

**Revocar (`enabled=false`).** Con credencial propia re-converge la cuenta **sin** `mysql.proc`: el
`REVOKE ALL` previo al re-grant lo quita. Re-aprovisionar des-verifica la credencial **antes** de tocar el
motor, así que el corte es inmediato aunque el motor falle; en ese caso la bandera queda como estaba y el
reintento revoca. Con credencial a mano el gateway no revoca nada: el DBA tiene que quitar el grant.

**Sonda con conciencia de la bandera.** Con la bandera apagada, `SELECT ON mysql.proc` sigue siendo
violación (`select_on_mysql_schema`) y la credencial no verifica; solo se tolera esa tabla (nunca
`mysql.*` ni otra tabla de `mysql`) con la bandera encendida **y** un motor que la necesita.

`ServerOut` agrega `readonly_proc_grant: bool` (default `false`). Es un dato de riesgo, no un secreto.

**Migración** `f5b7d9e1a3c6` (`down_revision` `e4a6c8f0b2d5`): agrega `servers.readonly_proc_grant`, booleana
`NOT NULL` con default falso. Idempotente.

**Nota de despliegue — rompe a propósito.** Un servidor MariaDB < 11.3 o MySQL 5.7 cuya credencial de solo
lectura se cargó a mano **con** `SELECT ON mysql.proc` ya otorgado **fallará la sonda después del deploy**
(`select_on_mysql_schema`) y quedará fuera del MCP hasta que un administrador habilite la bandera con el
acknowledgement, o el DBA quite el grant. Es lo buscado: un grant server-wide que nadie aceptó
explícitamente no verifica.

**Auditoría.** `server.readonly_proc_grant.set`, con intención fail-closed antes de tocar nada y registro
final con `touched_engine`. El detalle no lleva el texto del acknowledgement ni credenciales.

## Frontend (S8)

- `data.definitions` entra en el vocabulario de capacidades y en los scopes de datos del selector de
  scopes de token, con el aviso de que los cuerpos son texto de terceros no confiable que puede contener
  secretos. Es owner-only, sensible, con step-up del emisor, exactamente como `data.read` y `data.query`.
- `ReadonlyCredentialPanel` muestra `readonly_proc_grant` y el control para habilitarlo o deshabilitarlo.
  Como `ServerOut` no trae la versión del motor, lo ofrece a toda la familia MySQL/MariaDB y se apoya en
  el `422 engine_unsupported` para el mensaje. Habilitar abre un diálogo con el texto de acknowledgement y
  una casilla; `not_alterable` se muestra como aviso persistente.

## Estado de las slices

S1 a S8 hechas. No queda nada pendiente de este cambio salvo lo que figura como **SIN VERIFICAR** (motor
real, `GRANT SHOW CREATE ROUTINE`).

Variable nueva: `MCP_SCHEMA_DEFINITIONS_ENABLED` (default `false`, documentada en `.env.example`).
