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

## Lote por blueprint: convertir las N bases de una vez

Guía de API: [`api-reference-v17.md`](../api-reference-v17.md).

Un blueprint es un esquema que N bases replican. Convertir una sola no dejaba rastro y las otras
quedaban como estaban: el módulo no miraba `model_id` ni una vez.

**El lote son N conversiones REALES, no una migración.** Esa es la decisión de fondo y conviene
dejarla escrita, porque "materializarlo como versión de blueprint y repartirlo con `apply`" es la
respuesta intuitiva y va a volver a proponerse. No sirve: el SQL de una conversión promete un
resultado que **depende del estado de cada destino**. Una versión estática no puede recrear los
objetos con la collation congelada de la hermana —necesita su cuerpo, sus grants y su DEFINER, no
los del origen—, así que aplicarla le convertiría las tablas y le dejaría las vistas y rutinas en
la collation vieja: exactamente el `Illegal mix of collations` que este módulo existe para evitar,
ahora sobre una BD cuyo operador nunca vio el asistente.

Cuatro razones más, todas verificadas, por las que una versión con el `ALTER` de collation adentro
no es viable:

- **Un cambio de charset SÍ es destructivo**, y el propio repo ya lo decía en el otro camino:
  `schema_diff` marca `destructive=True, data_conversion=True` con el comentario "re-encoding
  físico: destructivo". `CONVERT TO` hacia un charset más angosto **reemplaza** los caracteres no
  representables. Pasaba el guard de entornos solo porque sqlglot degrada esas sentencias a
  `exp.Command` y el clasificador del AST no las veía. Eso ya está corregido.
- **El motor prohíbe la operación** con `foreign_key_checks` activo sobre una tabla con columna de
  texto en una FK. En el job es un ítem en error; en una versión **aborta el `upgrade()` entero**.
- **El timeout del camino de Alembic** es de socket del cliente, así que corta la conexión
  mientras el motor sigue reescribiendo: el checkpoint no registra la sentencia, el motor la
  completa, y el próximo `apply` la reejecuta. Bucle.
- **`CONVERT TO` cambia tipos de columna** (`TEXT`→`MEDIUMTEXT`, documentado en MySQL 8). Según el
  estado de partida de cada destino, la operación que promete uniformar puede crear divergencia.

### Flujo

`POST /database-models/{id}/collation-conversions` (planifica: un job por BD activa, ya
previsualizado) → `.../{batch_id}/execute` (confirma y encola) → `GET .../{batch_id}` (polling)
→ `.../{batch_id}/cancel`.

Solo entran las BDs **`status=active`**: una `pending` no existe en el motor, una `error` está en
cuarentena, una `archived` fue retirada de uso.

### La confirmación: un lote se lleva N re-tipeos y hay que reponerlos

`TODO.md` declara que lo que protege este módulo es *"su propio doble factor (re-tipeo +
`confirm_token`)"*. El `batch_token` lo genera el servidor, así que aporta **frescura**, no
**intención** — es literalmente el argumento con el que este repo eliminó el consentimiento por
corrida de la captura de SELECT. Por eso `execute` exige, junto:

1. `confirm_model_slug` == el slug del blueprint.
2. `database_ids` **echado de vuelta**, idéntico al previsualizado → 422 fail-closed. No se
   recorta ni se amplía, y **`force` tampoco puede agregar** (si dejara, alguien lo usaría para
   meter las bases que quedaron en cuarentena o fuera del tope).
3. El **nombre re-tipeado de cada BD** cuyo entorno tenga `blocks_destructive_migrations=true`.
   No inventa política nueva: repone el doble factor por base donde `TODO.md` dice que vive.
4. El token recomputado server-side sobre el lote resuelto.
5. Por cada job, el **mismo** camino de validación que una conversión suelta
   (`_validate_job_execution`, extraído justamente para que los dos no puedan divergir).

### Detalles que tienen motivo

- **`capped` se PERSISTE**, no solo se devuelve al planear: si no, el polling no puede reportar
  el recorte y el operador cree que se convirtió todo el blueprint.
- **El desenlace del lote se DERIVA al leer** (idempotente), no lo escribe ningún worker. Que
  cada uno consultara a sus hermanos para saber si es el último es una carrera, y aparece en
  cuanto `COLLATION_CONVERSION_MAX_WORKERS` deje de ser 1 (su default es configuración, no
  invariante).
- **Los jobs corren EN SERIE** con el default de 1 worker, y el pool es único: un lote de 12
  monopoliza el módulo por horas. De ahí `runs_serially` y `batch_seq` en el contrato — sin eso
  la UI no puede distinguir "en cola" de "colgado".
