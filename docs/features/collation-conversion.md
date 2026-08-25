# Conversión de charset/collation de una base de datos

Cambia el charset/collation de una BD y, a diferencia de las herramientas gráficas
habituales, **también recrea los objetos que arrastran la collation vieja congelada**:
PROCEDURE, FUNCTION, TRIGGER, EVENT y VIEW.

**Son DOS operaciones distintas bajo el mismo recurso**, y el modo lo decide el **motor**
(el usuario no lo elige):

| Modo | Motor | Qué cambia | Qué NO existe en ese modo |
|---|---|---|---|
| `universal` | MySQL / MariaDB | `ALTER DATABASE` + `CONVERT TO CHARACTER SET` por tabla + DROP/CREATE de los 5 tipos de objeto congelados | — |
| `columns` | PostgreSQL | Solo `ALTER TABLE ... ALTER COLUMN ... TYPE ... COLLATE ...` | `ALTER DATABASE` (imposible) y recreación de objetos (innecesaria) |

El resto de este documento describe el modo `universal`; el modo `columns` tiene su propia
sección ([Modo `columns`: PostgreSQL](#modo-columns-postgresql)).

## El problema (y el bug que esto corrige)

Cambiar el charset/collation de una base de datos y de sus tablas **no alcanza**. En
MySQL/MariaDB, cuando se crea uno de esos cinco objetos, el motor **congela** en él la
`collation_connection` de la sesión que lo creó. Esa collation congelada es la que heredan:

- los parámetros `VARCHAR`/`CHAR` de una **PROCEDURE**/**FUNCTION**,
- las variables `DECLARE` de un **TRIGGER**/**EVENT**,
- los **literales de texto** embebidos en el cuerpo de una **VIEW**.

Si después se cambia la collation de la BD y de las tablas sin recrear esos objetos, quedan
comparando texto en dos collations distintas y en producción aparece el clásico:

```
ERROR 1267 (HY000): Illegal mix of collations (utf8mb4_general_ci,IMPLICIT)
and (utf8mb4_unicode_ci,IMPLICIT) for operation '='
```

La documentación oficial de MySQL lo dice sin rodeos en la página de `ALTER DATABASE`:

> *"If you change the default character set or collation for a database, any stored routines
> that are to use the new defaults must be dropped and recreated."*

**No existe** un `ALTER PROCEDURE`/`ALTER TRIGGER` que cambie el cuerpo ni la collation de
sus parámetros. La única forma de arreglar un objeto ya creado es **`DROP` + `CREATE` con el
mismo cuerpo**, ejecutado en una sesión que ya tenga la collation objetivo.

**HeidiSQL tiene exactamente este bug**: ofrece "cambiar la collation de toda la BD" pero
convierte la BD y las tablas y deja las rutinas intactas. Ese es el hueco que este feature
cierra.

## Por qué PostgreSQL no usa el modo `universal`

Los dos pasos centrales del modo `universal` son **imposibles o innecesarios** en PostgreSQL:

- PostgreSQL **resuelve la collation dinámicamente**, en cada ejecución y contra el tipo real
  de la columna en ese momento. Una función o vista no congela la collation de la sesión que
  la creó, así que no hay nada que "refrescar" con un DROP+CREATE → **la fase de objetos no
  existe**.
- El `ENCODING`/`LC_COLLATE` de una base de datos es **inmutable** tras el `CREATE DATABASE`:
  no hay un `ALTER DATABASE ... SET ENCODING` equivalente. Cambiar el encoding de una BD en
  PostgreSQL implica volcar y recargar (para eso está el
  [módulo de clonado](database-clone.md)) → **la fase de base de datos no existe**.

Lo que sí se puede es cambiar la collation **por columna**, y eso es exactamente el modo
`columns`.

## Por qué NO hace falta un orden de dependencias

El [clon](database-clone.md) y el [diff de esquema](schema-comparison.md) necesitan un orden
topológico fino y un endpoint `resolve-selection` que cierre las dependencias de la
selección. **Acá no**, y es una simplificación deliberada, no un descuido.

Los 5 tipos de objeto **ya existen** en la BD, y cada uno se procesa de forma **independiente
y completa** (capturar DDL → capturar grants → drop → recrear → reaplicar grants) antes de
pasar al siguiente. Por eso el orden es irrelevante:

- MySQL/MariaDB **no validan el cuerpo** de una PROCEDURE/FUNCTION/TRIGGER/EVENT al crearlos
  (el cuerpo es opaco hasta que se ejecuta), así que da lo mismo si otra rutina que citan está
  en medio de su propio drop+create.
- `CREATE VIEW` **sí** valida que existan las tablas/vistas referenciadas, pero como el
  drop+create de **cada** objeto es inmediato (no se dropea todo el lote primero), en todo
  momento el resto de los objetos existe en su forma vieja o nueva — nunca ausente por más de
  un instante, y nunca mientras se está creando **otro** objeto.

Consecuencia práctica: no hay endpoint `resolve-selection`, no hay grafo de dependencias, y
la selección es exactamente lo que el usuario eligió.

## Flujo (plan → inventario → preview → confirmar → ejecutar asíncrono)

Mismo patrón seguro que el clon: el servidor es la única fuente de verdad y el cliente
confirma con `confirm_token` + `confirm_target_name`.

1. **`POST /servers/{server_id}/databases/{database}/collation-conversions`** — crea un PLAN.
   Valida el motor (422 si es PostgreSQL), que la BD no sea de sistema, que la BD exista en
   vivo y que el par `(target_charset, target_collation)` esté **habilitado en el catálogo
   global** (ver [charsets/collations](../api-reference-v2.md)). Persiste el job en `pending`
   con un `source_fingerprint` (anti-TOCTOU) y TTL (`COLLATION_CONVERSION_TTL_HOURS`, 24 h).
   Funciona con BDs **adoptadas y crudas** (patrón "por identidad").
2. **`GET /collation-conversions/{id}/objects`** — inventario en vivo para armar la selección:
   - `tables`: cada tabla con su `charset`/`collation` actual, cuántas **columnas** quedan
     fuera del objetivo (`mismatched_columns`) y si `needs_conversion`.
   - `summary`: **cuántos pares (charset, collation) distintos hay y cuántas tablas en cada
     uno** — la vista "cuántas collations tengo dando vueltas".
   - `objects`: los 5 tipos congelados con su `collation_connection` y `is_outdated`.
3. **`POST /collation-conversions/{id}/preview`** — resuelve el plan final SIN ejecutar:
   los pasos exactos, cuántas tablas se convierten/saltean, qué objetos se recrean, qué
   selección **ya no existe** (`missing`/`missing_tables`, se reporta y se excluye sin abortar)
   y el `confirm_token`.
4. **`POST /collation-conversions/{id}/execute`** — valida `confirm_target_name` +
   `confirm_token` + re-chequea el fingerprint + cuarentena, registra la intención (auditoría
   **fail-closed**) y **encola** el job asíncrono. Rate limit 3/min.
5. **`GET /collation-conversions/{id}`** — resumen + estado
   (`pending`→`running`→`succeeded`/`failed`/`interrupted`/`canceled`) + `phase` + `progress`.
6. **`GET /collation-conversions/{id}/items`** — un ítem por paso con su resultado.
   **`POST /collation-conversions/{id}/cancel`** — cancelación cooperativa.

### Por qué es asíncrono

`ALTER TABLE ... CONVERT TO CHARACTER SET` **reescribe la tabla completa** (cambia el default
de la tabla *y* convierte los datos de todas sus columnas de texto). En tablas grandes tarda
minutos u horas y bloquea escrituras: no cabe en una llamada HTTP síncrona. Igual que el clon,
el worker es **in-process** (`ThreadPoolExecutor`, `COLLATION_CONVERSION_MAX_WORKERS`, default
**1** para no saturar el I/O del servidor destino) y **no es durable**: si el gateway se
reinicia, los jobs `running` quedan `interrupted` (barrido en el `lifespan`).

## Qué ejecuta, en qué orden

1. **`ALTER DATABASE db CHARACTER SET x COLLATE y`** (opcional, `include_database_default`).
   Cambia **solo el default** que heredarán los objetos nuevos: **no toca** las tablas
   existentes (por eso hace falta el paso 2) ni los objetos ya creados (paso 3). Va primero
   para que los objetos recreados después queden asociados al default nuevo.
   **Es el único paso cuyo fallo corta el pipeline**: seguir dejaría los objetos recreados
   apuntando a un default que no cambió, o sea el problema que la operación viene a resolver.
2. **`ALTER TABLE db.t CONVERT TO CHARACTER SET x COLLATE y`** por cada tabla seleccionada,
   *best-effort*: un fallo se reporta en su ítem y **no aborta** las demás (abortar dejaría la
   BD a mitad de camino, el estado más peligroso para este feature). Esta fase —y **solo**
   esta— corre con los **chequeos de FK desactivados**; ver abajo.
3. **`SET NAMES x COLLATE y` + `DROP {TIPO} IF EXISTS` + `CREATE` verbatim** por cada objeto
   seleccionado, en cualquier orden, también best-effort.

### La pieza central: `SET NAMES` antes de cada `CREATE`

Recrear un objeto **no sirve de nada** si la sesión que lo recrea tiene la collation vieja: el
objeto volvería a congelar exactamente la collation que se quería cambiar. Por eso cada
`DROP`+`CREATE` viaja **precedido por `SET NAMES <charset> COLLATE <collation>` en la misma
conexión**, y si ese `SET NAMES` falla no se recrea nada (corte al primer fallo). Los engines
remotos usan `NullPool`, así que el `SET` no se filtra a ninguna otra operación.

## Detalles que importan

### La fase de tablas corre con `foreign_key_checks` DESACTIVADO

No es una optimización ni defensa en profundidad: sin eso el motor **rechaza la operación**.
La doc de MySQL, en `ALTER TABLE`:

> *"When the `foreign_key_checks` system variable is enabled, which is the default setting,
> **character set conversion is not permitted** on tables that include a character string
> column used in a foreign key constraint. The workaround is to disable `foreign_key_checks`
> before performing the character set conversion."*

Y no es un caso de borde: lo dispara **cualquier** esquema con una FK sobre `varchar`.

El flag va **solo** en la fase de tablas. El `ALTER DATABASE` no toca tablas, y un
`DROP`+`CREATE` de rutina no está sujeto a esta restricción: extenderlo ahí solo ampliaría la
ventana sin comprar nada.

**Efecto sobre el aviso de conversión parcial**: con los chequeos desactivados el motor
**acepta** el DDL, así que convertir unas tablas y no otras ya **no falla** con 3780/1832 — la
incoherencia entre los dos lados de una FK aparece recién al **consultar**
(`Illegal mix of collations`). El preview lo dice con ese matiz.

*Pendiente de motor real*: si MariaDB 10.x/11.x impone la misma restricción. Su KB no devolvió
la sección. El fix es inofensivo en cualquier caso.

### Los pasos corren con el timeout de volcado, no con el interactivo

Toda sentencia de la conversión va con `bulk=True`
(`REMOTE_BULK_STATEMENT_TIMEOUT_MS`, 1 h por default) en lugar del interactivo de 15 s
(`REMOTE_STATEMENT_TIMEOUT_MS`). Es lo que hace ejecutable el feature, no una mejora de
rendimiento.

El motivo tiene un matiz que importa: en MySQL/MariaDB ese timeout se traduce a
`read_timeout`/`write_timeout` **de socket, del CLIENTE** (`remote_engine._connect_args`). Un
`CONVERT TO CHARACTER SET` sobre cualquier tabla no trivial rompía la conexión a los 15 s
**mientras el motor seguía reescribiendo la tabla**: el gateway registraba como fallida una
sentencia que en realidad se iba a completar, y de paso dejaba la BD en cuarentena. O sea, el
peor estado posible —conversión corriendo, inventario diciendo que falló— era el resultado
*esperado* para cualquier tabla real. En PostgreSQL es `statement_timeout` de servidor
(cancelación limpia), así que ahí el síntoma era distinto pero el techo el mismo.

**Límite conocido que esto NO cubre**: el camino de Alembic (`_apply_one`/`_prepared`), que es
el que aplica una migración de blueprint, **sigue** con el timeout interactivo. Subírselo a
todas las migraciones es un cambio de comportamiento global y se decidió aparte (ítem en
`TODO.md`).

### No se puede convertir la propia BD de metadatos del gateway

`ensure_not_reserved_database` cubre las BDs de sistema del motor, pero la base del gateway es
para el motor una base de usuario común: si su servidor está dado de alta —el caso normal en un
compose—, nada impedía apuntarle una conversión. `ALTER TABLE audit_log CONVERT TO …` reescribe
la tabla completa **mientras el gateway escribe en ella**, y `audit_log` es el único control
compensatorio que el sistema declara para todo lo demás; `servers` guarda
`root_password_encrypted`.

Se reusa el guard de la consola SQL (`query_policy.is_gateway_metadata_target`), que resuelve
ambos hosts a IPs e intersecta —así que registrar el servidor por su IP en vez de por su nombre
no lo evade— y es fail-closed si la resolución falla. **409** con
`public_context.code = "collation.scope_not_allowed"`. Mismo guard que ya tenían el clon, el
export y la consola SQL; este módulo era el único de la familia sin él.

### El inventario se sincroniza al terminar

Una conversión exitosa escribe el charset/collation nuevos en `ManagedDatabase.charset` /
`.collation`. Sin esto la fila del inventario **mentía** después de cada conversión, y son
justamente las columnas que cualquier detección de deriva contra el blueprint tiene que leer.

Tres condiciones, y ninguna es opcional: solo en modo `universal` (en `columns` el objetivo es
la collation de una *columna*, no el `LC_COLLATE` de la base); solo si la BD está en el
inventario; y **solo si el `ALTER DATABASE` se ejecutó y salió `ok`**. Esa última importa: son
el *default de la base*, y ese paso es opcional (`include_database_default`). Sincronizar
incondicionalmente cambiaría una fila *stale* por una fila **falsa**, que es peor — la deriva
la reportaría como al día.

### El `DEFINER` se preserva verbatim (a diferencia del clon)

`dump_structure` y el clon **sanean** el `DEFINER` porque cruzan de servidor, donde un
definer inexistente rompe el `CREATE`. Acá el objeto se recrea en la **misma BD del mismo
servidor**, así que el usuario del definer sigue existiendo — y quitarlo **no sería neutro**:
una rutina o vista con `SQL SECURITY DEFINER` pasaría a ejecutarse con la credencial
pseudo-root del gateway, una **escalada de privilegios silenciosa**. Si el pseudo-root no
tiene permiso para fijar un definer ajeno (`SET_USER_ID`/`SUPER`), el `CREATE` falla y el paso
se reporta como error: preferible a recrear el objeto con permisos distintos de los que tenía.

### Privilegios de rutina: capturar antes, reaplicar después (fail-closed)

MySQL/MariaDB **borran los privilegios de una rutina al dropearla**. La doc de MySQL:

> *"MySQL does not automatically revoke any privileges when you drop a database or table.
> However, if you drop a routine, any routine-level privileges granted for that routine are
> revoked."*

Es una **asimetría real** con las tablas (un `DROP TABLE` + `CREATE TABLE` sí conserva sus
grants). Por eso, para PROCEDURE y FUNCTION el gateway:

1. **Lee** los privilegios de `mysql.procs_priv` **antes** del `DROP`. Es la única fuente
   directa: `information_schema` **no tiene** tabla de privilegios de rutina (llega hasta
   `COLUMN_PRIVILEGES` y se saltea las rutinas). Si `mysql.procs_priv` no es legible, hay un
   **fallback** que recorre `SHOW GRANTS FOR` por cuenta y filtra las líneas de esa rutina.
2. Si **ninguna** de las dos vías funciona, **no dropea la rutina** y reporta el paso con
   `status='skipped'` + `grants_error`. Dropear a ciegas destruiría privilegios sin forma de
   restaurarlos: peor que no convertir el objeto. *Solución*: otorgar `SELECT` sobre
   `mysql.procs_priv` (o sobre el esquema `mysql`) a la credencial del gateway y reintentar.
3. **Reaplica** los grants tras el `CREATE`
   (`GRANT EXECUTE, ALTER ROUTINE ON PROCEDURE|FUNCTION \`db\`.\`rutina\` TO 'u'@'h'`, sintaxis
   válida en MySQL 8 y MariaDB). Si la reaplicación falla, el ítem queda en **`error`** aunque
   el objeto se haya recreado bien: existe pero perdió permisos, y eso no puede reportarse
   como éxito.

**TRIGGER, EVENT y VIEW no necesitan nada de esto**: no tienen privilegios propios a nivel de
objeto. El permiso de un trigger viaja en el privilegio `TRIGGER` de su **tabla** y el de un
evento en el privilegio `EVENT` de la **base de datos**; no existe un `triggers_priv` ni un
`events_priv`.

### El DDL capturado se persiste ANTES de ejecutar

MySQL/MariaDB **no tienen DDL transaccional**. Si el `CREATE` falla después de un `DROP`
exitoso, el objeto **desapareció** del motor. Por eso el ítem se persiste con
`captured_ddl` **antes** de tocar el motor: esa columna es la única copia con la que el
operador puede recrear el objeto a mano. Cuando ocurre, el `error` del ítem lo dice
explícitamente ("el DROP se aplicó y el CREATE no… usá `captured_ddl`").

### Tablas ya al día: se saltean

Una tabla cuya `TABLE_COLLATION` ya es la objetivo **puede tener columnas con `COLLATE`
explícito distinto**, así que decidir "no necesita conversión" mirando solo el default sería
incorrecto. El inventario cuenta las columnas desalineadas (`mismatched_columns`) y solo
saltea la tabla si **el default y todas sus columnas de texto** ya están en el objetivo.
Importa porque `CONVERT TO CHARACTER SET` reescribe la tabla completa **aunque no cambie
nada**.

### Avisos del preview

- **Selección parcial de tablas**: MySQL/MariaDB exigen la **misma collation en ambos lados
  de una FK**, y comparar columnas de collations distintas produce `Illegal mix of
  collations`. Convertir unas tablas y no otras puede fallar con `(3780)`/`(1832)` o romper
  consultas que hoy funcionan.
- **FKs desde otra BD del servidor** (`external_fk_dependents`, reusado del clon): esas otras
  bases **no** se convierten con este job; hay que planificar una conversión para cada una.
- **Objetos congelados sin recrear**: es exactamente el caso que la herramienta existe para
  evitar, así que se avisa explícitamente.
- **Longitud de clave**: si el charset nuevo usa más bytes por carácter (p. ej. `utf8mb3` →
  `utf8mb4`), un índice existente puede superar el límite de InnoDB y fallar con
  `(1071, 'Specified key was too long')`.
- **Eventos vencidos**: un `EVENT` con fecha ya pasada y `ON COMPLETION NOT PRESERVE` puede
  rechazar su recreación verbatim ("Event execution time is in the past").

### Tablas internas del gateway

`_gw_v_*` y `_gw_stg_*` (la contabilidad de Alembic dentro de cada BD gestionada) **quedan
excluidas** del inventario, igual que en los otros cuatro caminos que enumeran tablas: no son
esquema del usuario y no deben aparecer en su selección.

## Confirmación, concurrencia y auditoría

- **Doble factor de backend**: `confirm_target_name` debe ser el nombre exacto de la BD
  (obliga a identificar conscientemente cuál se convierte) **y** `confirm_token` debe coincidir
  con el hash del **plan resuelto**. Cualquier cambio de selección, de objetivo o del
  inventario invalida la confirmación. Es el mismo mecanismo del clon (no el HMAC stateless de
  `confirm_token.py`, que se usa donde no hay fila donde anclar el plan).
- **Anti-TOCTOU en tres puntos**: al previsualizar, al ejecutar y **al arrancar el worker**.
  Si el inventario cambió, se prefiere no tocar nada antes que convertir un esquema distinto
  del que el operador confirmó. `force=true` adopta el inventario nuevo como base.
- **Advisory lock del motor** sostenido durante todo el pipeline sobre una conexión dedicada,
  con el **mismo espacio de claves** que el clon y schema-comparisons: una conversión y un clon
  sobre la misma BD física no pueden pisarse.
- **Auditoría**: `collation_conversion.plan` al planear; `collation_conversion.execute` con
  `record_intent` **fail-closed** antes de encolar y una entrada **agregada** al terminar
  (patrón `apply_profile_bulk`/`apply_all`).
- **Cuarentena**: si el job falla y la BD está en el inventario, queda en `status=error` hasta
  que un admin la revise (`force=true`).

## Modo `columns`: PostgreSQL

Mismo recurso, mismos endpoints, mismo doble factor de confirmación, mismo worker asíncrono y
mismo advisory lock. Lo que cambia es **qué se ejecuta**, porque PostgreSQL trata la collation
de otra forma.

### Qué ejecuta (y qué no)

Una sola clase de sentencia, **una por tabla seleccionada**:

```sql
ALTER TABLE "public"."users"
  ALTER COLUMN "email"  SET DATA TYPE character varying(255) COLLATE "es-ES-x-icu",
  ALTER COLUMN "nombre" SET DATA TYPE text                   COLLATE "es-ES-x-icu";
```

- **Nada de `ALTER DATABASE`**: `include_database_default` se ignora y el preview lo devuelve
  siempre en `false`. El `ENCODING`/`LC_COLLATE`/`LC_CTYPE` son inmutables.
- **Nada de recrear vistas/funciones/triggers/eventos**: enviar `objects` en el preview es un
  **422** (no se ignora en silencio; es un error de concepto del cliente).
- **La selección sigue siendo por TABLA** (igual que en `universal`), pero por debajo se
  traduce a una acción `ALTER COLUMN` por cada columna de texto **que todavía no esté** en la
  collation objetivo. Las columnas ya al día no se tocan; una tabla con todas sus columnas al
  día queda como paso `skip`.
- **Todas las columnas de una tabla viajan en la MISMA sentencia**. No es cosmético: hace una
  sola pasada (un solo `ACCESS EXCLUSIVE`, una sola reconstrucción de índices) y —como
  PostgreSQL **sí** tiene DDL transaccional— la vuelve atómica por tabla. El estado "media
  tabla convertida", que en MySQL/MariaDB es un riesgo real, acá no existe.
- No hay `ALTER COLUMN ... SET COLLATE` en la gramática de PostgreSQL: cambiar la collation de
  una columna **solo** se puede expresar como un `SET DATA TYPE` que repita el mismo tipo. Por
  eso el tipo se captura de `format_type()` (con sus parámetros exactos, `character
  varying(255)`) y se repite verbatim: cambiarlo por descuido convertiría una operación de
  collation en una migración de tipos. Tampoco se emite `USING`: es opcional y, con el tipo
  destino igual al de origen, la conversión implícita es la identidad.

### El catálogo de collations es OTRO (y se lee en vivo)

Esto es la diferencia conceptual más fácil de confundir:

| | `charset_collation_options` (catálogo global del gateway) | `pg_collation` (modo `columns`) |
|---|---|---|
| Para qué | `ENCODING`/`LC_COLLATE` con los que se **crea** una BD | El `COLLATE` de una **columna** |
| Forma del valor | Locale del SO: `en_US.UTF-8` | Nombre de objeto: `en_US`, `C`, `es-ES-x-icu` |
| Alcance | Global, administrado por el operador | **Por servidor**, leído EN VIVO |

El modo `columns` **no usa** `charset_catalog.resolve_enabled_combination`: valida el
`target_collation` contra `pg_collation` **del servidor destino**, y lo que viaja al DDL es el
nombre exacto que devolvió ese catálogo (nunca el texto crudo del request). Motivo: qué
collations existen depende de los locales instalados en el SO de **cada** máquina (y de si el
binario trae ICU), así que una lista global sería directamente falsa.

Detalles del filtro, tomados de la doc de `pg_collation`: se ofrecen las que tienen
`collencoding = -1` (independientes del encoding) **o** el encoding de la base — "PostgreSQL
generally ignores all collations that do not have `collencoding` equal to either the current
database's encoding or -1". `default` se excluye a propósito: no nombra una collation concreta
sino "la de la base", así que como objetivo no significaría nada. Los nombres son
**case-sensitive**: `c` no es `C`.

`GET /collation-conversions/{id}/objects` devuelve el catálogo en `available_collations`
(`name`, `provider`, `deterministic`) para que el frontend arme el selector, y cada tabla trae
sus `columns` (`name`, `data_type`, `current_collation`, `is_default_collation`). El `summary`
agrupa **por collation de columna**: `column_count` = cuántas columnas la usan y `table_count`
= en cuántas tablas aparece.

Una columna **sin** `COLLATE` explícito se reporta con `current_collation: null` +
`is_default_collation: true` y **cuenta como pendiente** aunque el locale de la base coincida
con el objetivo: para el motor `pg_catalog.default` y la collation concreta son objetos
distintos, y mezclarlas es justo lo que dispara el conflicto.

### Avisos del preview

- **Lock e índices**: `ALTER COLUMN ... TYPE` toma `ACCESS EXCLUSIVE` sobre la tabla durante
  toda la operación (bloquea incluso los `SELECT`) y **reconstruye todos los índices** que
  incluyan esas columnas — cambiar la collation cambia el orden, así que el índice viejo no
  sirve. Como el tipo no cambia, PostgreSQL normalmente **no** reescribe el heap (el tipo es
  binariamente coercible a sí mismo), pero la reconstrucción de índices en tablas grandes ya
  es una operación larga. El aviso lo dice en ese tono deliberadamente conservador: conviene
  verificarlo contra la versión concreta antes de una ventana ajustada.
- **Conversión parcial**, con la semántica de PostgreSQL (**no** la de MySQL): acá el motor
  **no rechaza el DDL**. El problema aparece al **consultar**: `could not determine which
  collation to use for string comparison` (SQLSTATE **42P22**) en un `=`/`<`/JOIN, y
  `collation mismatch between implicit collations` (**42P21**) al planificar un
  `COALESCE`/`CASE`/`UNION`/`ORDER BY`. La collation de una columna es una derivación
  *implícita*, así que dos columnas con collations explícitas distintas no tienen desempate.
- **FKs de texto** (`collatable_foreign_keys`, consulta a `pg_constraint`): si un lado se
  convierte y el otro no, se avisa. Hasta PostgreSQL 17 el motor **no valida** la collation de
  una FK ni revalida el constraint tras un `ALTER`, así que **no hay ningún error al aplicar**:
  el JOIN de la FK falla en tiempo de consulta y la FK puede quedar lógicamente inconsistente.
  PostgreSQL 18 sí lo exige, y un `pg_dump`/`pg_upgrade` hacia 18 fallaría al restaurar. La
  salida es incluir ambas tablas en la conversión.
- **Collation no determinista** (PG 12+, solo ICU): en PostgreSQL 12–17 **impide** la
  comparación por patrón (`LIKE`, expresiones regulares, operadores `*_pattern_ops`) y la
  deduplicación de índices B-tree. Si la app filtra con `LIKE` sobre una columna convertida,
  se rompe. Se avisa desde el inventario, no solo desde el preview.
- **Alcance**: solo el schema `public`, misma limitación que el diff de esquema y el clonado.

### Diferencias de contrato de API (todas aditivas)

- `target_charset` pasó a **opcional**: obligatorio en `universal`, **rechazado con 422** en
  `columns` (PostgreSQL no tiene charset por columna ni por tabla).
- Campos nuevos: `mode` (summary/inventario/preview), `columns` por tabla y por paso,
  `column_count` en el `summary`, `available_collations`, `columns_to_convert` en el preview,
  `columns_affected` por ítem.
- Los ítems del modo `columns` son de `object_type='table'` y la fase es `tables`: el polling
  del frontend no cambia. `objects` es siempre `[]`.

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `COLLATION_CONVERSION_TTL_HOURS` | `24` | Vida útil de un plan; después, `execute` exige replanear (410). |
| `COLLATION_CONVERSION_MAX_WORKERS` | `1` | Workers in-process. Default 1: varios `CONVERT TO` en paralelo saturan el I/O del destino. |

## Limitaciones conocidas

- **No es durable**: un reinicio del gateway deja el job en `interrupted`; hay que revisar los
  ítems ya aplicados antes de crear un plan nuevo.
- **Cancelar no interrumpe un `ALTER TABLE` en curso**: es cooperativa y detiene los pasos que
  todavía no empezaron. Matar la sentencia dejaría la tabla a medio reescribir.
- **Sin rollback automático**: convertir es una operación de una sola dirección. Volver atrás
  es otra conversión (a la collation anterior), con el mismo costo.
- **`automatic_sp_privileges`** (default `1` en ambos motores) hace que el creador de una
  rutina reciba `EXECUTE`/`ALTER ROUTINE` automáticamente: recrear una rutina puede agregar
  filas en `mysql.procs_priv` para el definer. Es inocuo (ya tenía esos permisos) pero
  explica filas nuevas ahí.
### Solo del modo `columns` (PostgreSQL)

- **Alcance `public`**: las tablas de otros schemas no se ven ni se convierten.
- **Un dominio o un array de texto** entra en el inventario (son colacionables), pero su
  `ALTER` puede ser rechazado por el motor; el fallo se reporta en el ítem de esa tabla y no
  aborta las demás.
- **Una columna cuyo tipo tiene una forma inesperada** (whitelist de `format_type`) queda
  FUERA del inventario con una nota: no se emite DDL a ciegas.
- **Nombres de collation duplicados en varios schemas**: el `COLLATE` se emite sin calificar y
  lo resuelve el `search_path`. Si un nombre existe en más de un schema se anota en `notes`,
  porque ahí podría aplicarse una collation distinta de la esperada.
- **No revalida FKs ni índices funcionales** después del cambio: solo avisa.

## Verificación

- Modo `universal`: `tests/test_api_collation_conversions.py` (adapter y motor mockeados).
  **Pendiente de e2e contra MySQL/MariaDB reales**: el `SET NAMES` + `DROP`+`CREATE`
  refrescando de verdad la `collation_connection` de una rutina, la lectura de
  `mysql.procs_priv` con la credencial pseudo-root real, y la migración Alembic contra la BD
  del gateway real.
- Modo `columns`: `tests/test_api_collation_conversions_pg.py` (el adapter fake **subclasa el
  PostgresAdapter real**, así que el DDL verificado lo rendea el código de producción).
  **Pendiente de e2e contra PostgreSQL real** (sin Docker en el entorno donde se implementó):
  las consultas de catálogo (`pg_collation` con el filtro de `collencoding`, `pg_attribute`
  con `attcollation`, `pg_constraint` con el par `conkey`/`confkey`), que el `ALTER` múltiple
  no reescriba la tabla, el tiempo real de reconstrucción de índices, y el comportamiento
  exacto de 42P22/42P21 en una FK con collations mezcladas.
