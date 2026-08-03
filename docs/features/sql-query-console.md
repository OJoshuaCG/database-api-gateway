# Consola SQL — ejecutar queries ad-hoc en modo seguro

Ejecuta SQL arbitrario contra una base de datos de cualquier servidor del inventario,
**con el usuario del motor que elijas**, para verificar permisos reales sobre tablas y
bases. Corre en un **modo seguro** donde toda sentencia que no sea lectura pura exige una
confirmación explícita, y donde un conjunto de sentencias está prohibido incluso
confirmando.

- **Rutas**: `POST /servers/{id}/query/preview`, `POST /servers/{id}/query/execute`,
  `GET /servers/{id}/query/history`
- **Controller**: `app/controllers/query_console_controller.py`
- **Servicios**: `app/services/db_admin/query_policy.py` (puro),
  `app/services/db_admin/query_runner.py` (ejecución)
- **Modelo**: `app/models/query_execution.py` — migración `a3b4c5d6e7f8`
- **Schemas**: `app/schemas/query_console.py`

---

## 1. Los cuatro niveles de peligro

La política clasifica **cada sentencia** del lote y el nivel del lote es el **máximo** de
sus sentencias. Una confirmación cubre el lote entero.

| Nivel | Qué incluye | Qué hace el gateway |
|---|---|---|
| `read` | `SELECT`, `SHOW`, `DESCRIBE`, `EXPLAIN` (sin `ANALYZE`) | Ejecuta directo, **dentro de una transacción de solo lectura** |
| `write` | `INSERT`, `UPDATE`, `DELETE`, `MERGE`, `SELECT … FOR UPDATE`, `SET` de sesión | Exige `confirm_target_name` + `confirm_token` |
| `ddl` | `CREATE`, `ALTER`, `DROP`, `TRUNCATE`, `RENAME`, `ANALYZE`, y **todo lo opaco** | Exige `confirm_target_name` + `confirm_token` |
| `blocked` | Ver §3 | **403 sin tocar el motor**, ni con confirmación |

El requisito operativo se cumple literalmente: un `UPDATE` o un `DELETE` piden
confirmación **tengan o no cláusula `WHERE`**. Lo mismo `ALTER`, `DROP` y `TRUNCATE`.

---

## 2. Por qué no se filtra por palabras clave

Buscar `"DELETE"` en el texto falla en las dos direcciones, y las dos importan:

```sql
-- Falsos positivos: no modifican nada, pero un filtro por texto los marcaría
SELECT * FROM logs WHERE accion = 'DELETE';
SELECT * FROM t -- DROP TABLE u

-- Falsos negativos: SÍ modifican, y un filtro por texto los dejaría pasar
/*x*/ dElEtE FROM t;
WITH d AS (DELETE FROM t RETURNING *) SELECT * FROM d;   -- la raíz es un SELECT
CALL sp_borrar_todo();                                    -- una sola palabra
EXPLAIN ANALYZE DELETE FROM t;                            -- ANALYZE ejecuta de verdad
/*!40101 GRANT ALL ON *.* TO 'x'@'%' */                   -- MySQL ejecuta ese "comentario"
```

En su lugar se parsea con **sqlglot** y se toma el peligro máximo de **cualquier nodo del
árbol**, no solo de la raíz. Sobre eso, dos reglas:

- **Fail-closed**: si el SQL no parsea, si el tipo de sentencia no está mapeado, o si es
  opaca (`CALL`, `DO`, bloque anónimo, `PREPARE`/`EXECUTE`), el resultado es **peligroso**,
  nunca lectura.
- **La blocklist corre sobre el texto normalizado y su veredicto es terminal.** No es
  redundante con el AST: `FLUSH PRIVILEGES` parsea como una expresión inofensiva
  (`exp.Alias`) y todo el DCL degrada a un nodo genérico sin estructura que inspeccionar.
  El texto se normaliza quitando comentarios y **vaciando el contenido de los literales**,
  para que `WHERE accion = 'GRANT'` no bloquee y `/*x*/GRANT` no evada.

### Solo lectura lo garantiza el motor, no el parser

Ninguna clasificación estática puede saber si `SELECT mi_funcion()` escribe. Por eso el
nivel `read` **siempre** se ejecuta dentro de una transacción de solo lectura
(`START TRANSACTION READ ONLY` en MySQL/MariaDB, `SET TRANSACTION READ ONLY` en
PostgreSQL). Si la clasificación se equivocara, el motor aborta la sentencia.

