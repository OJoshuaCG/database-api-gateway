# API Reference v10 — Exportación de bases de datos (estructura y/o datos, multiformato)

> **Guía para el equipo de frontend.** Addendum de [`api-reference.md`](api-reference.md) y de
> [`api-reference-v2.md`](api-reference-v2.md) … [`api-reference-v9.md`](api-reference-v9.md).
>
> Como **v6**, **v8** y **v9**: describe un módulo **NUEVO que nunca fue expuesto al frontend**.
> No hay pantalla existente que ajustar — hay que diseñar una desde cero. El backend está
> implementado (fases F1–F6) y este documento es suficiente para construir la interfaz **sin
> leer el código del backend**.
>
> ⚠️ **Dos cosas que la UI tiene que transmitir sí o sí y que no son cosmética:**
> 1. Una exportación es una **extracción masiva de datos en claro**. No hay enmascarado
>    ([§9.4](#94-el-riesgo-que-la-ui-tiene-que-mostrar-no-hay-enmascarado)). Cada descarga
>    queda auditada, y eso hay que decirlo en pantalla.
> 2. La **consistencia es asimétrica por motor** ([§2.4](#24-la-asimetría-de-consistencia-por-motor)).
>    En MySQL/MariaDB el punto único en el tiempo cubre los datos pero **no** la estructura. El
>    backend lo avisa; ocultar ese aviso sería el peor bug de esta pantalla.
>
> **El objetivo entero del módulo es que el cliente NO duplique lógica de negocio.** Todo lo que
> el formulario necesita —tipos, formatos, valores válidos, defaults, qué combinaciones están
> prohibidas y por qué, y los límites numéricos— sale de **un solo endpoint**
> ([`/export-capabilities`](#31-get-serverssiddatabasesdbexport-capabilities-)). Si terminás
> escribiendo un `if (format === 'csv')` en el frontend, algo se hizo mal:
> [§2.3](#23-cómo-armar-el-formulario-sin-hardcodear-ni-una-regla) explica cómo evitarlo.
>
> Documentación de ingeniería del mismo módulo (más detalle interno del que el frontend
> necesita): [`docs/features/database-export.md`](features/database-export.md).

**Versión de la API:** `v1` · 🔌 = lee/toca el servidor de BD destino · 🔒 = requiere sesión admin

---

## Índice

- [0. El problema: por qué existe este módulo](#0-el-problema-por-qué-existe-este-módulo)
- [1. Alcance: qué cubre y qué NO cubre](#1-alcance-qué-cubre-y-qué-no-cubre)
- [2. Los conceptos que la UI tiene que modelar bien](#2-los-conceptos-que-la-ui-tiene-que-modelar-bien)
  - [2.1 Dos conjuntos: estructura y datos](#21-dos-conjuntos-estructura-y-datos)
  - [2.2 `scope_ddl` / `entity_ddl` son enumerados de cuatro valores](#22-scope_ddl--entity_ddl-son-enumerados-de-cuatro-valores)
  - [2.3 Cómo armar el formulario sin hardcodear ni una regla](#23-cómo-armar-el-formulario-sin-hardcodear-ni-una-regla)
  - [2.4 La asimetría de consistencia por motor](#24-la-asimetría-de-consistencia-por-motor)
- [3. Los 12 endpoints](#3-los-12-endpoints)
- [4. Máquina de estados del job](#4-máquina-de-estados-del-job)
- [5. Flujos completos, paso a paso](#5-flujos-completos-paso-a-paso)
- [6. Matriz de errores](#6-matriz-de-errores)
- [7. Interpretación visual: pantallas y estados](#7-interpretación-visual-pantallas-y-estados)
- [8. Tipos (referencia rápida)](#8-tipos-referencia-rápida)
- [9. Notas transversales](#9-notas-transversales)
- [10. Checklist de implementación](#10-checklist-de-implementación)

---

## 0. El problema: por qué existe este módulo

El gateway administra bases de datos de terceros pero **no tenía forma de sacar nada de ellas**.
Para llevarse un esquema, versionarlo, mandárselo a alguien o recargar una tabla en otro
servidor había que entrar al servidor por SSH y correr `mysqldump`/`pg_dump` a mano — o sea,
fuera del gateway, **sin auditoría, sin confirmación y sin ningún límite**.

Este módulo pone esa capacidad adentro: un volcado configurable en cuatro formatos, con
confirmación de doble factor, auditoría de cada descarga, TTL corto sobre el archivo y un techo
de concurrencia. Y de paso resuelve tres cosas que un `mysqldump` a mano no da: **consistencia
de punto único en el tiempo** sobre los datos, **determinismo byte a byte** (dos volcados del
mismo esquema son idénticos, así que se pueden diffear y versionar) y un **manifiesto** que
permite auditar qué salió sin abrir el archivo.

---

## 1. Alcance: qué cubre y qué NO cubre

### Cubre

- Cualquier base de **cualquier servidor dado de alta**, esté adoptada en el inventario o no
  (se identifica por `server_id` + nombre).
- **Estructura, datos o ambos**, con dos selecciones independientes y cierre de dependencias.
- Objetos de primer nivel: tablas, vistas, rutinas, triggers y —según el motor— eventos, vistas
  materializadas, secuencias, tipos enumerados y extensiones.
- Cuatro formatos: `sql`, `csv`, `json`, `ndjson`.
- Un archivo o uno por objeto, fragmentación por tamaño, `gzip`/`zip`.
- Entrega como **archivo** (descarga reanudable) o **en línea** (texto plano para el
  portapapeles).

### NO cubre

- **Enmascarado / anonimización de datos.** Riesgo aceptado explícito; ver
  [§9.4](#94-el-riesgo-que-la-ui-tiene-que-mostrar-no-hay-enmascarado).
- **Importación / restauración.** El gateway genera el artefacto; ejecutarlo es del operador. Si
  lo que se quiere es "llevar esta base a este otro servidor", el módulo correcto es el de
  **clonado** ([`api-reference-v4`](api-reference-v4.md)), no éste.
- **Plantillas guardadas ni exportaciones programadas.**
- **Muestreo con cierre referencial** ("1000 pedidos y sus clientes").
- **PostgreSQL: solo el schema `public`** (viene declarado en `scope.scope_note`; mostralo).

---

## 2. Los conceptos que la UI tiene que modelar bien

### 2.1 Dos conjuntos: estructura y datos

No es "una lista de tablas con una casilla *incluir datos*". Son **dos selecciones separadas**:

- `selection` → qué objetos llevan su **DDL**;
- `data` → de qué **tablas** salen las filas.

Con una **restricción**: `data ⊆ selection`. Violarla es **422 `export.data_without_structure`**
con la lista de tablas huérfanas.

**La excepción que la UI tiene que soportar**: si `structure.scope_ddl == "NONE"` **y**
`structure.entity_ddl == "NONE"`, la exportación es **"solo datos"** y la restricción no aplica.
Es un caso de uso frecuente (recargar una tabla que ya existe en el destino) y es la **única**
forma en que `csv`/`json`/`ndjson` pueden existir.

**Sugerencia de UI**: un árbol con dos columnas de casillas por fila (📐 estructura / 📊 datos),
donde la casilla de datos solo aparece en las tablas y se deshabilita si la de estructura está
apagada — **salvo** en modo "solo datos", donde la columna de estructura desaparece entera.

Cada selección acepta:

```jsonc
{ "mode": "all" | "include" | "all_except",   // data acepta además "none"
  "types": ["table", "view", ...],            // vacío = todos los tipos
  "names": ["clientes", "pedidos"],
  "include_patterns": ["fact_*"],
  "exclude_patterns": ["tmp_*", "*_log"] }
```

Los patrones son **glob** contra los nombres del catálogo (nunca llegan a una consulta). Orden:
`mode` → `include_patterns` → `exclude_patterns`, y **la exclusión gana**.

### 2.2 `scope_ddl` / `entity_ddl` son enumerados de cuatro valores

`NONE` | `CREATE` | `DROP_CREATE` | `CREATE_IF_NOT_EXISTS`.

**No los modeles como dos casillas** ("borrar" + "crear"). El backend usa un enumerado
precisamente para que el estado *"eliminar sin crear"* **no sea representable**. Dos casillas en
la UI lo vuelven a representar y el usuario va a poder pedirlo.

`DROP_CREATE` y `CREATE_IF_NOT_EXISTS` **no son opuestos**: la primera dice *"que quede
exactamente esto, destruyendo lo que haya"*, la segunda *"que exista, sin tocar lo que ya
está"*. Presentalos como cuatro opciones de un mismo control (radio o select), no como dos
interruptores.

- `scope_ddl` = la **base de datos** (`CREATE DATABASE` / `DROP DATABASE`).
- `entity_ddl` = **cada objeto** (`CREATE TABLE` / `DROP TABLE` …).
- `drop_if_exists` es **ortogonal** y aplica al `DROP` de `DROP_CREATE`.
- **`scope_ddl: "DROP_CREATE"` exige `structure.confirm_scope_drop` = el nombre real de la
  base**, re-tecleado. El artefacto va a contener un `DROP DATABASE`. Pediló con un campo de
  texto, nunca preseleccionado.
- `capabilities.options["structure.scope_ddl"].destructive` trae `["DROP_CREATE"]`: usalo para
  pintar esa opción en rojo, **sin hardcodear cuál es**.

### 2.3 Cómo armar el formulario sin hardcodear ni una regla

Éste es el objetivo del módulo entero. `GET /export-capabilities` devuelve todo lo que hace
falta y **la misma matriz que el servidor hace cumplir** — no una copia, la misma estructura de
datos. Si el formulario deshabilita lo que la matriz prohíbe, el 422 no puede aparecer.

#### a) Los controles salen de `options`

```jsonc
"options": {
  "structure.scope_ddl": {
    "values": ["NONE", "CREATE", "DROP_CREATE", "CREATE_IF_NOT_EXISTS"],
    "default": "NONE",
    "applicable": true,
    "destructive": ["DROP_CREATE"]
  },
  "sanitize.definer": {
    "values": ["keep", "omit", "replace", "auto"],
    "default": "omit",          // ya RESUELTO para este motor (en PostgreSQL sería "keep")
    "applicable": false,        // ← en PostgreSQL: el concepto no existe. Ocultá el control.
    "destructive": []
  },
  "csv.header": { "values": ["true", "false"], "default": true, "applicable": true, "destructive": [] }
}
```

La clave es la **ruta con puntos del campo en el `ExportSpec`** (`sanitize.definer`,
`output.compression`, `csv.line_terminator`, …). Reglas de renderizado:

- `values` → las opciones del select. **Nunca las escribas a mano**: si mañana el backend agrega
  un valor, aparece solo.
- `default` → el valor inicial. En las opciones enumeradas es un **string**; en las booleanas es
  un **boolean** de verdad y `values` son los strings `"true"`/`"false"` (esa asimetría existe,
  tenela en cuenta al parsear).
- `applicable: false` → **el concepto no existe en este motor**. Ocultá el control o mostralo
  deshabilitado con la explicación. Caso testigo: `sanitize.definer` en PostgreSQL.
- `destructive` → los valores que hay que marcar visualmente como peligrosos.

Los tipos de objeto salen de `object_types`, los formatos de `formats`, y los números duros
(`inline_max_bytes`, `artifact_ttl_minutes`, `max_duration_seconds`, `max_parts`, …) de `limits`.

#### b) Las combinaciones prohibidas salen de `compatibility`

```jsonc
"compatibility": [
  { "when":    { "format": "csv" },
    "forbids": ["structure.*", "data.insert_variant", "sanitize.session_preamble",
                "sanitize.transaction_wrap", "sanitize.charset_override.mode=override",
                "output.organization=single", "output.schema_manifest",
                "output.delivery=inline"],
    "requires": [],
    "reason":  "El formato delimitado solo transporta datos, un archivo por tabla.",
    "blocking": true,
    "code": "export.incompatible_option" },

  { "when":     { "structure.scope_ddl": "DROP_CREATE" },
    "forbids":  [],
    "requires": ["structure.confirm_scope_drop"],
    "reason":   "…", "blocking": true, "code": "export.incompatible_option" },

  { "when":    { "engine": "postgresql", "structure.scope_ddl": "DROP_CREATE" },
    "forbids": [], "requires": [],
    "reason":  "El DROP DATABASE no es ejecutable desde una conexión a esa misma base…",
    "blocking": false,                                     // ← AVISO, no bloqueo
    "code": "export.incompatible_option" }
]
```

**Cómo evaluarla en el cliente** (≈30 líneas, una sola vez, sin conocer ninguna opción):

1. **`when`** — la regla aplica si **todas** sus claves coinciden con el valor actual del spec.
   La clave especial **`engine`** se compara contra `capabilities.engine`, no contra el spec.
   La matriz viaja **entera**, incluidas las reglas de otros motores: filtrar por `when.engine`
   es tu trabajo.
2. **`forbids`** — cada entrada es:
   - `"ruta.opcion"` → esa opción debe estar en su **valor neutro** (`NONE` para los DDL,
     `"none"` para `data.insert_variant` y `output.compression`, `false` para los booleanos,
     `null` para `output.split_max_bytes`);
   - `"ruta.opcion=valor"` → **ese** valor concreto está prohibido;
   - `"structure.*"` → comodín; se expande a `structure.scope_ddl` y `structure.entity_ddl`.
3. **`requires`** — esa opción tiene que estar presente y no vacía.
4. **`blocking: false`** → es un **aviso**, no un bloqueo. Mostralo, no deshabilites nada.
5. **`reason`** es el texto listo para mostrar. No lo reescribas.

> **Regla práctica**: recalculá las reglas activas en cada cambio del formulario y usá el
> resultado para (a) deshabilitar controles, (b) mostrar el `reason` como ayuda contextual y
> (c) impedir el envío. Como el servidor evalúa exactamente lo mismo, un 422
> `export.incompatible_option` que llegue igual es un **bug de tu evaluador**, no del usuario:
> loguealo.

#### c) El dialecto csv y el empaquetado salen de sus bloques

```jsonc
"csv_dialect": {
  "delimiter": ",", "quote_char": "\"", "escape_char": null, "null_representation": "",
  "single_char_options": ["delimiter", "quote_char", "escape_char"],
  "null_vs_empty": "El NULL se escribe sin comillas como 'null_representation' y la cadena vacía SIEMPRE entre comillas…"
},
"packaging": {
  "multifile_when": ["output.organization=per_object", "output.split_max_bytes"],
  "container": "zip",
  "container_is_implicit": true,
  "part_naming": "{base}.part{NN}{ext}",
  "index_entry": "000-INDICE.txt",
  "entry_extension": { "sql": ".sql", "csv": ".csv", "json": ".json", "ndjson": ".ndjson" }
}
```

`single_char_options` te dice qué campos deben validarse como **exactamente un carácter** (sin
hardcodearlo). `container_is_implicit: true` significa que **multiarchivo ⇒ zip aunque se pida
`compression: "none"`**: avisale al usuario que va a bajar un `.zip`, porque el backend no lo
rechaza, lo resuelve.

#### d) Los códigos de error salen de `error_codes`

`capabilities.error_codes` es la lista de los códigos estables. Usala para que tu mapa de
mensajes falle ruidosamente si el backend agrega uno que no manejás.

### 2.4 La asimetría de consistencia por motor

| Motor | Datos | Estructura |
|---|---|---|
| PostgreSQL | consistente | **consistente** |
| MySQL / MariaDB | consistente | **NO consistente** |

En MySQL/MariaDB el snapshot de InnoDB es MVCC **de filas**: el diccionario de datos no
participa, y congelarlo exigiría bloquear las escrituras del servidor entero (misma limitación
que `mysqldump --single-transaction`).

**Lo que la UI tiene que hacer:**
- El `preview` devuelve el aviso en `warnings` — **mostralos todos**, no solo el primero.
- Si el job termina con `structure_drift_detected: true`, el esquema cambió **durante** la
  corrida: mostrá una banda de advertencia junto a la descarga. No invalida el artefacto (los
  datos siguen siendo consistentes) pero el operador tiene que enterarse.

---

## 3. Los 12 endpoints

Todos bajo sesión admin (`AdminDep`). Todos devuelven `ApiResponse[T]` **salvo `download` y
`content`**, que son entregas de archivo/texto y lo dicen abajo.

| # | Método y ruta | Límite | Toca el motor |
|---|---|---|---|
| 1 | `GET /servers/{sid}/databases/{db}/export-capabilities` | 30/min | 🔌 |
| 2 | `POST /servers/{sid}/databases/{db}/database-exports` | 10/min | 🔌 |
| 3 | `GET /database-exports/{id}/objects` | 10/min | 🔌 |
| 4 | `POST /database-exports/{id}/resolve-selection` | 10/min | 🔌 |
| 5 | `POST /database-exports/{id}/preview` | 10/min | 🔌 |
| 6 | `POST /database-exports/{id}/execute` | **3/min** | 🔌 |
| 7 | `GET /database-exports/{id}` | — | no |
| 8 | `GET /database-exports/{id}/items` | — | no |
| 9 | `POST /database-exports/{id}/cancel` | — | no |
| 10 | `GET /database-exports/{id}/manifest` | — | no |
| 11 | `GET /database-exports/{id}/download` | **3/min** | no |
| 12 | `GET /database-exports/{id}/content` | **3/min** | no |

> **7, 8, 9 y 10 no tienen límite a propósito.** El 7 es el que consultás cada 2–3 s mientras
> dura el job; el 9 detiene una exportación que está degradando el origen y no puede quedar
> bloqueado por una cuota.

---

### 3.1 `GET /servers/{sid}/databases/{db}/export-capabilities` 🔌🔒

Lo primero que llama la pantalla. Ver [§2.3](#23-cómo-armar-el-formulario-sin-hardcodear-ni-una-regla).

**Respuesta (recortada; la estructura completa está en §2.3):**

```jsonc
{
  "data": {
    "engine": "mysql",
    "engine_version": "8.0.36",
    "scope": { "kind": "database", "name": "tienda", "scope_note": null },
    "object_types": ["event", "routine", "table", "trigger", "view"],
    "formats": [
      { "name": "sql",    "supports_structure": true,            "supports_data": true,  "one_file_per_table": false },
      { "name": "csv",    "supports_structure": false,           "supports_data": true,  "one_file_per_table": true  },
      { "name": "json",   "supports_structure": "manifest_only", "supports_data": true,  "one_file_per_table": false },
      { "name": "ndjson", "supports_structure": "manifest_only", "supports_data": true,  "one_file_per_table": false }
    ],
    "options": { "...": "ver §2.3" },
    "compatibility": [ "...ver §2.3" ],
    "csv_dialect": { "...": "ver §2.3" },
    "packaging":   { "...": "ver §2.3" },
    "limits": {
      "inline_max_bytes": 1048576, "max_statement_bytes": 1048576, "rows_per_statement": 200,
      "plan_ttl_hours": 24, "artifact_ttl_minutes": 30,
      "max_duration_seconds": 14400, "max_parts": 500
    },
    "error_codes": ["export.artifact_consumed", "export.artifact_expired", "export.data_without_structure",
                    "export.fingerprint_changed", "export.incompatible_option", "export.inline_too_large",
                    "export.invalid_row_filter", "export.missing_dependencies", "export.quota_exceeded"],
    "charset_collation_catalog_url": "/api/v1/charset-collation-options?family=mysql"
  }
}
```

En PostgreSQL cambian, y **solo**, estas cuatro cosas: `object_types` (agrega
`materialized_view`, `sequence`, `enum_type`, `extension`; no trae `event`),
`options["sanitize.definer"].applicable = false` con `default: "keep"`, `scope.scope_note`
(cadena que explica que solo se cubre el schema `public`) y la `family` de la URL de charsets.

---

### 3.2 `POST /servers/{sid}/databases/{db}/database-exports` 🔌🔒 → **201**

Crea el **plan**. **El cuerpo *es* el `ExportSpec`**: el servidor y la base salen de la ruta.
Todos los bloques son opcionales y tienen defaults, así que `{}` es un cuerpo válido.

**Request completo (todos los campos, con sus defaults):**

```jsonc
{
  "format": "sql",                              // sql | csv | json | ndjson

  "structure": {
    "scope_ddl": "NONE",                        // NONE | CREATE | DROP_CREATE | CREATE_IF_NOT_EXISTS
    "entity_ddl": "CREATE",                     // idem
    "drop_if_exists": true,
    "drop_cascade": false,
    "confirm_scope_drop": null                  // OBLIGATORIO si scope_ddl == DROP_CREATE
  },

  "selection": {                                // conjunto ESTRUCTURA
    "mode": "all",                              // all | include | all_except
    "types": [], "names": [],
    "include_patterns": [], "exclude_patterns": []
  },

  "data": {                                     // conjunto DATOS (⊆ estructura)
    "mode": "none",                             // none | all | include | all_except
    "names": [], "include_patterns": [], "exclude_patterns": [],
    "insert_variant": "insert",                 // none | insert | insert_ignore | replace | upsert
    "rows_per_statement": 200,
    "max_statement_bytes": 1048576,
    "include_column_list": true,
    "per_object": {
      "pedidos": { "where": "created_at >= '2026-01-01'", "limit": 100000 }
    }
  },

  "sanitize": {
    "script_comments": true,                    // encabezado y separadores DEL SCRIPT
    "object_comments": true,                    // COMMENT del ESQUEMA  ← opción SEPARADA
    "definer": "auto",                          // keep | omit | replace | auto
    "definer_value": null,                      // obligatorio si definer == replace
    "autoincrement": "auto",                    // keep | omit | auto
    "engine_specific_options": false,
    "partitions": true,
    "constraints_placement": "deferred",        // inline | deferred
    "session_preamble": true,
    "transaction_wrap": false,
    "charset_override": { "mode": "keep", "charset": null, "collation": null }  // keep | override
  },

  "csv": {                                      // solo si format == "csv"
    "delimiter": ",", "quote_char": "\"", "escape_char": null,
    "line_terminator": "lf",                    // lf | crlf
    "header": true, "null_representation": "", "bom": false
  },

  "output": {
    "organization": "single",                   // single | per_object
    "split_max_bytes": null,
    "compression": "none",                      // none | gzip | zip
    "filename_template": "{database}-{date}-{job_id}",
    "file_encoding": "utf-8",           // utf-8 | utf-8-sig | latin-1 | cp1252 (whitelist)
    "delivery": "file",                         // file | inline
    "binary_encoding": "hex",                   // hex | base64   (csv/json/ndjson)
    "schema_manifest": false                    // solo json/ndjson
  },

  "on_error": "continue",                       // stop | continue
  "idempotency_key": null
}
```

**Respuesta**: `ExportSummary` (ver [§8](#8-tipos-referencia-rápida)) con `status: "pending"`.

**`output.file_encoding`** es una **whitelist**: `utf-8`, `utf-8-sig`, `latin-1`, `cp1252` (y sus
alias `utf8` / `latin1` / `iso-8859-1` / `windows-1252`). Cualquier otra es **422
`export.incompatible_option`** con `field: "output.file_encoding"` y la lista en `allowed`. El
motivo no es purismo: el artefacto se codifica **por trozo**, así que un códec con estado
(`utf-16`, `utf-32`) incrusta su marca de orden de bytes en **cada** escritura y el archivo sale
corrupto — con un sha256 que igual lo declara íntegro. Ofrecé un selector cerrado, no un campo
de texto.

**`data.per_object.{tabla}.where`** no puede contener **ningún comentario** (`--`, `/*`, `*/`, y
`#` en MySQL/MariaDB): **422 `export.invalid_row_filter`** con `reason: "comment_not_allowed"`.
Validalo también del lado del cliente para avisar al escribir. Y tené en cuenta que el filtro se
inserta **entre paréntesis** y la consulta lleva `ORDER BY` y `LIMIT` detrás, así que el `limit`
que confirmaste es el que se aplica.

**`filename_template`** admite **solo** estos tokens: `{database}`, `{object}`, `{date}`,
`{time}`, `{job_id}`. Cualquier otro, o una llave suelta, es **422
`export.incompatible_option`** con `unknown_tokens` y `allowed` en el `public_context` — usá esa
lista para el autocompletado.

**`idempotency_key`**: reenviar la **misma** clave con el **mismo** spec devuelve el plan ya
creado (no dispara una segunda lectura del catálogo); con un spec distinto es **409
`export.idempotency_conflict`**, que trae el `export_job_id` del plan original. Útil para que un
doble clic o un reintento de red no genere dos planes.

---

### 3.3 `GET /database-exports/{id}/objects` 🔌🔒

Catálogo en vivo. Query: `?page=&size=` + `?object_type=` + `?name_like=`.

> **No usa el envelope paginado estándar.** La respuesta lleva metadatos de catálogo que una
> lista plana no transporta; la paginación viaja **dentro** del objeto (`total`, `page`, `size`).

```jsonc
{
  "data": {
    "engine": "mysql",
    "database": "tienda",
    "scope_note": null,
    "object_types": ["event", "routine", "table", "trigger", "view"],
    "counts_by_type": { "table": 42, "view": 6, "routine": 3, "trigger": 2 },
    "objects": [
      { "object_type": "table", "name": "clientes",
        "estimated_rows": 15234, "size_bytes": null,
        "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci",
        "has_primary_key": true, "has_triggers": false,
        "is_materialized": null, "row_filter": false }
    ],
    "total": 53, "page": 1, "size": 20,
    "excluded_internal": ["_gw_v_tienda"]
  }
}
```

- `estimated_rows` es una **estimación del catálogo** (`TABLE_ROWS` / `reltuples`), no un conteo
  exacto: contar de verdad exigiría recorrer cada tabla. Etiquetalo como aproximado (`~15 K`).
- `size_bytes` es **hoy siempre `null`**. No construyas nada que dependa de él.
- `has_primary_key: false` → esa tabla, si lleva datos, sale **sin orden garantizado**
  (`deterministic: false` en el preview). Marcá el ícono.
- `excluded_internal` son las tablas de contabilidad del gateway (`_gw_v_`, `_gw_stg_`) que se
  descartan **siempre**. Mostralas en un pie de lista para que nadie las busque en el artefacto
  y crea que se perdieron.

---

### 3.4 `POST /database-exports/{id}/resolve-selection` 🔌🔒

Resuelve las dos selecciones y el cierre de dependencias **sin congelar nada**. Es el
"seleccioná uno y traé lo necesario".

```jsonc
// request — los tres campos son opcionales; null = usa lo que tiene el plan
{ "selection": { "mode": "include", "names": ["pedidos"] },
  "data":      { "mode": "include", "names": ["pedidos"] },
  "auto_resolve_dependencies": false }
```

```jsonc
// 200
{ "data": {
    "structure": [ { "object_type": "table", "name": "pedidos" } ],
    "data": ["pedidos"],
    "added": [],
    "excluded_by_dependency": [],
    "edges": [ { "from_type": "table", "from_name": "pedidos",
                 "to_type": "table", "to_name": "clientes",
                 "reason": "foreign_key", "authoritative": true } ],
    "advisory": [],
    "excluded_internal": [],
    "unknown_names": [],
    "warnings": [] } }
```

**La política depende de quién eligió** (y la UI tiene que reflejarlo):

- **Selección explícita** (`mode: "include"`) a la que le falta una dependencia → **422
  `export.missing_dependencies`** con `missing_dependencies` + `suggested_names`. **No se
  recorta en silencio.** Mostrá un diálogo *"Faltan estos objetos: … ¿los agrego?"* y reintentá
  con `auto_resolve_dependencies: true`; lo agregado vuelve en **`added`** (mostralo, no lo
  escondas).
- **Selección automática** (`all`, `all_except`, patrones) → el backend **poda** y devuelve
  `excluded_by_dependency`. Mostralo como *"se excluyeron N objetos porque una dependencia suya
  quedó fuera"*.

`edges` (con `authoritative: true`) es el grafo firme para dibujar el árbol; `advisory` son
referencias detectadas dentro de cuerpos (best-effort) — presentalas como sugerencias, no como
obligaciones.

---

### 3.5 `POST /database-exports/{id}/preview` 🔌🔒

**El endpoint más importante de la pantalla.** Valida el spec entero, **congela** la selección y
emite el `confirm_token`.

> Solo sobre un plan **`pending`**. Sobre un job ya ejecutado responde **409
> `export.already_executed`**: re-previsualizar reescribiría `spec`/selección/`fingerprint`/token
> y el `GET /manifest` dejaría de describir el artefacto entregado. Para volver a exportar,
> creá un plan nuevo con `POST /servers/.../database-exports` (es barato).

```jsonc
// request — todo opcional
{ "spec": { "...": "un ExportSpec completo que REEMPLAZA al del plan; null = usa el guardado" },
  "auto_resolve_dependencies": false,
  "dry_run_only": false,
  "include_sample": false }
```

```jsonc
// 200
{ "data": {
    "job_id": 12,
    "engine": "mysql",
    "database": "tienda",
    "format": "sql",
    "scope_note": null,
    "objects": [
      { "seq": 1, "object_type": "table", "name": "clientes", "phase": "structure",
        "step": 30, "with_data": true, "estimated_rows": 15234, "deterministic": true },
      { "seq": 2, "object_type": "table", "name": "auditoria", "phase": "structure",
        "step": 30, "with_data": true, "estimated_rows": 900, "deterministic": false }
    ],
    "data_tables": ["auditoria", "clientes"],
    "estimated_rows": 16134,
    "estimated_bytes": 2411520,
    "inline_delivery_viable": false,
    "inline_max_bytes": 1048576,
    "warnings": [
      "En MySQL/MariaDB la consistencia de punto único cubre los DATOS pero no la ESTRUCTURA: …",
      "Tablas sin clave primaria seleccionadas para datos (auditoria): sus filas salen sin orden garantizado…"
    ],
    "advisories": [
      { "when": { "engine": "postgresql", "structure.scope_ddl": "DROP_CREATE" },
        "forbids": [], "requires": [],
        "reason": "…", "blocking": false, "code": "export.incompatible_option" }
    ],
    "excluded_by_dependency": [],
    "sample": null,
    "confirm_token": "9f2c…"
  } }
```

Puntos que la UI **tiene que** honrar:

- **`objects` viene en el orden EXACTO en que va a salir en el artefacto.** `seq` es 1..N y
  `step` es la fuente de verdad del orden (`phase` es solo una etiqueta legible). No lo
  reordenes alfabéticamente: el orden es una garantía del backend, no un detalle de
  presentación.
- **`deterministic: false`** en un objeto = esa tabla sale sin orden garantizado (sin PK y sin
  una tupla de columnas ordenable). Ícono de advertencia en la fila.
- **`warnings` es una lista y hay que mostrarla entera.** Ahí viven los avisos de consistencia,
  de tablas sin PK, de que el artefacto va a salir en `.zip` aunque se pidiera sin comprimir, y
  de un filtro `where` definido para una tabla que no está en la selección de datos.
- **`advisories`** son reglas de la matriz que se cumplen pero **no bloquean** (`blocking:
  false`). Mostralas como avisos, no como errores.
- **`inline_delivery_viable: false`** ⇒ si el usuario eligió `delivery: "inline"`, avisale
  **ahora**: al descargar sería un 409 y ya habría pagado la lectura completa del origen.
  `estimated_bytes` es una estimación **gruesa** (filas × ancho nominal); presentala como "≈".
- **`confirm_token` es `null` cuando `dry_run_only: true`** — y ese es el punto: valida y reporta
  sin congelar nada. Usalo para refrescar el panel de consecuencias en cada cambio del
  formulario, y llamá al preview "de verdad" (sin `dry_run_only`) solo cuando el usuario
  confirma.
- **`sample` es hoy siempre `null`.** No construyas una vista previa del contenido.

**Cada preview no-`dry_run` reemplaza el token anterior.** Si el usuario vuelve atrás, cambia
algo y vuelve a previsualizar, guardá el token nuevo.

---

### 3.6 `POST /database-exports/{id}/execute` 🔌🔒

```jsonc
{ "confirm_target_name": "tienda",     // el nombre real de la base, re-tecleado
  "confirm_token": "9f2c…" }           // el del ÚLTIMO preview de ESTE plan
```

Devuelve el `ExportSummary` de inmediato, con `status` `pending` o `running`. **Encola**; no
esperes a que termine.

Los cinco controles corren **en este orden**: TTL del plan → estado → nombre re-tecleado →
re-snapshot del catálogo (anti-TOCTOU) → token. Y el worker vuelve a comprobar el fingerprint al
arrancar (cuarta comprobación), porque entre la confirmación y el arranque real puede haber
tiempo de cola.

**Errores propios**: `409 export.already_executed` (el plan ya se usó — hay que crear uno nuevo),
`409 export.not_previewed`, `422 export.incompatible_option` con `field: "confirm_target_name"`
o `field: "confirm_token"`, `409 export.fingerprint_changed`, `409 export.quota_exceeded`,
`410 export.artifact_expired` (plan vencido).

---

### 3.7 `GET /database-exports/{id}` 🔒 — **polling**

Sin rate limit. Consultá cada 2–3 s mientras `status ∈ {pending, running}`.

```jsonc
{ "data": {
    "id": 12, "server_id": 3, "database_name": "tienda", "database_id": 7,
    "engine": "mysql", "format": "sql",
    "status": "running", "phase": "data",
    "progress": {
      "phase": "data",
      "objects": 18, "rows": 9400, "statements": 412,
      "tables_with_data": 5, "bytes": 1884160,
      "warnings": [],
      "generator_version": "1.0",
      "engine_version": "8.0.36",
      "degradations": []
    },
    "error": null,
    "expired": false,
    "structure_drift_detected": false,
    "has_resolved_selection": true,
    "idempotency_key": null,
    "created_at": "2026-08-16T14:00:00Z", "expires_at": "2026-08-17T14:00:00Z",
    "started_at": "2026-08-16T14:03:11Z", "finished_at": null } }
```

- `phase` ∈ `preamble` | `scope` | `prerequisites` | `structure` | `data` | `constraints` |
  `bodies` | `epilogue` | `done`. Es lo que va en la barra de progreso; **no hay porcentaje**
  (no se sabe el total real de bytes de antemano), así que usá una barra indeterminada con el
  nombre de la fase y los contadores.
- `progress` se persiste **throttleado a ~3 s**: no esperes un cambio en cada llamada.
- `progress.degradations` lista las garantías que **no** se pudieron aplicar (por ejemplo, que el
  motor rechazó el `SET idle_in_transaction_session_timeout`). Si viene no vacía, mostrala.
- Al terminar, `progress.artifact` trae `{ byte_size, sha256, part_count }` y `elapsed_ms`.

---

### 3.8 `GET /database-exports/{id}/items` 🔒

Paginado estándar (`paginated()`), en orden de emisión.

```jsonc
{ "data": [
    { "id": 1, "job_id": 12, "seq": 1, "object_type": "table", "object_name": "clientes",
      "phase": "structure", "status": "ok", "reason": null,
      "rows_exported": 15234, "bytes_written": 1201234, "deterministic": true,
      "execution_ms": 3120, "executed_at": "2026-08-16T14:03:20Z" },
    { "id": 2, "job_id": 12, "seq": 2, "object_type": "view", "object_name": "v_ventas",
      "phase": "bodies", "status": "skipped", "reason": "manifest_only",
      "rows_exported": null, "bytes_written": 0, "deterministic": null,
      "execution_ms": 0, "executed_at": "…" } ],
  "meta": { "page": 1, "size": 20, "total": 2, "...": "…" } }
```

`status` ∈ `ok` | `error` | `skipped`. **`reason` es de vocabulario cerrado** y nunca el mensaje
del driver (podría incrustar valores de filas). Traducilo a texto amigable:

| `reason` | Qué decir |
|---|---|
| `structure_disabled` | «No se exportó su definición porque `entity_ddl` es `NONE`.» |
| `no_ddl_rendered` | «El gateway no pudo generar el DDL de este objeto.» |
| `all_columns_generated` | «Todas sus columnas son generadas: no hay nada que insertar.» |
| `manifest_only` | «Este formato no transporta estructura; el objeto figura solo en el manifiesto.» |
| `format_data_only` | «Este formato solo transporta datos.» |
| `unsupported_type:<tipo>` | «Hay un valor de tipo `<tipo>` que no se puede serializar.» |

---

### 3.9 `POST /database-exports/{id}/cancel` 🔒

Cancelación **cooperativa**: el worker corta en el próximo punto seguro, cierra la transacción
contra el origen y **descarta el artefacto parcial**. El job pasa a `canceled`. Sin rate limit.

**409 `export.not_cancellable`** con `status` si el job ya terminó.

---

### 3.10 `GET /database-exports/{id}/manifest` 🔒

Inventario verificable **sin abrir el archivo** — mirar el contenido para saber qué se llevó
sería una segunda divulgación.

```jsonc
{ "data": {
    "job_id": 12, "engine": "mysql", "engine_version": "8.0.36",
    "database": "tienda", "format": "sql",
    "complete": true,
    "structure_drift_detected": false,
    "generator_version": "1.0",
    "spec": { "...": "el ExportSpec completo, tal como se ejecutó" },
    "objects": [
      { "object_type": "table", "name": "clientes", "status": "ok",
        "rows_exported": 15234, "bytes_written": 1201234,
        "deterministic": true, "reason": null } ],
    "total_rows": 16134,
    "byte_size": 2380112,
    "sha256": "3b1f…",
    "part_count": 1,
    "created_at": "…", "expires_at": "…" } }
```

`sha256` es el mismo valor que viaja en el `ETag` y en la cabecera `X-Export-Sha256` de la
descarga: ofrecé un botón "copiar checksum" para que el operador pueda verificar el archivo que
bajó.

---

### 3.11 `GET /database-exports/{id}/download` 🔒 — **sin `ApiResponse`**

Devuelve el archivo (`FileResponse`), con:

| Cabecera | Contenido |
|---|---|
| `Content-Disposition` | `attachment; filename="tienda-20260816-12.sql"` |
| `Content-Type` | `application/sql` \| `text/csv` \| `application/json` \| `application/x-ndjson` \| `application/zip` \| `application/gzip` |
| `Content-Length` | tamaño real |
| `ETag` | `"<sha256>"` |
| `X-Export-Sha256` | el sha256 sin comillas |
| `X-Export-Complete` | `"true"` \| `"false"` |
| `Accept-Ranges` | `bytes` |

- **`X-Export-Complete: false` ⇒ el artefacto es PARCIAL.** El archivo además lleva la marca por
  dentro, pero la UI tiene que advertirlo **antes** de que el usuario lo ejecute.
- **Descarga reanudable** vía `Range` (`206`, `416`, `If-Range`).
- **Un solo uso**: al completarse la entrega el archivo se borra y el artefacto pasa a
  `consumed`. Un segundo intento es **410 `export.artifact_consumed`**. Una descarga
  **genuinamente parcial** **no** lo consume.

  Lo que decide es si el rango **cubre todo el archivo**, no la presencia de la cabecera: un
  `Range: bytes=0-` baja el artefacto entero y **sí lo consume**. Si estás implementando
  reanudación, pedí rangos acotados (`bytes=<desde>-<hasta>`) y contá con que el último trozo
  —el que completa el archivo— lo va a consumir. Lo que no se puede interpretar (multi-rango,
  otra unidad) se trata como parcial. El contador de descargas se incrementa **en los dos casos**.
- **Decile al usuario, antes del clic, que solo puede bajarlo una vez** y cuánto le queda de TTL
  (`limits.artifact_ttl_minutes` + `manifest.expires_at`). Es la diferencia entre una UX honesta
  y un ticket de soporte.
- Cada descarga **queda auditada** antes de que salga un solo byte. Decilo en pantalla.

---

### 3.12 `GET /database-exports/{id}/content` 🔒 — **sin `ApiResponse`**

El artefacto como `text/plain`, sin envolver, para copiar al portapapeles. Mismas cabeceras
`ETag` / `X-Export-Sha256` / `X-Export-Complete`.

**409 `export.inline_too_large`** si supera `limits.inline_max_bytes`, con `byte_size` real e
`inline_max_bytes` en el `public_context`. **Nunca se trunca en silencio**: un script cortado que
alguien pega y ejecuta es peor que un fallo. El botón "copiar" debería estar deshabilitado ya
desde el preview cuando `inline_delivery_viable` es `false`.

---

## 4. Máquina de estados del job

```
                       POST .../execute
   [ (no existe) ] ──POST database-exports──▶ pending ──────────────▶ running
                                                │                       │
                                    plan vencido│                       ├──▶ succeeded ──▶ (artefacto: available)
                                    (expired:   │                       ├──▶ failed        │        │
                                     true)      │                       ├──▶ canceled      │        ├─ download ──▶ consumed
                                                │                       └──▶ interrupted   │        └─ TTL / purga ▶ purged
                                                └──▶ (se descarta creando un plan nuevo)
```

**Estados del job** (`status`):

| Estado | Qué es | Qué muestra la UI |
|---|---|---|
| `pending` | plan creado, aún no ejecutado (o encolado y esperando worker) | «Listo para exportar» / «En cola» |
| `running` | el worker está generando | barra indeterminada + `phase` + contadores |
| `succeeded` | terminó sin ningún objeto en error | botón de descarga habilitado |
| `failed` | al menos un objeto falló, o la corrida abortó | `error` + enlace al reporte de incidencias. **Puede haber artefacto parcial** |
| `canceled` | cancelación cooperativa; el parcial se descartó | «Cancelado». Sin artefacto |
| `interrupted` | el proceso del gateway se reinició a mitad | «Interrumpido por un reinicio del gateway; volvé a crear el plan». Sin artefacto |

**Estados del artefacto** (vía `manifest` / los errores de descarga):

| Estado | Cómo se ve desde el cliente |
|---|---|
| `available` | la descarga funciona |
| `consumed` | `410 export.artifact_consumed` |
| `purged` / vencido | `410 export.artifact_expired` |
| nunca existió | `409 export.no_artifact` |

Y hay **dos vencimientos distintos**, no los mezcles:

- el del **PLAN** (`expires_at` del summary, `limits.plan_ttl_hours` = 24 h) → afecta a
  `preview`/`execute`;
- el del **ARTEFACTO** (`limits.artifact_ttl_minutes` = 30 min desde que el job termina) →
  afecta a `download`/`content`.

---

## 5. Flujos completos, paso a paso

### 5.1 Camino feliz: volcado completo a SQL

```
GET  /api/v1/servers/3/databases/tienda/export-capabilities
POST /api/v1/servers/3/databases/tienda/database-exports
     { "format": "sql",
       "structure": { "scope_ddl": "NONE", "entity_ddl": "CREATE" },
       "selection": { "mode": "all" },
       "data": { "mode": "all" } }                          → 201 { id: 12, status: "pending" }
GET  /api/v1/database-exports/12/objects?page=1&size=50      → catálogo para el árbol
POST /api/v1/database-exports/12/preview  { }                → objects[], warnings[], confirm_token
POST /api/v1/database-exports/12/execute
     { "confirm_target_name": "tienda", "confirm_token": "9f2c…" }   → status: "running"
GET  /api/v1/database-exports/12   (cada 2–3 s)              → … → status: "succeeded"
GET  /api/v1/database-exports/12/manifest                    → sha256, byte_size, objetos
GET  /api/v1/database-exports/12/download                    → el archivo (un solo uso)
```

### 5.2 Solo esquema, apto para control de versiones

```jsonc
{ "format": "sql",
  "structure": { "scope_ddl": "NONE", "entity_ddl": "CREATE" },
  "selection": { "mode": "all" },
  "data": { "mode": "none" },
  "sanitize": { "script_comments": false },     // ← saca la fecha: dos volcados son idénticos
  "output": { "organization": "per_object" } }  // ← un archivo por objeto → .zip
```

Con `script_comments: false` el artefacto es **byte a byte idéntico** entre dos corridas sin
cambios de esquema, así que se puede commitear y diffear. Vale la pena ofrecerlo como un preset
"Esquema para el repositorio".

### 5.3 Solo datos de tres tablas, con filtro

```jsonc
{ "format": "sql",
  "structure": { "scope_ddl": "NONE", "entity_ddl": "NONE" },   // ← modo "solo datos"
  "selection": { "mode": "include", "names": [] },
  "data": { "mode": "include", "names": ["pedidos", "items", "clientes"],
            "insert_variant": "upsert",
            "per_object": { "pedidos": { "where": "created_at >= '2026-01-01'", "limit": 50000 } } } }
```

Con ambos `*_ddl` en `NONE` la restricción de subconjunto **no** aplica. El `where` se valida
antes de tocar el motor: tiene que ser una condición de lectura simple **sobre esa misma tabla**
(sin subconsultas, sin CTEs, sin referencias a otras tablas).

### 5.4 Selección parcial que arrastra dependencias

```
POST /database-exports/12/resolve-selection
     { "selection": { "mode": "include", "names": ["pedidos"] } }
  → 422 export.missing_dependencies
     public_context.missing_dependencies = [ { "object_type": "table", "name": "clientes" } ]
     public_context.suggested_names      = ["clientes", "pedidos"]

  [ diálogo: «"pedidos" depende de "clientes". ¿Lo agrego?» ]

POST /database-exports/12/resolve-selection
     { "selection": { "mode": "include", "names": ["pedidos"] },
       "auto_resolve_dependencies": true }
  → 200  structure = [pedidos, clientes],  added = [ { table, clientes } ]
```

Mostrá lo que vino en `added` con un distintivo: el usuario no lo eligió.

### 5.5 El esquema cambió entre el preview y el execute

```
POST /database-exports/12/execute { … }
  → 409 export.fingerprint_changed   (field: "confirm_token")
```

No es recuperable con un reintento: hay que **volver a previsualizar** (el catálogo cambió y la
selección congelada puede describir objetos que ya no existen). Ofrecé un botón "Volver a
previsualizar" que reenvíe el mismo spec al preview y actualice el token.

### 5.6 Entrega en línea sobre el tope

```
POST /database-exports/12/preview { }
  → inline_delivery_viable: false, estimated_bytes: 2411520, inline_max_bytes: 1048576
     warnings: ["La entrega en línea admite hasta 1048576 bytes y la estimación es de 2411520…"]

  [ si el usuario ejecuta igual y después llama a /content ]
GET /database-exports/12/content
  → 409 export.inline_too_large  { byte_size: 2380112, inline_max_bytes: 1048576 }
```

Lo correcto es no dejar llegar hasta ahí: con `inline_delivery_viable: false`, cambiá el control
de entrega a "archivo" y explicá por qué.

### 5.7 Módulo apagado

```
cualquier endpoint → 409 { "public_context": { "code": "export.disabled" } }
```

`EXPORT_ENABLED=False` es el kill switch. Mostrá un estado vacío explicativo ("la exportación
está deshabilitada en este gateway") y **ocultá el punto de entrada** en la navegación, no un
error por cada clic.

---

## 6. Matriz de errores

Todos los códigos viajan en **`detail.public_context.code`**, que **se ve también en
producción** — a diferencia de `detail.context`, que solo existe en `development`. Nunca
dependas de `context`.

Forma del error:

```jsonc
{ "detail": {
    "msg": "El formato 'csv' no admite sentencias de estructura.",
    "type": "AppHttpException",
    "public_context": { "code": "export.incompatible_option",
                        "field": "structure.entity_ddl",
                        "format": "csv",
                        "allowed": ["NONE"] } } }
```

### 6.1 Los once códigos estables

| Código | HTTP | Dónde aparece | Claves extra de `public_context` | Qué hacer |
|---|---|---|---|---|
| `export.incompatible_option` | 422 | create, preview, execute | `field` (ruta con puntos), `allowed[]`, `fields[]` (todos los campos culpables), + las claves del `when` de la regla | Marcá el control `field` en rojo con `msg`. Si aparece, tu evaluador de la matriz ([§2.3](#23-cómo-armar-el-formulario-sin-hardcodear-ni-una-regla)) tiene un bug: **loguealo**. También cubre `confirm_target_name`, `confirm_token`, `definer_value`, el dialecto csv y `filename_template` (con `unknown_tokens`) |
| `export.data_without_structure` | 422 | resolve-selection, preview | `field: "data.names"`, `data_without_structure[]` | Ofrecé agregar esas tablas a la estructura, o pasar a modo "solo datos" |
| `export.missing_dependencies` | 422 | resolve-selection, preview | `field: "selection.names"`, `missing_dependencies[{object_type,name}]`, `suggested_names[]` | Diálogo «faltan estos objetos» → reintentar con `auto_resolve_dependencies: true` |
| `export.invalid_row_filter` | 422 | preview | `field: "data.per_object.<tabla>.where"`, `table`, `reason`, a veces `limit`/`danger`/`reasons[]` | Marcá el campo `where` de **esa** tabla. Ver la tabla de `reason` abajo. **El backend no devuelve tu texto** (regla anti-reflexión): mostralo desde tu propio estado |
| `export.inline_too_large` | 409 | content | `field: "output.delivery"`, `byte_size`, `inline_max_bytes` | Cambiá a descarga como archivo. **Nunca lo trunques vos** |
| `export.fingerprint_changed` | 409 | execute | `field: "confirm_token"` | Botón "Volver a previsualizar". No reintentes automáticamente |
| `export.artifact_expired` | 410 | preview/execute (plan vencido), download, content | — | Si es el plan: crear uno nuevo. Si es el artefacto: volver a exportar |
| `export.artifact_consumed` | 410 | download, content | — | «Ya se descargó una vez». Ofrecé volver a exportar |
| `export.quota_exceeded` | 409 | execute | `running`, `limit` | «Hay N exportaciones en curso o en cola (máx. M)». Reintentá más tarde. `running` cuenta el trabajo **admitido** (cola + ejecución), no solo lo que corre |
| `export.idempotency_conflict` | 409 | create | `field: "idempotency_key"`, `export_job_id` | La clave ya se usó con otras opciones. Usá `export_job_id` para llevar al plan original |
| `export.scope_not_allowed` | 409 | capabilities, create | — | El destino es la **propia base de metadatos del gateway** y no se puede exportar. No es reintentable ni configurable: sacá esa base del selector |

### 6.2 Códigos internos del ciclo de vida

No están en `capabilities.error_codes` pero los vas a recibir:

| Código | HTTP | Cuándo |
|---|---|---|
| `export.disabled` | 409 | kill switch `EXPORT_ENABLED=False` — en **cualquier** endpoint |
| `export.already_executed` | 409 | `execute` **o `preview`** sobre un plan que ya no está `pending` (trae `status`). Un job es de un solo uso: re-previsualizar uno ya ejecutado reescribiría la selección congelada y el `manifest` dejaría de describir el artefacto entregado. Creá un plan nuevo |
| `export.not_previewed` | 409 | `execute` sin haber previsualizado |
| `export.not_ready` | 409 | `download`/`content` con el job en `pending`/`running` (trae `status`) |
| `export.no_artifact` | 409 | el job terminó sin producir artefacto (`canceled`, `interrupted`, un `failed` temprano) |
| `export.not_cancellable` | 409 | `cancel` sobre un job ya terminado (trae `status`) |
| `export.not_owner` | 403 | la exportación la creó otro administrador |

Y los de siempre: **401** sin sesión, **404** job o base inexistente, **429** rate limit.

### 6.3 Los `reason` de `export.invalid_row_filter`

| `reason` | Qué decir |
|---|---|
| `empty_filter` | «El filtro está vacío.» |
| `too_long` | «El filtro supera los `limit` caracteres.» |
| `unparseable` | «No se pudo interpretar la condición.» |
| `multiple_statements` | «Escribí una sola condición, sin `;`.» |
| `not_read_only` | «La condición no es de solo lectura.» |
| `subquery_not_allowed` | «No se admiten subconsultas, CTEs ni `UNION`.» |
| `foreign_table_reference` | «La condición solo puede referirse a esta tabla.» |
| `foreign_column_qualifier` | «Hay columnas calificadas con otra tabla o base.» |
| `comment_not_allowed` | «El filtro no puede contener comentarios (`--`, `/* */`, y `#` en MySQL/MariaDB).» Validá esto también del lado del cliente para dar el aviso al escribir |

---

## 7. Interpretación visual: pantallas y estados

**Pantalla sugerida: un asistente de 4 pasos + una vista de job.**

1. **Origen y formato** — servidor y base ya vienen del contexto. Selector de formato desde
   `capabilities.formats`. Al elegir formato, re-evaluá la matriz: `csv` va a apagar toda la
   sección de estructura, forzar "un archivo por tabla" y deshabilitar la entrega en línea.
   Mostrá `scope.scope_note` si no es `null` (PostgreSQL).
2. **Qué exportar** — árbol del catálogo (`/objects`) con las dos columnas de casillas
   ([§2.1](#21-dos-conjuntos-estructura-y-datos)), buscador (`name_like`), filtro por tipo y
   conteos (`counts_by_type`). Botón "Resolver dependencias" → `/resolve-selection`. Pie con
   `excluded_internal`.
3. **Opciones** — generado **enteramente** desde `capabilities.options`, agrupado por el prefijo
   de la ruta (`structure.`, `data.`, `sanitize.`, `csv.`, `output.`). Panel lateral vivo con el
   resultado de `preview` en modo `dry_run_only`.
4. **Confirmar** — resumen del plan (`objects` **en orden**, `data_tables`, `estimated_rows`,
   `estimated_bytes`), **todos** los `warnings` y `advisories`, el campo de
   `confirm_target_name` y —si corresponde— el de `confirm_scope_drop`. Botón "Exportar".

**Vista del job**: barra indeterminada con la `phase` en texto, contadores de
`progress` (objetos / filas / bytes), botón "Cancelar", y al terminar: checksum, tamaño, botón
de descarga con el aviso de **un solo uso** y el TTL restante, botón "Ver reporte" (`/items`) y
banda de advertencia si `structure_drift_detected` o `X-Export-Complete: false`.

**Señales que no pueden faltar en pantalla:**

| Señal | De dónde sale | Cómo se ve |
|---|---|---|
| «Esto extrae datos en claro y queda auditado» | fijo | banda permanente en el paso 4 y junto al botón de descarga |
| Aviso de consistencia (MySQL/MariaDB) | `preview.warnings` | banda ámbar en el paso 4 |
| Tabla sin orden garantizado | `objects[].deterministic === false` | ícono por fila + resumen |
| El artefacto va a salir en `.zip` | `preview.warnings` + `packaging.container_is_implicit` | nota junto al selector de organización |
| La entrega en línea no es viable | `preview.inline_delivery_viable` | el control de entrega salta a "archivo" y explica |
| Opción no aplicable a este motor | `options[x].applicable === false` | control oculto o deshabilitado con tooltip |
| Valor destructivo | `options[x].destructive` | opción en rojo + confirmación extra |
| El esquema cambió durante la corrida | `structure_drift_detected` | banda ámbar sobre la descarga |
| Artefacto parcial | `X-Export-Complete: false` / `manifest.complete === false` | banda roja **antes** de descargar |
| Garantía degradada | `progress.degradations[]` | lista en el detalle del job |

---

## 8. Tipos (referencia rápida)

```ts
type Format        = "sql" | "csv" | "json" | "ndjson";
type ScopeDdl      = "NONE" | "CREATE" | "DROP_CREATE" | "CREATE_IF_NOT_EXISTS";
type EntityDdl     = ScopeDdl;
type SelectionMode = "all" | "include" | "all_except";
type DataMode      = "none" | SelectionMode;
type InsertVariant = "none" | "insert" | "insert_ignore" | "replace" | "upsert";
type DefinerMode   = "keep" | "omit" | "replace" | "auto";
type Autoincrement = "keep" | "omit" | "auto";
type Constraints   = "inline" | "deferred";
type Organization  = "single" | "per_object";
type Compression   = "none" | "gzip" | "zip";
type Delivery      = "file" | "inline";
type BinaryEnc     = "hex" | "base64";
type CharsetMode   = "keep" | "override";
type LineTerm      = "lf" | "crlf";
type OnError       = "stop" | "continue";          // ⚠️ el valor es "continue", sin guión bajo

type JobStatus  = "pending" | "running" | "succeeded" | "failed" | "canceled" | "interrupted";
type JobPhase   = "preamble" | "scope" | "prerequisites" | "structure" | "data"
                | "constraints" | "bodies" | "epilogue" | "done";
type ItemStatus = "ok" | "error" | "skipped";
type ArtifactState = "available" | "consumed" | "purged";

interface ExportSummary {
  id: number; server_id: number; database_name: string; database_id: number | null;
  engine: string; format: Format;
  status: JobStatus; phase: JobPhase | null;
  progress: ExportProgress | null;
  error: string | null;
  expired: boolean;                      // el PLAN venció
  structure_drift_detected: boolean;
  has_resolved_selection: boolean;       // false = todavía no se previsualizó
  idempotency_key: string | null;
  created_at: string; expires_at: string;        // vencimiento del PLAN
  started_at: string | null; finished_at: string | null;
}

interface ExportProgress {
  phase: JobPhase;
  objects: number; rows: number; statements: number;
  tables_with_data: number; bytes: number;
  warnings: string[];
  generator_version: string;
  engine_version?: string;
  degradations?: string[];
  artifact?: { byte_size: number | null; sha256: string | null; part_count: number | null };
  elapsed_ms?: number;
}

interface ExportPlannedObject {
  seq: number; object_type: string; name: string;
  phase: string; step: number;            // step = orden real; phase = etiqueta legible
  with_data: boolean; estimated_rows: number | null;
  deterministic: boolean;
}

interface ExportCompatibilityRule {
  when: Record<string, string>;           // puede traer la clave especial "engine"
  forbids: string[]; requires: string[];
  reason: string; blocking: boolean; code: string;
}

interface ExportOption {
  values: string[];
  default: string | boolean | number | null;   // string en enums, boolean en flags
  applicable: boolean;
  destructive: string[];
}
```

---

## 9. Notas transversales

### 9.1 El envelope y los errores

Idénticos al documento original y a las precisiones de
[`api-reference-v8.md` §3.0](api-reference-v8.md#30-envelope-y-errores-tres-precisiones-que-valen-para-todo-el-módulo):
sin campo `success`, en producción los errores traen solo `detail.msg` (+ `public_context`) y
`X-Request-ID` viaja en el header. **Guardá el `X-Request-ID` de todo fallo de este módulo**: es
la única forma de que el backend pueda correlacionar un job fallido con su traza (el `error` del
job es deliberadamente acotado y nunca trae el mensaje del motor).

### 9.2 Las dos rutas que NO devuelven `ApiResponse`

`download` y `content`. No las pases por tu interceptor genérico de respuestas: una espera un
`Blob`, la otra texto plano. Precedente en el proyecto: el `export` de schema-comparisons.

### 9.3 Rate limiting

`execute`, `download` y `content` son **3/min**. Un usuario que hace clic dos veces en descargar
ya consumió dos. Deshabilitá el botón mientras la petición está en vuelo y mostrá el `429` como
«esperá un momento», no como un error.

### 9.4 El riesgo que la UI tiene que mostrar: no hay enmascarado

El módulo **no tiene enmascarado de datos**: lo que sale, sale en claro. No es un descuido, es un
alcance decidido y registrado. Los controles compensatorios son la confirmación de doble factor,
el TTL corto, la descarga de un solo uso y —sobre todo— **la auditoría de cada descarga**.

Consecuencias concretas para el frontend:

- una banda **permanente** (no un tooltip) en el paso de confirmación y junto al botón de
  descarga: *«Esta exportación extrae los datos sin enmascarar. Cada descarga queda registrada
  en la auditoría.»*;
- nunca renderices el contenido del artefacto en pantalla "para revisar" — la única vía de
  lectura es `/content`, que también audita y tiene tope;
- el modo "solo estructura" (`data.mode: "none"`) debería ser el **default visual** del
  formulario: es el caso seguro, y quien necesita datos sabe que los necesita.

---

## 10. Checklist de implementación

- [ ] Llamar a `/export-capabilities` **primero** y construir el formulario entero desde ahí.
- [ ] Implementar el evaluador de `compatibility` de [§2.3](#23-cómo-armar-el-formulario-sin-hardcodear-ni-una-regla)
      (`when` + `forbids` con `=valor` y `*` + `requires` + `blocking`) y **no escribir ningún
      `if (format === …)` fuera de él**.
- [ ] Respetar `applicable: false` (ocultar) y `destructive` (marcar en rojo).
- [ ] Modelar estructura y datos como **dos** conjuntos, con la excepción "solo datos".
- [ ] `scope_ddl`/`entity_ddl` como **un** control de cuatro opciones, nunca dos casillas.
- [ ] Campo `confirm_scope_drop` cuando `scope_ddl === "DROP_CREATE"`.
- [ ] Usar `preview` con `dry_run_only: true` para el panel vivo, y sin él solo al confirmar.
- [ ] Mostrar **todos** los `warnings` y los `advisories` del preview.
- [ ] Renderizar `objects` **en el orden que llega**; ícono en los `deterministic: false`.
- [ ] Manejar `inline_delivery_viable: false` antes de ejecutar.
- [ ] Polling de `GET /{id}` cada 2–3 s con barra indeterminada + `phase` + contadores.
- [ ] Botón "Cancelar" visible durante todo `running`.
- [ ] Traducir los `reason` de `/items` (vocabulario cerrado, tabla de [§3.8](#38-get-database-exportsiditems-)).
- [ ] Avisar de **un solo uso** y del **TTL** antes del clic de descarga; mostrar el `sha256`.
- [ ] Banda de advertencia con `structure_drift_detected` y con `X-Export-Complete: false`.
- [ ] Mapear los 10 códigos estables + los 7 internos; loguear cualquier
      `export.incompatible_option` como bug propio.
- [ ] Guardar el `X-Request-ID` de cada fallo.
- [ ] Banda permanente sobre la extracción en claro y la auditoría.
- [ ] Manejar `export.disabled` ocultando el punto de entrada, no con un error por clic.