- **Se reusa `create_plan` + `preview` por BD** aunque cueste 2-3 lecturas de catálogo por base.
  Un camino "optimizado" que no pase por esos métodos deja de heredar sus validaciones (catálogo
  de charsets, existencia de la BD, guard de la BD de metadatos, TTL, fingerprint), y es así como
  los dos caminos se separan. Si el costo molesta, lo correcto es cachear el inventario DENTRO de
  la corrida, no saltear los métodos.
- **El barrido de arranque cierra también los jobs EN COLA** de un lote `running`. La cola del
  `ThreadPoolExecutor` no es durable: un reinicio dejaba un job `interrupted` y los demás
  `pending` **para siempre**, con el lote colgado. Solo se tocan los que tienen `batch_id` — un
  `pending` suelto es un plan legítimo sin ejecutar. **No se re-encolan**: el fingerprint pudo
  cambiar y el token autorizaba un plan sobre un inventario que ya no es el actual.
- **Cancelar un job en cola ya no ejecuta nada.** El reclamo del worker mira `cancel_requested`;
  antes solo filtraba por `status`, así que un job cancelado entraba al pipeline y ejecutaba la
  fase 1 completa —el `ALTER DATABASE`— porque los dos únicos cortes cooperativos están dentro de
  los bucles de tablas y objetos.

## La versión de contabilidad

`POST /database-models/{id}/collation-conversions/{batch_id}/blueprint-version` registra un lote
terminado como versión secuencial y la **stampea** en sus N bases.

**Se crea y se marca; no se aplica nunca.** La conversión ya la hizo cada job. Esto solo evita
que el ledger del blueprint mienta sobre lo que sus bases tienen físicamente.

Es una llamada **explícita del operador** y no un hook del worker, y eso resuelve cuatro cosas de
una:

- el worker sostiene el advisory lock del motor durante todo su `_finish`, con la **misma clave**
  que `stamp` pide en otra conexión: el `GET_LOCK` esperaría 30 s y devolvería **409, siempre**;
- `run_job` envuelve el pipeline en un `except` que reetiquetaría como fallida una conversión ya
  ocurrida e **irreversible**;
- acá hay `admin` y Request ID reales, y el worker no los hereda (`ThreadPoolExecutor.submit` no
  propaga ContextVars);
- un fallo es un HTTP que el operador ve, no un estado que descubre horas después.

### Ocho guards, y por qué cada uno

| Guard | Por qué |
|---|---|
| lote completo | Versionar un lote que falló afirmaría en el ledger algo que el plano físico no tiene. |
| sin bases de otro motor | Una hermana PostgreSQL queda con la cadena trabada **permanentemente**: no puede existir un `up_sql_postgresql` válido porque su `LC_COLLATE` es inmutable tras el `CREATE DATABASE`. |
| ninguna activa fuera del lote | La que quedó afuera tendría la versión pendiente, y aplicarla le convertiría las tablas sin recrearle los objetos congelados. |
| todas en el head | Dos motivos independientes: el SQL sale de un inventario que no refleja las versiones intermedias, y stampear `max+1` afirmaría que esas intermedias se aplicaron. |
| mismos conjuntos de tablas | Si difieren hay deriva estructural que resolver antes de declarar una versión común. |
| ninguna conversión parcial | Convertir parcialmente UNA base es una decisión informada; propagar esa incoherencia de FKs a N bases no es la misma decisión. |
| ninguna en cuarentena | `stamp` limpia la cuarentena: stampearla borraría en silencio la marca de "revisá esta base". |
| tope de tamaño | `SNAPSHOT_MAX_SQL_PER_VERSION` (4 MB), el que ya usan los llamadores internos — no el cap de 256 KB de la ruta HTTP. |

### Cómo se construye el SQL

- **Sin calificar** con el nombre de la base: la migración corre conectada al destino, y
  `ALTER DATABASE` sin nombre aplica a la base por defecto de la conexión (documentado en MySQL 8
  y en MariaDB). Con el nombre del origen adentro, aplicarla a una hermana convertiría la base
  **equivocada**, en silencio.
- **Incluye las tablas que en el origen ya estaban al día**: una hermana futura puede no estarlo.
- El plan se **reconstruye** desde `selection` + un inventario fresco, no desde los ítems
  persistidos. Las tablas que ya no existen quedan fuera solas (viven en `missing_tables`, que no
  son pasos), mientras que en los ítems "ya no existe" y "ya estaba al día" son ambos `skipped` y
  distinguirlos por el texto del error sería frágil.