---

## 3. Sentencias prohibidas (no se ejecutan ni confirmando)

Estas devuelven **403 sin tocar el motor** mientras no exista un segundo factor. La lista
va más allá de lo destructivo obvio, porque el gateway conecta con una credencial
pseudo-root: el motor **sí** permitiría todo esto.

| Código | Qué cubre | Por qué |
|---|---|---|
| `dcl_grant_revoke`, `dcl_user_role` | `GRANT`, `REVOKE`, `CREATE/ALTER/DROP USER\|ROLE`, `SET PASSWORD`, `RENAME USER` | Evita el módulo de permisos, sus guards anti-lockout y su auditoría estructurada |
| `server_file_access`, `copy_statement` | `COPY … FROM/TO PROGRAM`, `INTO OUTFILE`, `LOAD DATA`, `LOAD_FILE()`, `pg_read_file()`, `lo_import/lo_export` | Lectura/escritura de archivos en el host de la BD; `FROM PROGRAM` es ejecución de comandos |
| `extension_or_untrusted_language` | `CREATE EXTENSION`, rutinas en `C`/`plpython3u`/`plperlu`/`pltclu` | Ejecución de código arbitrario en el host |
| `server_global_state` | `SET GLOBAL/PERSIST`, `ALTER SYSTEM`, `FLUSH`, `KILL`, `SHUTDOWN`, `RESET`, replicación, plugins, tablespaces | Afecta a **todas** las bases del servidor, no solo a la elegida |
| `database_lifecycle` | `CREATE/DROP DATABASE`, `CREATE/DROP SCHEMA` | Ya tienen endpoint dedicado con doble confirmación y guard de BDs de sistema |
| `session_control` | `BEGIN`, `COMMIT`, `ROLLBACK`, `SAVEPOINT`, `SET TRANSACTION`, `SET autocommit`, `LOCK TABLES`, `USE`, `HANDLER` | Romperían el envoltorio transaccional del runner, incluida la garantía de solo lectura |
| `role_switch` | `SET [LOCAL\|SESSION] ROLE`, `RESET ROLE`, `SET [LOCAL] SESSION AUTHORIZATION`, `set_config()`, `DISCARD` | Anularían el usuario elegido para la prueba. Un superusuario de PostgreSQL puede volver a serlo con **cualquiera** de estas formas; `set_config` se bloquea entera porque los literales llegan vaciados y su primer argumento es indistinguible |
| `dynamic_sql` | `PREPARE`, `EXECUTE`, `DEALLOCATE` | Ejecutarían texto que la política nunca llegó a clasificar |
| `system_schema_write` | Escritura sobre `mysql.*`, `pg_catalog`, `information_schema`, `sys`, `pg_temp*` | **Leerlos está permitido** (es parte de probar permisos); escribirlos corrompe el servidor. Se escanea también la variante sin comillas: con backticks (`` `mysql`.`user` ``) el patrón no matcheaba |
| `gateway_internal_table` | `_gw_v_*`, `_gw_stg_*` | Contabilidad de versiones de Alembic dentro de cada BD gestionada |
| `native_code_load` | `CREATE [AGGREGATE] FUNCTION … SONAME`, `INSTALL/UNINSTALL SONAME`, `CREATE LANGUAGE` | La UDF de MySQL/MariaDB carga una **librería nativa**: es la vía clásica de ejecución de comandos del SO. El equivalente de PostgreSQL (`LANGUAGE C`) ya estaba cubierto |
| `outbound_connection` | `CREATE/ALTER/DROP SERVER`, FDW, `IMPORT FOREIGN SCHEMA`, `CREATE PUBLICATION/SUBSCRIPTION`, `dblink()`, `ENGINE=FEDERATED` | Hacen que **el motor** abra una conexión saliente, que no pasa por el guard anti-SSRF del gateway |
| `server_control_function` | `pg_terminate_backend()`, `pg_reload_conf()`, `pg_promote()`, slots de replicación | Administran el servidor entero. Viajan dentro de un `SELECT`, así que salían como `read`, y una transacción de solo lectura no las frena |
| `session_guarantee_override` | `SET statement_timeout`, `SET search_path`, `SET @@GLOBAL.x`, `SET STATEMENT … FOR`, `foreign_key_checks`, `XA` | Cambian los parámetros de sesión que **sostienen las garantías** de la consola. `search_path` es el peor: el `COUNT` del preview corre en otra conexión, así que contaría `public.t` mientras el `DELETE` confirmado golpea `otro.t` |
| `delimiter_directive` | `DELIMITER` | Ver §3.1 |

