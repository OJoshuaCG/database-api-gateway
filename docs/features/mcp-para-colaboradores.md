# Enlazar el MCP del gateway en la máquina de cada persona

Guía operativa. Dos partes: lo que hace **quien administra** (una vez, y después una vez por
persona) y lo que hace **cada colaborador** en su propia máquina.

> **Qué expone hoy.** Tools de solo lectura: el inventario (`list_databases`,
> `list_environments`, `list_exports`, `list_clones`, `list_catalogs`), el de blueprints
> (`list_blueprints`, `list_blueprint_migrations`), las que leen la estructura de una base
> (`list_objects`, `check_freshness`, `get_schema`, `search_schema`, `diff_schemas`,
> `get_table_stats`) y `draft_query`, que **clasifica un texto SQL sin ejecutarlo**. Esas no
> devuelven filas, cuerpos de vistas, rutinas o triggers, ni SQL de migraciones. Los cuerpos
> los entrega solo `get_definition` (scope `data.definitions`, apagada por default; ver «MCP:
> leer el código de vistas, triggers, eventos y rutinas»), el SQL de las migraciones solo
> `get_blueprint_migration` (scope `data.blueprint_sql`, apagada por default; ver «MCP:
> blueprints y el SQL de sus migraciones») y las filas, las tools de datos. Para encontrar una
> tabla o columna cuyo nombre exacto no se conoce, usar `search_schema`.

---

## Parte A — Quien administra: preparar el gateway (una sola vez)

Hace falta una sesión con **`environments.write`**, que solo tiene la capacidad global
`security_officer` — ni el rol `owner` ni `access_admin` la tienen. Sin ningún `security_officer`
asignado no se puede abrir una base a agentes: no hay fallback.

### A.1 Encender el servidor

Nace **apagado**. En el `.env` del gateway:

```bash
MCP_ENABLED=true
```

Y reiniciar. Sin esto, todo request al MCP recibe `503 mcp.disabled`.

Variables opcionales, con sus defaults:

| Variable | Default | Qué hace |
|---|---|---|
| `MCP_TOKEN_MAX_TTL_DAYS` | `90` | Tope de vida de un token. No hay tokens perpetuos |
| `MCP_MAX_OBJECTS` | `500` | Tope de bases por respuesta, evaluado **antes** de consultar |
| `MCP_MAX_BODY_KIB` | `256` | Tope del cuerpo de un mensaje |
| `MCP_RATE_LIMIT` | `120/minute` | Límite de tasa **por token** (no por IP) |

### A.2 Conseguir una sesión para las llamadas siguientes

Los endpoints de administración van con cookie de sesión **y** token CSRF. Con `curl`:

```bash
BASE=https://TU-HOST

# login: guarda las cookies en un archivo
curl -s -c cookies.txt -X POST "$BASE/api/v1/auth/login" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"TU-PASSWORD"}'

# el token CSRF sale de la cookie que el servidor acaba de setear
CSRF=$(awk '/gw_csrf/{print $7}' cookies.txt)
```

De acá en adelante, toda escritura lleva `-b cookies.txt -H "X-CSRF-Token: $CSRF"`.

> **Por qué el header.** Todo `POST`/`PATCH`/`PUT`/`DELETE` de la API lo exige. Y **el token rota
> con la sesión**: si volvés a hacer login, hay que releer la cookie.

### A.3 Agrupar los blueprints en un proyecto

**El alcance de un token es un proyecto.** Un token no puede ser global: sin proyecto no alcanza
ninguna base.

```bash
# crear el proyecto y vincularle blueprints de una vez
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/v1/projects" \
  -d '{"name":"Omnicanal","model_ids":[3,7]}'

# o vincular más después
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/v1/projects/1/blueprints" -d '{"model_ids":[9]}'
```

> **Cuidado con los blueprints compartidos.** Si un blueprint pertenece a **más de un** proyecto,
> sus bases quedan fuera del alcance de **todos** los agentes. Es deliberado y es fail-closed: el
> modelo no tiene forma de expresar *para qué proyecto* se abrió una base, así que ante la duda no
> se entrega. Si necesitás que un agente vea esas bases, el blueprint tiene que estar en un solo
> proyecto.

### A.4 Abrir el acceso: son DOS niveles, y los dos niegan por default

**Nada es visible hasta que se abre explícitamente.** Y hay que abrir en los dos niveles:

```bash
# 1) el ENTORNO. Encenderlo exige repetir el slug: habilita una superficie de lectura
#    nueva sobre bases de terceros, así que cuenta como debilitamiento de la política.
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X PATCH "$BASE/api/v1/environments/2?confirm_slug=development" \
  -d '{"allows_agent_access":true}'

# 2) cada BASE, una por una
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X PUT "$BASE/api/v1/managed-databases/7/agent-access" \
  -d '{"allowed":true,"blocked":false}'
```

> **Por qué base por base y no solo el entorno.** Con solo el flag del entorno, encenderlo
> abriría de golpe **todas** sus bases — incluidas las que nadie revisó y **las que se creen
> después**. El opt-in por base es el eje que decide el alcance; el flag del entorno es la
> condición previa.

`blocked` es el **veto de emergencia**: gana sobre `allowed` y **no tiene override** — ni `force`,
ni nada. Es la palanca para cortar el acceso a una base sin tocar nada más.

Abrir una base se audita **fail-closed**: si el rastro no se puede persistir, la apertura no
ocurre.

### A.5 Las cinco condiciones que tienen que cumplirse

Para que una base aparezca en `list_databases`, **todas**:

1. pertenece al proyecto del token;
2. su blueprint pertenece a **un solo** proyecto;
3. tiene entorno asignado (una base sin clasificar nunca es alcanzable);
4. su entorno tiene `allows_agent_access = true`;
5. la base tiene `agent_access_allowed = true` **y** `agent_access_blocked = false`.

Si falta cualquiera, la base **no aparece** — y el listado **no dice que existe**. Eso es a
propósito: enumerar lo negado sería decirle al agente qué hay del otro lado.

### A.6 La credencial de solo lectura del servidor (para leer estructura)