- El `up_sql` se une con el separador del manifiesto **resuelto al importar desde su única
  fuente**. Si no coincide, `usable_manifest` **descarta el manifiesto** con un warning en el log
  y se pierde `reconcile-partial` sin que nada falle.

### Por qué nace `is_baseline=False, reviewed=True`

No es DDL capturado del motor: son `ALTER TABLE` que construye el gateway con un charset y una
collation ya canonizados por el catálogo. Con `is_baseline=True, reviewed=False` **congelaría
`apply`/`apply_all` de todo el blueprint** hasta que alguien la aprobara, porque
`_guard_reviewed_baseline` es un tripwire de blueprint entero.

El control compensatorio es otro y ya está en pie: el guard de entornos ahora ve la conversión de
charset como destructiva, así que la versión queda bloqueada en los entornos protegidos.

### Sin `down_sql` ni `down_sql_suggested`

`RollbackGenerator` devuelve `None` para este SQL, y eso **es la verdad**. Un reverso derivado de
las collations previas del origen sería peor que no tener ninguno: el campo se publica, la doc del
módulo empuja a promoverlo con `PATCH`, y ejecutarlo re-codifica hacia atrás —pérdida de
caracteres irreversible— con collations que las hermanas nunca tuvieron.

### Lo que la versión NO garantiza

`CONVERT TO` puede cambiar `TEXT`→`MEDIUMTEXT` / `VARCHAR`→`MEDIUMTEXT`, así que un
`schema-comparison` posterior puede mostrar diffs de **tipo** causados por esta conversión. Y una
BD agregada al blueprint **después** del lote tendrá la versión pendiente: aplicarla le convierte
las tablas pero **no** le recrea los objetos congelados. Para esa base el camino correcto es su
propio job de conversión y después `stamp`.

## Deriva contra la declaración del blueprint

`GET /database-models/{id}/collation-drift`. **Cero conexiones al motor.**

`DatabaseModel.charset`/`.collation` existían con un comentario diciendo que servían "para
detectar BDs que se han desviado", y **nadie los leía**. Esto los vuelve una referencia usable: se
comparan contra la copia del inventario, que la conversión ahora sincroniza al terminar.

Por eso la respuesta declara `source: "cached"` y trae `source_note`: presentar una caché como
verdad del motor sería mentir en una pantalla que se usa para decidir conversiones.

Cinco estados, y **`unknown` no es `ok`** — pintarlos iguales le diría al operador que todo está
bien sobre bases de las que no se sabe nada. `not_applicable` es PostgreSQL: allá el concepto es
`encoding` + `lc_collate`, que no son equivalentes.

Cada fila trae `source_of_truth`, y no es adorno: `charset`/`collation` siguen siendo escribibles
a mano por `PATCH /managed-databases/{id}`, así que una fila puede decir `ok` porque alguien lo
tipeó. Es el mismo defecto que el repo ya corrigió para `model_version`; mientras siga abierto, la
UI necesita poder distinguir un dato leído del motor de una afirmación.

## Variables de entorno

| Variable | Default | Descripción |
|---|---|---|
| `COLLATION_CONVERSION_TTL_HOURS` | `24` | Vida útil de un plan; después, `execute` exige replanear (410). |
| `COLLATION_CONVERSION_MAX_WORKERS` | `1` | Workers in-process. Default 1: varios `CONVERT TO` en paralelo saturan el I/O del destino. |

## Limitaciones conocidas

- **No es durable**: un reinicio del gateway deja el job en `interrupted`; hay que revisar los
  ítems ya aplicados antes de crear un plan nuevo. En un **lote**, además, los jobs que estaban
  en cola se cierran como `interrupted` y el lote como `failed`: la cola del `ThreadPoolExecutor`
  no sobrevive al proceso y **no se re-encolan** a propósito (el fingerprint pudo cambiar y el
  token autorizaba un plan sobre un inventario que ya no es el actual). Se replanifica.
- **El lote corre en SERIE.** `COLLATION_CONVERSION_MAX_WORKERS` es 1 por default y el pool es
  único, así que un lote de 12 bases monopoliza el módulo por horas y bloquea cualquier
  conversión suelta mientras dure.
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

### De la versión de contabilidad

- **`CONVERT TO` puede cambiar el TIPO de una columna** (`TEXT`→`MEDIUMTEXT`,
  `VARCHAR`→`MEDIUMTEXT`; documentado en MySQL 8, para que entre el texto re-codificado). Un
  `schema-comparison` posterior puede mostrar diffs de **tipo** causados por esta conversión.