### 3.1 Por qué se rechaza `DELIMITER`

`DELIMITER` es una directiva del **cliente** `mysql`, no del motor. Aquí su único efecto
era agrupar varias sentencias del servidor en una sola unidad del splitter — y con eso el
keyword peligroso dejaba de estar al principio del texto, evadiendo **toda** la blocklist
anclada:

```sql
DELIMITER //
UPDATE t SET x = 1; GRANT ALL ON SCHEMA public TO evil //
```

Eso se clasificaba `write` (confirmable) en vez de `blocked`. Lo mismo con el comentario
ejecutable `SELECT 1/*!;DROP DATABASE victima*/`, que salía `read`. Hoy la blocklist se
aplica al texto completo **y a cada segmento entre `;`**, y `DELIMITER` se rechaza.

No se pierde funcionalidad: el splitter reconoce los cuerpos `BEGIN…END` de las rutinas
por sí solo, así que un `CREATE PROCEDURE` se envía sin la directiva.

Efecto colateral aceptado: el cuerpo de una rutina que contenga `COMMIT;` o
`START TRANSACTION;` queda bloqueado. Es deliberado — crear rutinas con control de
transacción o DCL adentro desde una consola ad-hoc es justamente la escalada que la
blocklist corta (crear la rutina y después llamarla).

Además, **la consola nunca puede apuntar a la propia base de metadatos del gateway**: se
compara resolviendo **ambos hosts a IPs**, no por texto. Comparar strings dejaba pasar
cualquier grafía equivalente: con la base del gateway en el host `db` de un compose,
bastaba registrar un servidor apuntando a `172.18.0.2` (la misma máquina) para evadirlo, y
esa IP privada además pasa el filtro anti-SSRF por diseño.

---

## 4. Elegir con qué usuario se ejecuta

`connection.mode` es parte del request, porque elegir el usuario *es* la funcionalidad.

| Modo | Cómo se conecta | Cuándo usarlo |
|---|---|---|
| `admin` | Credencial pseudo-root del servidor | Operar, **no** probar permisos. La respuesta trae un `warning` explícito |
| `stored` | Usuario del inventario cuya contraseña fijó el gateway (Fernet) | El gateway creó o rotó esa contraseña |
| `provided` | Contraseña enviada en el request, **nunca persistida** | Un usuario que el gateway no creó (el caso más común) |
| `impersonate` | Conecta como pseudo-root y emite `SET ROLE` | **Solo PostgreSQL**: probar un rol sin conocer su contraseña |

`impersonate` devuelve **422 en MySQL/MariaDB**: su `SET ROLE` solo alcanza roles ya
otorgados al usuario actual, así que no hay forma de adoptar una identidad arbitraria sin
credencial. Es una diferencia de motor, no una limitación del gateway.

> **El usuario forma parte de la identidad de la conexión.** El cache de engines
> (`remote_engine`) incluye el usuario en su clave. Sin eso —el estado anterior a esta
> feature— una prueba pedida como usuario limitado habría reusado el engine pseudo-root y
> habría dado verde siempre.

---

## 5. Flujo

### Paso 1 — `POST /servers/{id}/query/preview`

```json
{
  "database": "tienda",
  "sql": "UPDATE pedidos SET estado = 'cancelado' WHERE creado_en < '2025-01-01'",
  "connection": { "mode": "provided", "username": "app_rw", "password": "…" }
}
```

Responde con la clasificación, los motivos, **cuántas filas afectaría** y el token:

```json
{
  "danger": "write",
  "requires_confirmation": true,
  "blocked": false,
  "statements": [
    { "seq": 0, "kind": "update", "danger": "write", "estimated_rows": 2481902, "reasons": [] }
  ],
  "confirm_token": "1754131200.9f3c…",
  "expires_at": "2026-08-02T12:02:00Z"
}
```