`list_databases`, `list_environments`, `list_exports`, `list_clones` y `list_catalogs` leen el
inventario del gateway y **no tocan ningún motor**. Las tools que sí leen el catálogo
—`list_objects`, `check_freshness`, `get_schema`, `search_schema` y `diff_schemas`— exigen además una **credencial de SOLO LECTURA** registrada y
**verificada** en el servidor de la base. El MCP **nunca** usa la pseudo-root, ni como fallback.

Hay dos caminos para tener esa credencial. El **automático** (A.6.0) es un click y es el
recomendado; el **manual** (pasos 1 a 3) sigue vigente cuando el DBA no quiere que el gateway use
la pseudo-root para esto o necesita grants distintos.

#### A.6.0 Camino automático: aprovisionar con un click

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -X POST "$BASE/api/v1/servers/3/readonly-credential/provision"
```

Sin cuerpo: `servers.admin` + step-up, 3 por minuto. El gateway usa la **pseudo-root** del servidor
para crear la cuenta, genera una contraseña aleatoria (nunca se devuelve ni se loguea), la guarda
cifrada y corre la sonda negativa del paso 3. La respuesta es el `ServerOut`, con
`readonly_verified_at` cargado. Si la sonda falla: `422 server.readonly_probe_failed`, y el
servidor queda **sin verificar** (fuera del MCP).

- **Usuario y host no se eligen en el request.** Salen de `MCP_READONLY_ACCOUNT_USERNAME`
  (`mcp_ro`) y `MCP_READONLY_ACCOUNT_HOST` (`%`; en producción acotalo a la IP de egreso del
  gateway). En PostgreSQL el host no aplica.
- **Grants fijos, definidos en el servidor** (`readonly_probe.MYSQL_READONLY_*`): `SELECT`,
  `SHOW VIEW`, `TRIGGER`, `EVENT` por base, y `SHOW_ROUTINE` global **solo en MySQL >= 8.0.20**.
  **MariaDB no tiene `SHOW_ROUTINE`**: ahí las rutinas se cubren con `SHOW CREATE ROUTINE` por base
  **solo en MariaDB >= 11.3** (ver «Cuerpos de rutinas» más abajo). MySQL anterior a 8.0.20 y
  MariaDB anterior a 11.3 no reciben ninguno de los dos (tampoco si la versión no se puede leer; el
  detalle de auditoría lo dice) y el MCP no ve cuerpos de rutinas salvo con la bandera de más
  abajo (plan 12 §7.2). La versión se lee ANTES de tocar la cuenta. En
  PostgreSQL: rol `NOSUPERUSER NOCREATEDB NOCREATEROLE NOINHERIT NOREPLICATION NOBYPASSRLS`,
  `default_transaction_read_only = on`, `CONNECT` por base y `USAGE` sobre `public`.
- **Solo rota cuentas PROPIAS.** Si en el motor ya existe una cuenta con ese usuario, el gateway
  la rota únicamente si **este servidor ya guarda una credencial de solo lectura con ese mismo
  usuario** (registrada a mano con `PUT`, o de un aprovisionamiento anterior a medias). Si no, no
  la toca: `409 readonly_account.already_exists`. El operador la registra a mano (`PUT`) o
  configura otro `MCP_READONLY_ACCOUNT_USERNAME`. Rotar la de un tercero le rompería la app.
- **Idempotente y recuperable.** Si la cuenta propia ya existe, rota la contraseña y deja los
  grants exactamente en la lista fija (en MySQL/MariaDB hace `REVOKE ALL` antes de otorgar). Como
  no hay DDL transaccional, el gateway guarda la credencial nueva (sin verificar) **antes** de
  que el motor cambie, y una corrida cortada se reintenta tal cual. Un rol PostgreSQL
  **privilegiado** preexistente con ese nombre se rechaza con `409 engine_user.protected_account`
  (también si es propio): no se lo degrada en silencio. En MySQL/MariaDB, una cuenta propia con
  **roles** otorgados (que `REVOKE ALL` no quita) se rechaza con `409 readonly_account.has_roles`:
  revocalos en el motor y reintentá.
- **Nada cambia si una precondición falla.** Usuario y host configurados, existencia, versión,
  roles y propiedad se comprueban antes de la primera sentencia que muta; los errores de
  configuración (`MCP_READONLY_ACCOUNT_*` inválidos) también se validan al arrancar el gateway.
- **Un aprovisionamiento por servidor a la vez.** Un segundo click mientras corre el primero da
  `409 readonly_provision.in_progress` sin cambiar nada. El lock es del proceso: con varios
  workers no serializa entre ellos (ver el archivo de decisiones e incidentes).
- **Aprovisionar de nuevo invalida la contraseña anterior.** Desde que el gateway guarda la
  nueva, el servidor queda sin verificar (fuera del MCP) hasta que la sonda pase.

> **Alcance: POR SERVIDOR, no por base.** Los `SELECT` se otorgan sobre **todas** las bases no
> internas del servidor en el momento de aprovisionar, incluidas las de otros proyectos. Quedan
> fuera las bases del sistema del motor y la base de metadatos del propio gateway si está
> co-alojada. Nunca `SELECT ON *.*` ni sobre `mysql.*`. Una base creada **después** no queda
> cubierta hasta repetir el aprovisionamiento. El límite entre proyectos lo pone el gate de A.5,
> no el motor. Si necesitás acotar a ciertas bases, usá el camino manual.

> **Solo desde la SPA/API.** El MCP no tiene una tool que provisione ni puede importar esta capa:
> nunca toca la pseudo-root.

#### Cuerpos de rutinas (procedimientos y funciones)

`get_definition` (ver «MCP: leer el código de vistas, triggers, eventos y rutinas») necesita que la
cuenta de solo lectura pueda leer el código de las rutinas. Cómo se logra depende del motor:

- **MariaDB >= 11.3:** regenerar la credencial otorga `SHOW CREATE ROUTINE` por base. Una credencial
  ya aprovisionada no lo recibe hasta regenerarla.
- **MariaDB < 11.3 y MySQL 5.7:** no existe un grant por base; el único camino es `SELECT ON
  mysql.proc`, que se enciende por servidor con la bandera `readonly_proc_grant`
  (`PUT /api/v1/servers/3/readonly-credential/routine-bodies`; en la SPA, la sección colapsada
  «Opciones avanzadas: servidores antiguos»). **Es server-wide:** la cuenta pasa a leer el código de
  las rutinas de **todas** las bases del servidor, también las de otros proyectos; solo el filtrado
  del gateway lo contiene, y por eso habilitarla exige escribir el texto de acknowledgement exacto.
  Detalle y efectos en [`mcp-definiciones.md`](mcp-definiciones.md).

> **Dónde se regenera.** En el panel «Acceso de agentes (MCP)» del **detalle del servidor**, con el
> botón «Regenerar credencial». **No** en el modal «Acceso de agentes» de cada base: ese administra la
> credencial de **datos** (`data.read`/`data.query`), que es otra cuenta y no otorga nada sobre
> rutinas.

#### Camino manual

**1. Crear la cuenta en el motor** (lo hace el DBA del servidor; grants mínimos del plan 12 §7.2):

```sql
-- MySQL 8.0.20+
CREATE USER 'mcp_ro'@'10.0.0.%' IDENTIFIED BY '…';
GRANT SELECT, SHOW VIEW, TRIGGER, EVENT ON `la_base`.* TO 'mcp_ro'@'10.0.0.%';
GRANT SHOW_ROUTINE ON *.* TO 'mcp_ro'@'10.0.0.%';