- **Una BD agregada al blueprint DESPUÉS del lote tendrá la versión pendiente**, y aplicarla le
  convierte las tablas pero **no** le recrea los objetos con la collation congelada. Para esa
  base el camino correcto es su propio job de conversión y después `stamp`, no el `apply`.
- **La versión queda bloqueada en entornos con `blocks_destructive_migrations`**, y eso es
  deliberado: el guard de entornos ahora ve la conversión de charset como destructiva. Es el
  control compensatorio que sustituye al gate de `reviewed` que la versión no lleva.

## Cuatro defectos que aparecieron al construir esto

Estaban en producción, ninguno se buscó, y ninguno se manifiesta contra datos de juguete — que
es por lo que la feature figuraba como verificada.

1. **El motor PROHIBÍA la operación.** MySQL no permite convertir el charset de una tabla con
   una columna de texto usada en una FK mientras `foreign_key_checks` esté activo. No es un
   borde: lo dispara cualquier esquema con una FK sobre `varchar`. La fase de tablas ahora corre
   con los chequeos desactivados, que es el workaround que la propia doc nombra.
2. **El timeout de 15 s era de socket DEL CLIENTE.** Cortaba la conexión de un `CONVERT TO`
   **mientras el motor seguía reescribiendo la tabla**: el gateway registraba como fallida una
   sentencia que en realidad se completaba, y encima dejaba la BD en cuarentena. El peor estado
   posible era el resultado *esperado* para cualquier tabla real.
3. **Cancelar un job EN COLA no lo detenía.** El reclamo del worker filtraba solo por `status` y
   no miraba `cancel_requested`, así que el job entraba al pipeline y ejecutaba la fase 1
   completa —el `ALTER DATABASE`— porque los dos únicos cortes cooperativos están DENTRO de los
   bucles de tablas y objetos.
4. **Nada impedía apuntar una conversión a la propia BD de metadatos del gateway**, o sea
   reescribir `audit_log` —el único control compensatorio del sistema— y `servers`, que guarda
   `root_password_encrypted`. El clon, el export y la consola SQL ya cerraban esto; este era el
   único módulo de la familia sin el guard.

Y una corrección que vale anotar porque la solución evidente rompía producción: el guard de
entornos **no** puede basarse en `forced_charsets` de `MigrationFacts`. Esa lista matchea
**cualquier mención** de `CHARACTER SET`/`CHARSET`, incluido el `DEFAULT CHARSET=utf8mb4` que
lleva prácticamente todo `CREATE TABLE` de MySQL: habría marcado como destructiva casi toda
migración del parque y bloqueado `apply`/`apply_all` de golpe. El detector es de **forma** (la
conversión), no de mención, y hay un test dedicado a fijar esa distinción.

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
- Lote, versión y deriva: `tests/test_api_collation_batches.py` (31 casos, worker síncrono).
  Dos de ellos fijan invariantes que se rompen **en silencio** y por eso no alcanzan con una
  aserción indirecta: uno invoca `MigrationRunner.usable_manifest` **de verdad** sobre el spec
  cargado con `_load_specs` (si el separador del manifiesto dejara de coincidir, se descarta el
  manifiesto con un warning en el log y se pierde `reconcile-partial` sin que nada falle), y
  otro corre la deriva con un adapter que **explota si lo llaman**, para que la promesa de
  `source: "cached"` no se degrade a una lectura del motor sin que nadie se entere.

**Lo que NO está verificado, y hay que decirlo:**

- **Nada contra motores reales.** Lo más importante a confirmar es si **MariaDB** impone la misma
  restricción de `foreign_key_checks` que MySQL documenta — es la premisa del primer fix de arriba
  y su KB no devolvió la sección —, y el ciclo completo lote → versión → `stamp` contra los tres
  motores.
- **El `ALTER DATABASE` sin nombre de base** está documentado en MySQL 8 y MariaDB, pero no se
  ejecutó: es lo que hace replicable el SQL de la versión, así que un fallo ahí la invalida.
- **Ruff no estaba instalado** en el entorno donde se implementó: el lint no se corrió. Solo se
  comprobó a mano que ninguna línea agregada supere los 100 caracteres.
- La migración `a6b7c8d9e0f1` **no se probó contra la BD del gateway real** (sí ciclo
  upgrade/downgrade/upgrade en SQLite, `alembic check` sin drift nuevo y head único).