`estimated_rows` sale de un `SELECT COUNT(*)` derivado del AST con **el mismo `WHERE`**,
ejecutado con **la misma credencial**. Es la cifra que hace evidente un `WHERE` olvidado
antes de confirmar. Vale `null` cuando el conteo no sería exacto (varias tablas, `JOIN`,
`USING`/`FROM` de PostgreSQL) o cuando el usuario no puede leer la tabla — y en ese caso
la confirmación se exige igual, solo que sin cifra.

### Paso 2 — `POST /servers/{id}/query/execute`

```json
{
  "database": "tienda",
  "sql": "UPDATE pedidos SET estado = 'cancelado' WHERE creado_en < '2025-01-01'",
  "connection": { "mode": "provided", "username": "app_rw", "password": "…" },
  "confirm_target_name": "tienda",
  "confirm_token": "1754131200.9f3c…"
}
```

Confirmación de **doble factor**, igual que el `DROP DATABASE`: el nombre obliga a
identificar cuál base se toca; el token da frescura y anti-replay. Con una diferencia
importante: **el token se ata también al hash del SQL y al usuario elegido**. Sin eso se
podría previsualizar un `SELECT` y canjear el token para ejecutar un `DROP`.

Una consulta de solo lectura no necesita ninguno de los dos campos.

### `dry_run`

`dry_run: true` ejecuta y **revierte**, devolviendo `rows_affected` real. Atención: el DDL
de MySQL/MariaDB hace `COMMIT` implícito y **no** se revierte; la respuesta incluye el
aviso.

---

## 6. Un rechazo del motor es un resultado, no un error de la API

```json
{
  "success": false,
  "statements": [{
    "seq": 0, "success": false,
    "error": { "code": "1142", "message": "SELECT command denied to user 'app_ro'@'%' for table 'pagos'" }
  }]
}
```

**HTTP 200.** Una query denegada es una prueba exitosa: confirma que el permiso no está.
Por eso la consola no usa el traductor `map_driver_error`, que convierte 1142/42501 en un
403 genérico y oculta justo el mensaje del motor que se quiere leer. Lo mismo para una
credencial inválida o una base inexistente, que llegan en `connection_error`.

Los códigos HTTP quedan para lo que sí es un problema del gateway: **502** host
inalcanzable, **504** timeout de conexión, **403** sentencia prohibida, **422**
confirmación faltante o inválida, **409** destino inválido, **410** token expirado.

---

## 7. Topes y auditoría

| Variable | Default | Qué protege |
|---|---|---|
| `QUERY_SAFE_MODE` | `True` | Ver §8 |
| `QUERY_MAX_ROWS` | `1000` | Ver §7.1 |
| `QUERY_TIMEOUT_MS` | `30000` | El interactivo general (15 s) es corto para una consola |
| `QUERY_MAX_TIMEOUT_MS` | `300000` | Techo de lo que un request puede pedir |
| `QUERY_MAX_SQL_BYTES` | `262144` | Tamaño del SQL aceptado |
| `QUERY_MAX_CELL_CHARS` | `4096` | Una celda `BLOB`/`TEXT` se recorta con marca |
| `QUERY_HISTORY_SQL_MAX_CHARS` | `16384` | Tamaño del SQL persistido |

### 7.1 El tope de filas se empuja al MOTOR

Recortar del lado del gateway (`fetchmany`) acota la memoria pero **no el transporte**: en
MySQL/MariaDB, cerrar un cursor sin agotarlo dispara `MySQLResult._finish_unbuffered_query()`,
que —según el comentario del propio pymysql— gira leyendo paquetes hasta el EOF *porque no
hay forma de que el servidor deje de mandarlos*. Un `SELECT * FROM tabla_de_50M` con tope
de 1000 filas transfería igual las 50M.

Por eso la política emite un `fetch_sql` con el `LIMIT` incorporado (una fila de más, para
informar `truncated` con certeza) y el runner ejecuta **ese** SQL, que además se devuelve en
`statements[].sql` para no mentir sobre qué corrió. No se acota cuando cambiaría la
semántica: `FOR UPDATE` (el `LIMIT` cambia qué filas se bloquean), `SELECT … INTO`, un
`LIMIT` propio más chico, o cualquier cosa que no sea `SELECT`/`UNION`.