-- PostgreSQL
CREATE ROLE mcp_ro LOGIN PASSWORD '…' NOSUPERUSER NOCREATEDB NOCREATEROLE
  NOINHERIT NOREPLICATION NOBYPASSRLS;
ALTER ROLE mcp_ro SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE la_base TO mcp_ro;
GRANT USAGE ON SCHEMA public TO mcp_ro;
```

**2. Registrarla en el gateway** (`servers.admin`, con step-up):

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X PUT "$BASE/api/v1/servers/3/readonly-credential" \
  -d '{"username":"mcp_ro","password":"…"}'
```

**3. Verificarla** — la sonda negativa. El gateway conecta con esa cuenta y exige que el motor
observe que **no puede escribir**: en MySQL/MariaDB clasifica `SHOW GRANTS` contra la lista
permitida; en PostgreSQL revisa los atributos del rol, `default_transaction_read_only`, los
privilegios de creación y además **intenta** un `CREATE TEMP TABLE`, que tiene que fallar.

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -X POST "$BASE/api/v1/servers/3/test-connection?credential=readonly"
```

Si pasa, la respuesta trae `readonly_verified_at`. Si no, `422 server.readonly_probe_failed` con
`violations` (por ejemplo `privilege:insert` o `role_attribute:rolsuper`): corregí los grants y
volvé a verificar.

Tres reglas:

- **La verificación vence** a los `MCP_READONLY_MAX_AGE_DAYS` (30 por default). Pasado el plazo,
  las tools que leen el motor niegan con `mcp.readonly_credential_missing` hasta re-verificar.
- **Cambiar la credencial borra la verificación**, y **re-apuntar el servidor** (host, puerto,
  motor o un TLS más débil) **descarta la credencial entera**: nunca viaja a un destino distinto
  del que se verificó.
- **Quitarla** (`DELETE /api/v1/servers/3/readonly-credential`) saca a ese servidor del MCP al
  instante. Es la palanca de emergencia más granular.

> **El límite honesto:** la credencial es **por servidor**, no por base. Alcanza todas las bases de
> ese servidor, incluidas las de otros proyectos. El límite entre proyectos lo pone el gateway (el
> gate de §A.5), no el motor.

---

## Parte B — Quien administra: dar de alta a una persona

### B.1 Emitir un token, uno por máquina

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$BASE/api/v1/api-tokens" \
  -d '{"name":"laptop-de-ana","project_id":1,"expires_in_days":30}'
```

Respuesta (recortada):

```jsonc
{ "data": {
    "id": 4,
    "token_id": "J5uBh8FFa52o3b7xKq2w",
    "token": "datum.J5uBh8FFa52o3b7xKq2w.el-secreto-de-256-bits",  // ← UNA sola vez
    "name": "laptop-de-ana",
    "project_id": 1,
    "scopes": ["blueprints.read"],
    "expires_at": "2026-10-09T12:00:00"
} }
```

Por default el token sale con `blueprints.read`, que alcanza **solo** para `list_databases`,
`list_blueprints` y `list_blueprint_migrations`. Para el resto, pedí los scopes al emitirlo:
`"scopes": ["blueprints.read", "databases.read"]`. El scope `data.blueprint_sql` **nunca** viaja por
default.
`tools/list` publica únicamente las tools que el token puede llamar.

Los tokens nuevos llevan el prefijo `datum.`. Los emitidos antes conservan `dbgw.` y siguen
funcionando: no hace falta reemitirlos por el cambio de nombre.

| Scope | Tools |
|---|---|
| `blueprints.read` | `list_databases`, `list_blueprints`, `list_blueprint_migrations` |
| `data.blueprint_sql` | `get_blueprint_migration` (ver «MCP: blueprints y el SQL de sus migraciones») |
| `databases.read` | `list_objects`, `check_freshness`, `get_schema`, `search_schema`, `draft_query`, `get_table_stats` |
| `data.definitions` | `get_definition` (ver [`mcp-definiciones.md`](mcp-definiciones.md)) |
| `schema_diff.read` | `diff_schemas` |
| `environments.read` | `list_environments` |
| `exports.read` | `list_exports` |
| `clones.read` | `list_clones` |
| `catalogs.read` | `list_catalogs` |

Notas por tool:

- **`list_objects`** acepta `include_column_counts: true` para sumar `column_count` a cada tabla.
  Es el camino caro: con más de `MCP_MAX_OBJECTS_PER_CALL` tablas en el filtro responde
  `mcp.too_many_objects` antes de contar.
- **`check_freshness`** no devuelve estructura: dice la versión que la base declara
  (`applied_version`), si el gateway puede probar que corrió (`trust`: `applied`, `declared` o
  `unknown`) y si hay una aplicación a medias. Una versión igual **no** prueba que el esquema
  siga igual: con `trust` distinto de `applied`, revalidar con `list_objects`.