**No se usa `stream_results`.** Fijarlo a nivel de conexión hacía que SQLAlchemy enrutara
*toda* sentencia por un cursor con nombre, y en psycopg eso se compone como
`DECLARE … CURSOR FOR <sentencia>` — gramática que solo acepta consultas. Con eso, **toda**
ejecución contra PostgreSQL moría en la primera línea de preparación de sesión
(`SET TRANSACTION READ ONLY`), y el error salía además como un 502 "no se pudo conectar".

**Auditoría**: `audit.record_intent` **fail-closed** antes de tocar el motor para todo lo
que no sea lectura (si no se puede auditar, no se ejecuta), y `query_console.blocked` para
los intentos rechazados. **Historial**: tabla propia `query_executions`, que **no** guarda
las filas devueltas (son datos del usuario final, el gateway no es su custodio) sino solo
conteos, y que pasa el SQL por `redact_secrets` para que un `IDENTIFIED BY 'x'` no quede
en claro.

---

## 8. El día que exista 2FA

`QUERY_SAFE_MODE=False` salta la confirmación de `write`/`ddl` — y **nada más**. La lista
de sentencias prohibidas de §3 no depende de esa variable: seguirá bloqueando. El camino
previsto es que un segundo factor habilite `blocked` caso por caso, no que se apague el
modo seguro entero.

---

## 9. Estado de verificación

Tras la implementación se corrió una **segunda pasada adversarial** (revisión de seguridad,
ingeniería, paridad cross-engine y cobertura). Encontró 4 bloqueantes —uno de ellos hacía
que el módulo **no funcionara en absoluto en PostgreSQL**— y una decena de hallazgos
menores, todos corregidos. Lo más relevante quedó documentado arriba: §3 (categorías
nuevas de prohibidos), §3.1 (`DELIMITER` y el guard de la BD del gateway por IP) y §7.1
(el tope de filas y por qué no se usa `stream_results`).

Verificado **sin motores reales** (ejecución directa de funciones y dobles de conexión,
sin `pytest` por política del proyecto):

- **Política** (`query_policy`): 101 casos, incluidos los ataques concretos de la revisión
  (bundling con `DELIMITER`, `#` como XOR en PostgreSQL, `set_config('role')`, UDF por
  `SONAME`, `dblink`, `pg_terminate_backend`, `SET search_path`, esquemas de sistema con
  backticks). `tests/test_query_policy.py`.
- **Invariantes de seguridad**: token atado al SQL/usuario/host/base, separación del cache
  de engines por usuario **y contraseña**, clasificación de errores según el modo de
  conexión, serialización de valores. `tests/test_query_console_security.py`.
- **Ejecución** (`query_runner`): 20 casos con doble de conexión — orden de preparación de
  sesión, stop-on-error, commit vs rollback, `LIMIT` empujado, savepoint por conteo,
  `policy_miss`. `tests/test_query_runner_execution.py`.
- **API** (`query_console_controller`): 24 casos de extremo a extremo con el runner
  mockeado — confirmación, tokens cruzados, 403 sin tocar el motor, modos de conexión,
  historial. `tests/test_api_query_console.py`.
- **Migración** `a3b4c5d6e7f8`: ciclo `upgrade`/`downgrade`/`upgrade` contra SQLite.

### PENDIENTE — verificación e2e contra motores reales

`scripts/verify_query_console_e2e.py` está **escrito pero NO ejecutado** (no hay Docker en
el entorno de desarrollo). Es lo que falta para poder afirmar que el módulo está verificado
de punta a punta, y cubre:

1. Que la transacción READ ONLY **rechace de verdad** una escritura que la clasificación
   dejó pasar como lectura (`SELECT fn_que_escribe()`), con verificación **física** de que
   la fila no se insertó — es la garantía central del diseño.
2. Que `SET ROLE` aplique los permisos del rol en PostgreSQL, incluida RLS.
3. Que el tope de filas no baje la tabla entera (tabla de 200k filas; se mide el tamaño de
   la respuesta y el tiempo).
4. El mensaje nativo exacto de un rechazo por permisos en los tres motores.
5. Que un `GRANT` bloqueado no haya creado el usuario en `mysql.user`.
6. Que `dry_run` + DDL deje la tabla creada en MySQL/MariaDB (commit implícito) y **no** en
   PostgreSQL.

Sin esa corrida, el módulo está listo en cuanto a **lógica de decisión** (qué se ejecuta,
con qué confirmación, con qué usuario), pero no se debe declarar verificado contra motores
reales.