- **`search_schema(database_id, query, kinds?, limit?)`** busca en la **estructura** de una base
  —nombres de tablas y vistas, nombres de columnas y comentarios de tablas y columnas— cuando el
  agente no sabe el nombre exacto. Nunca lee filas. No necesita privilegios nuevos: usa la misma
  credencial de solo lectura y los mismos grants (`SELECT`, `SHOW VIEW`, `TRIGGER`, `EVENT`) que
  `get_schema`.
  - **Consulta:** `query` de 2 a 100 caracteres (vacía o solo espacios se rechaza con
    `mcp.invalid_argument`). No distingue mayúsculas ni acentos, separa `snake_case`, `camelCase` y
    dígitos, y tolera plurales simples y prefijos (`cli` encuentra `cliente`). Las palabras vacías
    (`de`, `the`) se descartan. **Todas** las palabras tienen que aparecer (AND), repartidas entre el
    nombre y el comentario; una columna no matchea solo por llamarse su tabla como la consulta.
    Funciona igual en español e inglés, también sobre comentarios.
  - **Orden:** nombre exacto > prefijo del nombre > palabras en el nombre (tabla/vista) > columna >
    comentario; el desempate es estable (nombre más corto, tipo, tabla, columna).
  - **`kinds`:** `table`, `view`, `column` (default) y, opt-in, `routine` y `trigger` (solo por
    nombre; el índice no trae más). Las columnas de vistas no se buscan.
  - **`limit`:** default 20, máximo 50. Si hay más coincidencias, `total_matches` las cuenta todas.
  - **Cada resultado** trae `kind`, `name`, `table`, `column`, `data_type`, `key_flags`
    (`primary_key`, `foreign_key`, `unique`), `references` (destino de la FK), `comment`
    (máx. 200 caracteres), `score`, `matched_on` (`name`, `name_and_table`, `comment`,
    `name_and_comment`), `matched_tokens` y `get_schema_object`: el objeto a pasar en `objects` de
    `get_schema` para ver la estructura completa.
  - **Los comentarios son texto de terceros:** salen listados en `untrusted_fields` (y en
    `clipped_fields` si se cortaron), igual que en `get_schema`.
  - **Costo acotado y dicho:** los nombres se buscan siempre; las columnas y los comentarios de
    tabla exigen leer cada tabla, así que se leen hasta `MCP_SEARCH_MAX_TABLES` (200) tablas por
    llamada —las de nombre afín primero— y dentro de la mitad de `MCP_SESSION_MAX_SECONDS`. Si algo
    queda sin leer o recortado, la respuesta trae `truncated: true`, el motivo en
    `truncated_reasons` (`results_limit`, `scan_cap`, `time_budget`), `scanned_tables` de
    `total_tables` y un warning. No hay caché: servir estructura vieja es peor que releerla.
- **`list_catalogs`** devuelve privilegios, charsets/collations habilitados y las plantillas de
  perfil (nivel → privilegios), sin sus descripciones.
- **`draft_query(database_id, sql)`** recibe un texto SQL y devuelve SOLO texto:
  `{classification, reasons, warnings, query_text, touches_engine}`. **No abre ninguna conexión al
  motor, tampoco para una lectura** (`touches_engine` vale siempre `false`): sirve para redactar una
  consulta —o una escritura que va a revisar una persona— y saber de antemano qué clase de
  sentencia es y por qué no sería una lectura aceptable.
  - **`classification`:** `read` (una lectura que el validador acepta), `write`, `ddl`, `blocked` (la
    consola la prohíbe incluso confirmando, o es una lectura que el perfil de agente rechaza) o
    `invalid` (vacía, ilegible, enorme o con varias sentencias).
  - **`reasons`:** códigos cerrados: `PARSE_FAILED`, `MULTIPLE_STATEMENTS`, `NOT_SELECT`,
    `DML_IN_CTE`, `DML_IN_SUBQUERY`, `SELECT_INTO`, `LOCKING_READ`, `FUNCTION_NOT_ALLOWED`,
    `VARIABLE_ASSIGNMENT`, `EXECUTABLE_COMMENT`, `COMMENT_NOT_ALLOWED`, `SYSTEM_SCHEMA`,
    `CROSS_DATABASE`, `UNSUPPORTED_NODE`, `LIMIT_NOT_BOUNDABLE`, `OFFSET_TOO_HIGH` y
    `SQL_TOO_LARGE`. Una escritura o un DDL siempre traen además una advertencia
    (`WRITE_NOT_EXECUTED` / `DDL_NOT_EXECUTED`): **este servidor no los ejecuta**.
  - **`query_text`:** el texto canónico si es una lectura aceptada; en cualquier otro caso, el texto
    recibido, sin caracteres de control y recortado a 16 KiB.
  - **La base la fija `database_id`:** los nombres calificados con otra base (`otra.t`) se rechazan,
    y también los esquemas del sistema (`information_schema`, `mysql`, `pg_catalog`…).
  - **Qué se rechaza aunque sea una lectura:** funciones que no están en la lista blanca (la
    lista es cerrada: `SLEEP`, `GET_LOCK`, `nextval`, funciones propias…), variables y
    asignaciones (`@a := 1`), `FOR UPDATE`/`FOR SHARE`, `SELECT … INTO`, comentarios de cualquier
    tipo (también `/*!…*/` y `/*M!…*/`, que el motor ejecuta) y cualquier `LIMIT`/`OFFSET` que no sea
    un literal entero. **Costo conocido:** en MySQL/MariaDB un literal que lleve una barra invertida
    (`'x\_y'`) o un salto de línea se rechaza (`PARSE_FAILED`), y `a DIV 2` también
    (`UNSUPPORTED_NODE`; alternativa: `FLOOR(a / 2)`).
  - Exige el mismo gate que las demás tools de una base, incluida la credencial de solo lectura
    verificada del servidor, aunque no la use para conectar.

Ninguno muta ni divulga: es un invariante del catálogo que se verifica al arrancar.

Tres reglas que no son burocracia:

- **El `token` se muestra una sola vez.** Lo que se guarda es su HMAC, así que no hay forma de
  volver a mostrarlo. Si se pierde, se emite otro.
- **Uno por máquina o repo**, y el `name` lo dice. Un token compartido entre seis máquinas es un
  token que **nadie revoca**, porque romperlo rompe a los seis.
- **Máximo 90 días.** Un token de agente vive en el `.mcp.json` del repo de otra gente: es la
  credencial con más probabilidad de terminar commiteada.

### B.2 Entregarlo

Por un canal privado (gestor de contraseñas, mensaje directo). **No** por el chat del equipo ni
por correo a una lista.

El `token_id` —la primera parte, después de `datum.` (o de `dbgw.` en los legados)— **no es secreto** y es lo que aparece en la
auditoría: sirve para hablar de "el token de Ana" sin exponer nada.

### B.3 Ampliar o recortar scopes sin reemitir

Los scopes están en el gateway, no dentro del bearer: se cambian con el **mismo token**, sin
redistribuir el secreto ni abrir una terminal nueva. El cambio rige desde la llamada siguiente
del agente. Es el reemplazo completo de la lista, y el `4` es el `id` del listado:

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" -H "Content-Type: application/json" \
  -X PATCH "$BASE/api/v1/api-tokens/4" \
  -d '{"scopes": ["blueprints.read", "databases.read", "schema_diff.read"]}'
```

Pide step-up. Un scope fuera del techo de agente da 422 `api_token.scope_not_allowed`, y un
token revocado, 409. Ampliar un token ya repartido amplía lo que puede hacer quien lo tenga.

### B.4 Revocar

```bash
curl -s -b cookies.txt -H "X-CSRF-Token: $CSRF" \
  -X DELETE "$BASE/api/v1/api-tokens/4"
```

**No se deshace.** Un token que alguien creyó muerto y no lo está es peor que emitir uno nuevo. Si
la persona sigue en el equipo, se le emite otro.

Para ver qué tokens hay, con su último uso:

```bash
curl -s -b cookies.txt "$BASE/api/v1/api-tokens?size=50"
```

---

## Parte C — Cada colaborador, en su máquina

Tres pasos. Necesita: el **host del gateway** y su **token**.

### C.1 Guardar el token en el entorno, nunca en un archivo del repo

**bash / zsh** (`~/.bashrc`, `~/.zshrc`):

```bash
export GATEWAY_MCP_TOKEN='datum.J5uBh8FFa52o3b7xKq2w.el-secreto'
```

### B.2.1 Un token hereda los permisos de quien lo emitió

Un token no es una identidad independiente: es una **delegación** del usuario que lo emitió
(`created_by_admin_id`). Sus capacidades son `scopes ∩ techo de agente ∩ capacidades del emisor`,
y el emisor se relee en cada request:

- Un token **nunca** puede más que su emisor. Si al emisor le quitan una capacidad, el token la
  pierde desde la llamada siguiente (`tools/list` deja de publicar esas tools).
- Si el emisor se **desactiva**, se borra o no existe, el token se rechaza con el mismo
  `401 mcp.token_invalid` de siempre; el motivo (`emisor_inactivo`) queda solo en la auditoría.
- **Los tokens legados con emisor `NULL` quedan rechazados** a partir de este cambio: hay que
  reemitirlos.
- **No restringe el alcance por entorno.** El modelo de roles no tiene denegación por entorno (un
  `viewer` lee todo), así que lo único que acota el destino de un token es su proyecto.

**PowerShell** (perfil, `$PROFILE`):

```powershell
$env:GATEWAY_MCP_TOKEN = 'datum.J5uBh8FFa52o3b7xKq2w.el-secreto'
```

Después, abrir una terminal nueva (o `source ~/.zshrc`).

### C.2 Registrar el servidor

Hay dos formas, y **la elección importa**:

**Opción recomendada — `.mcp.json` del repo, con la variable de entorno.** El repo declara el
servidor una vez, y cada persona pone su propio token en su entorno. Claude Code expande `${VAR}`
en `headers` y en `url`.

**Ese `.mcp.json` va en la raíz del repo que *consume* el MCP**, el de tu equipo — no en el del
gateway. En el repo del gateway no existe ni tiene que existir: ese **sirve** el MCP, no lo usa.
Se crea desde cero; no hay ningún archivo previo que sobrescribir.

```jsonc
// .mcp.json — SE COMMITEA. Por eso va la variable y nunca el literal.
{
  "mcpServers": {
    "gateway": {
      "type": "http",
      "url": "${GATEWAY_URL:-https://gateway.interno}/mcp",
      "headers": { "Authorization": "Bearer ${GATEWAY_MCP_TOKEN}" }
    }
  }
}
```

La primera vez que se abra Claude Code en ese repo, pide **aprobar** el servidor del proyecto. Es
esperado: un `.mcp.json` viene del repositorio y el cliente no lo confía solo.

**Opción alternativa — global en su máquina**, sin tocar ningún repo:

```bash
claude mcp add --transport http --scope user gateway https://gateway.interno/mcp \
  --header 'Authorization: Bearer ${GATEWAY_MCP_TOKEN}'
```

`--scope user` lo deja disponible en **todos** sus proyectos y **no** se commitea. Dos detalles
que muerden:

- **El default de `--scope` es `local`, no `user`.** Omitir el flag ata el servidor a un solo
  directorio y no aparece en ningún otro proyecto.
- **Las comillas tienen que ser simples.** Con dobles, `$GATEWAY_MCP_TOKEN` lo expande el *shell*
  antes de que la CLI lo vea, y el secreto queda **literal** dentro de `~/.claude.json`. Con
  simples se guarda `${GATEWAY_MCP_TOKEN}` tal cual y lo resuelve Claude Code al conectar: scope
  global y token en el entorno, las dos cosas a la vez.

> **Lo que nunca hay que hacer**: poner el token literal en `.mcp.json`. Ese archivo se commitea,
> y el gate de secretos del CI protege el repo del gateway, **no** el de quien consume. Si el
> repo de tu equipo no tiene una regla de escaneo de secretos, conviene agregarla.

### C.3 Comprobar que quedó enlazado

```bash
claude mcp list
```

Buscá `gateway` con `✔ Connected`. Dentro de una sesión de Claude Code, `/mcp` muestra el detalle.

Y para verlo funcionando, pedile en lenguaje natural:

> «Listá las bases de datos que ves por el MCP del gateway»

**Si devuelve una lista vacía, no falló.** Significa que ninguna base tiene el opt-in todavía — el
propio resultado lo dice en su campo `note`. Es el estado normal el primer día.

---

## Si algo no conecta

| Qué ves | Qué pasó | Qué hacer |
|---|---|---|
| `503 mcp.disabled` | El servidor está apagado | `MCP_ENABLED=true` y reiniciar (A.1) |
| `401 mcp.token_invalid` | El token no existe, venció, está revocado o su emisor está inactivo, borrado o sin registrar (token legado) | Es **un solo código para todos**, a propósito. Quien administra lo distingue: en `audit_log`, `action='mcp.auth'` trae el motivo real en `detail` |
| `403` | Mandaste un `Origin` que no está en `CORS_ORIGINS` | Solo pasa desde un navegador. Un cliente MCP no manda `Origin` |
| `400` con `-32020` | Los headers no coinciden con el cuerpo | **No actualices el cliente todavía: el mensaje dice cuál no calza.** Si se queja de que falta `_meta.…/protocolVersion`, el cliente está bien y el servidor está clasificando mal su revisión — reportalo. Recién si el que no calza es `Mcp-Method` o `Mcp-Name`, mirá el cliente |
| `400` con `-32022` | El cliente pide una versión de protocolo que el servidor no habla | La respuesta trae `data.supported` con las que sí |
| `413` | El cuerpo supera `MCP_MAX_BODY_KIB` | No debería pasar con un cliente normal |
| `Missing environment variable: GATEWAY_MCP_TOKEN` | La variable no está en el entorno de esa terminal | Terminal nueva, o `source` del perfil (C.1) |
| `⏸ Pending approval` en `claude mcp list` | El `.mcp.json` del repo no fue aprobado | Abrir Claude Code en ese repo y aceptar |
| La lista viene vacía | Ninguna base cumple las cinco condiciones | Revisar A.4 y A.5. **No es un error** |
| `mcp.scope_denied` | El token no tiene el scope de esa tool | Emitir un token con el scope (B.1) |
| `mcp.readonly_credential_missing` | El servidor no tiene credencial de solo lectura verificada, o venció | A.6: registrarla y verificarla |
| `mcp.not_found` | La base no existe o no es del proyecto del token | Es **el mismo código para los dos**, a propósito |
| `mcp.too_many_objects` | El resultado supera el tope | Acotar con `kinds`/`name_prefix`, o pedir menos objetos por llamada. Nunca se trunca |
| `mcp.session_timeout` | La lectura del catálogo superó `MCP_SESSION_MAX_SECONDS` | Pedir menos objetos por llamada |

Del lado del gateway, todo intento de autenticación deja una fila:

```sql
SELECT created_at, status, detail
FROM audit_log
WHERE action = 'mcp.auth'
ORDER BY id DESC LIMIT 20;
```

El `detail` de un rechazo dice el motivo (`rechazo=inexistente|hmac|revocado|expirado|emisor_inactivo`) y el
`token_id`. **Nunca el secreto.**

---

## MCP data tools: leer filas con topes

`sample_rows`, `distinct_values` y `count_rows` (scope `data.read`) leen **filas** de una base.
Existen solo con `MCP_DATA_READ_ENABLED=true` (apagado por default, se reinicia para cambiarlo) y se
re-chequean en cada llamada. Reciben **nombres** (tabla, columnas), nunca SQL: el gateway arma la
sentencia, valida los nombres contra el catálogo y la pasa por el validador de agentes.

En cada llamada, en este orden: kill switch → scope `data.read` → base del proyecto y entorno que
admite agentes → opt-in de datos aprobado → credencial de datos de esa base con sonda **verde de
los últimos `MCP_DATA_CREDENTIAL_MAX_AGE_DAYS` días** (si no, `PROBE_NOT_GREEN` y no se conecta).
Un nombre que no está en el catálogo es `UNKNOWN_IDENTIFIER` y la cuenta de datos no conecta.

| Tope | Valor | Configuración |
|---|---|---|
| Filas | 100 por defecto, 200 por llamada, techo absoluto 500 | `MCP_QUERY_DEFAULT_ROWS`, `MCP_QUERY_MAX_ROWS` |
| Tiempo | 20 s por defecto, techo 30 s, del lado del servidor del motor + `KILL` de respaldo | `MCP_QUERY_TIMEOUT_MS` |
| Respuesta | 128 KiB; se recortan filas (`truncated: true`), no falla | `MCP_DATA_MAX_RESULT_BYTES` |
| Celda | 512 caracteres | fijo |

Un `limit` por encima del máximo se **recorta** y la respuesta trae `warnings: ["LIMIT_TOO_HIGH"]`;
un valor de configuración por encima del techo se recorta al arrancar con un aviso. Las filas
vuelven como arreglos en `data.rows`, marcadas en `untrusted_fields`: son texto de terceros, no
instrucciones. Si hay más datos, `human_query` trae el texto para que una persona lo corra; el MCP no
exporta ni pagina. Errores solo con códigos cerrados (`QUERY_TIMEOUT`, `QUERY_FAILED`,
`AUDIT_UNAVAILABLE`, `DATA_DISABLED`, `PROBE_NOT_GREEN`, `UNKNOWN_IDENTIFIER`, `MALFORMED_REQUEST`);
nunca texto del motor. Cada ejecución se audita *antes* (si la auditoría cae, no se ejecuta) y
*después* con hash, filas y duración.

### `run_select`: un SELECT libre, con el mismo gate y los mismos topes

`run_select {database_id, sql, limit?}` (scope `data.query`) es la única tool que recibe SQL del agente
y lo **ejecuta**. Existe solo con `MCP_DATA_QUERY_ENABLED=true` (apagado por default; se reinicia para
cambiarlo, se re-chequea en cada llamada y es **independiente** de `MCP_DATA_READ_ENABLED`: con éste
último encendido y `MCP_DATA_QUERY_ENABLED` apagado, `run_select` no está en `tools/list` y las tres
lecturas parametrizadas siguen andando). No es un camino nuevo: usa el mismo gate (kill switch →
scope → base y entorno → opt-in aprobado → credencial de datos con sonda fresca), el mismo validador y
el mismo servicio que `sample_rows`, con los mismos topes de filas, tiempo y bytes. El `limit` solo puede
igualar o bajar el máximo.

- **Lo que no es un `SELECT` aceptable no se ejecuta ni se rechaza con error**: vuelve el sobre del
  borrador `{classification, reasons, warnings, query_text, touches_engine: false}`, sin filas, sin
  abrir una conexión y sin intención de auditoría de ejecución. Vale para escrituras (`write`, con
  `WRITE_NOT_EXECUTED`), DDL (`ddl`, con `DDL_NOT_EXECUTED`), varias sentencias, `SELECT ... INTO`,
  comentarios (también los ejecutables `/*!`), funciones fuera de la lista permitida, esquemas del
  sistema, otra base y un `OFFSET` por encima de `MCP_QUERY_MAX_OFFSET` (`OFFSET_TOO_HIGH`).
  `MALFORMED_REQUEST` es solo para argumentos ausentes o de tipo equivocado.
- **El motor ejecuta el render de `sqlglot` del árbol ya verificado** (`executed_sql`), jamás el texto
  crudo del agente; ese render vuelve a pasar el pipeline completo. Un `LIMIT` propio literal menor o
  igual al tope se respeta; uno mayor o ausente se reemplaza por tope + 1; si no se puede acotar,
  `LIMIT_NOT_BOUNDABLE`. Si el resultado se recorta, `human_query` es la consulta completa del agente
  sin el tope del gateway, y el servidor no la ejecuta.
- **Costo conocido, deliberado:** en MySQL/MariaDB un literal con una barra invertida se rechaza
  (`PARSE_FAILED`, p. ej. `LIKE '%\_%'`), porque su significado cambia con `NO_BACKSLASH_ESCAPES`.

**Riesgo residual ACEPTADO** (para `run_select` y para las tres tools de datos; la barrera real es la
cuenta del motor con `SELECT` sobre una sola base en una transacción `READ ONLY`, no el validador):

1. **Inyección de prompt por los datos de las filas.** Una fila puede contener texto que parezca una
   instrucción. Se mitiga con el sobre (`data.rows` como arreglos, `untrusted_fields`, `notice`,
   caracteres de control fuera, celdas de 512 caracteres), pero es una mitigación de eficacia
   desconocida; lo que la contiene es que **ninguna tool escribe**.
2. **Lo que el análisis del SQL no puede ver:** vistas con `DEFINER`, tablas `FEDERATED`/`CONNECT`/
   `SPIDER` y FDW/`dblink` (la sonda de la credencial bloquea o avisa lo que puede detectar) y las
   diferencias entre cómo `sqlglot` y el motor leen el mismo texto (el render canónico las acota, el
   motor las cierra).
3. **Los datos personales (PII) no se filtran.** La lista de denegación por PII quedó **diferida**
   (enmienda S14 de la spec; `PII_BLOCKED` queda reservado). Lo que `run_select` puede leer lo fija
   el `GRANT` de la cuenta de datos, no una marca de sensibilidad.

---

## MCP: leer el código de vistas, triggers, eventos y rutinas

`get_definition` (scope `data.definitions`) entrega el **código** de hasta 3 objetos por llamada,
pedidos por nombre y tipo. Lo que no existe vuelve en `missing[]`; un cuerpo de más de 64 KiB se
rechaza con `too_large` en vez de cortarse. **No ejecuta nada:** el código se lee como texto
(`SHOW CREATE ...` o `pg_get_*`) y nunca se corre ni se dispara. `get_table_stats` (scope
`databases.read`) es su complemento: tamaño, motor y fechas de las tablas; `row_estimate` y
`auto_increment` salen solo si el token además tiene `data.read`.

Para habilitarlo, las dos cosas:

1. En el `.env` del gateway, `MCP_SCHEMA_DEFINITIONS_ENABLED=true`, y reiniciar. Nace **apagado**: es
   el kill switch, y apagarlo de nuevo retira la tool al instante de `tools/list`.
2. Un token con el scope `data.definitions`. Lo emite solo un owner con el step-up abierto, igual que
   `data.read` y `data.query`.

Los cuerpos pueden contener secretos y son texto de terceros: emitir el scope solo a quien pueda ver el
código de esa base. Si una rutina que existe no aparece, puede ser un tema de privilegios de la
credencial de solo lectura (A.6, «Cuerpos de rutinas»). Contrato, límites y advertencias en
[`mcp-definiciones.md`](mcp-definiciones.md).

---

## MCP: blueprints y el SQL de sus migraciones

Tres tools leen la **BD de metadatos del gateway**; ninguna abre una conexión a un motor ni escribe:

| Tool | Scope | Qué devuelve |
|---|---|---|
| `list_blueprints` | `blueprints.read` | Los blueprints del proyecto del token, por `slug`: id, nombre, descripción, versión actual, si está activo, charset, collation y cantidad de migraciones. Sin SQL. |
| `list_blueprint_migrations` | `blueprints.read` | Una página de las migraciones de UN blueprint, en orden numérico de versión (`0009` antes que `0010`): versión, nombre, tipo, `is_baseline`, `reviewed`, `has_rollback`, motor de origen, `has_procedural_objects`, checksum y fecha. Sin SQL. |
| `get_blueprint_migration` | `data.blueprint_sql` | El SQL de UNA migración: `up_sql`, `down_sql` (rollback confirmado) y `down_sql_suggested`, más los metadatos y `sql_bytes`. |

Las dos listas existen siempre. `list_blueprints` **no pagina**: con más de 100 blueprints visibles
responde `413 mcp.too_many_objects` en vez de entregar una lista cortada.
`list_blueprint_migrations {blueprint_id, after_version?, limit?}` pagina por clave (`limit` de 1 a 200,
100 por defecto): `next_after_version` es la última versión de la página si quedan más, y `total` es la
cantidad completa. Si se renumeran las migraciones entre dos pedidos, una versión puede saltearse o
repetirse.

### Qué blueprints ve un token

Uno es visible **solo si está vinculado al proyecto del token y a ningún otro proyecto**, haya o no una
base alcanzable que lo use. Un blueprint compartido entre dos proyectos no lo ve ninguno de los dos
agentes. Un blueprint ajeno, uno compartido, uno inexistente y una versión que no existe responden
**exactamente igual**: `mcp.not_found` con el mismo mensaje (hay un test que lo compara byte a byte), para
que la respuesta no confirme que el blueprint existe.

### Habilitar `get_blueprint_migration`

Las dos cosas; con una sola, la tool no existe para el agente:

1. En el `.env` del gateway, `MCP_BLUEPRINT_SQL_ENABLED=true`, y reiniciar. Nace **apagado**: es el kill
   switch. Apagado, la tool no está en `tools/list`, el scope queda inerte (se descarta al leer los scopes
   del token) y si se la invoca igual responde `403 mcp.blueprint_sql_disabled`. Es independiente de los
   kill switches de `data.read`, `data.query` y `data.definitions`.
2. Un token con el scope `data.blueprint_sql`. Lo emite solo un owner con el step-up abierto, es sensible
   (el alta suelta pide segundo aprobador) y es agente-grantable: es la cuarta capacidad de la excepción de
   datos (`AGENT_DATA_EXCEPTIONS`, ahora `data.read`, `data.query`, `data.definitions` y
   `data.blueprint_sql`).

### Límites y avisos

- **El SQL es texto de terceros, no confiable.** Una migración de tipo `data` lleva filas semilla; por eso
  trae el aviso `mcp.warn.blueprint_data_migration`. Los tres cuerpos salen en `untrusted_fields`
  (`data.up_sql`, `data.down_sql`, `data.down_sql_suggested`), bajo el `notice` de la respuesta, y **nunca
  se recortan**. El control real es que ninguna tool del MCP escribe.
- **La redacción de credenciales es best effort y no es una frontera.** Enmascara patrones conocidos
  (`IDENTIFIED BY '...'`, `PASSWORD '...'`, URIs con contraseña, bloques PEM, asignaciones a variables que
  delatan un secreto) con el mismo redactor que `get_definition`, y `redactions` cuenta por categoría, nunca
  el valor. Puede dejar pasar un secreto con otra forma: la frontera es el scope `data.blueprint_sql`,
  emitilo solo a quien pueda ver ese SQL.
- **Tamaño.** Si la respuesta no entra en el tope de 512 KiB del MCP (que cuenta el JSON dos veces), la
  llamada falla con `413 mcp.blueprint_sql_too_large` y `details: {sql_bytes, response_bytes,
  max_response_bytes}`, sin ningún cuerpo: un SQL cortado a mitad es peor que ausente.
- **Auditoría fail-closed.** Antes de entregar el SQL se escribe una fila de intención (token y versión,
  nunca un cuerpo); si no se puede escribir, la respuesta es `AUDIT_UNAVAILABLE` y no sale nada.
- Quedan fuera de esta versión `up_sql_mysql`, `up_sql_postgresql`, la traducción por motor y la autoría
  (quién creó la migración).

### El REST y el MCP no usan el mismo scope

Por la API REST, quien tiene `blueprints.read` (incluido un `viewer`) ya lee el SQL de las migraciones.
`data.blueprint_sql` **no cambia eso**: solo controla lo que llega a un agente, porque es texto de terceros
que viaja al contexto de un modelo. Un token de agente con `blueprints.read` ve el inventario y los
metadatos, pero no el SQL.

---

## Cuando alguien se va del equipo

1. **Revocar sus tokens** (B.4). El acceso corta en el request siguiente, sin caché. Desactivar
   su usuario también corta los tokens que EMITIÓ (B.2.1), pero revocar sigue siendo lo explícito.
2. Verificar con `GET /api/v1/api-tokens` que no queda ninguno vivo a su nombre.
3. Su usuario del gateway se **desactiva**, nunca se borra: el username no se reusa jamás, porque
   `audit_log` lo desnormaliza y una persona nueva heredaría la apariencia de las filas viejas.

---

## Lo que este MCP nunca va a hacer

- **Ejecuta únicamente `SELECT` únicos validados, bajo una credencial por base con `SELECT`
  solamente.** Es el invariante de la fase 2 (reemplaza a "no acepta SQL del agente" y a "no EJECUTA
  SQL del agente"): el SQL del agente se ejecuta solo si pasa el validador compartido, dentro de una
  transacción `READ ONLY` y con la cuenta del motor con `SELECT` sobre una sola base. `draft_query`
  clasifica sin abrir conexión; `run_select` ejecuta lecturas y devuelve como texto todo lo demás.
  `sqlglot` no tokeniza los comentarios ejecutables `/*!` de MySQL ni `/*M!` de MariaDB, así que
  todo guard por AST sobre SQL arbitrario es evadible — fue una vulnerabilidad real de la consola
  SQL de este repo — y por eso el validador es defensa en profundidad y no la barrera.
- **Lo que el validador no puede detectar** (y por qué no es la barrera): vistas con `DEFINER`,
  tablas `FEDERATED`/`CONNECT`/`SPIDER` que leen otras bases, y las diferencias entre cómo `sqlglot`
  y el motor leen el mismo texto. Tampoco filtra datos personales (PII: lista diferida, enmienda S14)
  ni impide que el contenido de una fila sea una inyección de prompt. La barrera real es la cuenta
  del motor con `SELECT` sobre **una sola base**, dentro de una transacción `READ ONLY` y con
  timeout del lado del servidor. Ver «`run_select`» más arriba.
- **No escribe nada.** El techo de capacidades de un token excluye todo lo que mute y todo lo que
  divulgue, salvo el conjunto cerrado `data.read`/`data.query` (filas, con opt-in y credencial propios),
  `data.definitions` (código de objetos, solo texto, tras su propio kill switch) y `data.blueprint_sql`
  (SQL de migraciones de blueprints, solo texto, tras su propio kill switch),
  y la intersección se aplica dos veces: al emitir y al autenticar.
- **No usa la credencial pseudo-root.** Las tools que leen el catálogo van con la credencial de
  solo lectura del servidor (A.6), verificada por el motor, y con la sesión en `READ ONLY`.
- **No devuelve cuerpos** de vistas, rutinas ni triggers salvo por `get_definition`, con su scope y su
  kill switch, y aun así solo como texto: nunca los ejecuta. El SQL de las migraciones de blueprints sale
  solo por `get_blueprint_migration` (scope `data.blueprint_sql`, también con su kill switch). En las demás tools salen como
  `body_omitted_reason: "scope_disabled"`; tampoco devuelve SQL de un diff. `diff_schemas` dice QUÉ difiere y nada más; no guarda
  la comparación ni emite un `confirm_token`.

El contrato técnico completo está en `docs/api-reference-v23.md` §9.
