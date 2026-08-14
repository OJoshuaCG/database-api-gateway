# API Reference v7 — Catálogo de charsets/collations: selector en la creación de BDs + pantalla de administración

> **Guía para el equipo de frontend.** Addendum de [`api-reference.md`](api-reference.md),
> [`api-reference-v2.md`](api-reference-v2.md), [`api-reference-v3.md`](api-reference-v3.md),
> [`api-reference-v4.md`](api-reference-v4.md), [`api-reference-v5.md`](api-reference-v5.md) y
> [`api-reference-v6.md`](api-reference-v6.md).
>
> Este documento tiene **las dos naturalezas** de la serie a la vez:
>
> - Como **v5**: corrige un supuesto de una pantalla **ya especificada** (el formulario de
>   creación de bases de datos, que hoy trata `charset`/`collation` como texto libre). No es
>   una feature aislada: es un cambio de contrato que vuelve inválido lo que ese formulario
>   asume.
> - Como **v6**: describe un módulo **nuevo** que nunca fue expuesto al frontend (la pantalla
>   de administración del catálogo). Ahí no hay nada que ajustar, hay algo que diseñar.
>
> Mismo formato que v3/v4/v5/v6: **problema → qué debe pasar → escenarios → flujos →
> endpoints → ejemplos → interpretación visual**.
>
> Convenciones (base URL `/api/v1`, envelope `ApiResponse[T]`, auth por cookie, errores,
> paginación) idénticas al documento original ([§3](api-reference.md#3-convenciones-de-la-api)).
>
> Documentación de ingeniería del mismo módulo (más detalle interno del que el frontend
> necesita): [`docs/features/server-database-lifecycle.md`](features/server-database-lifecycle.md),
> sección *"Catálogo de charsets/collations"*.

**Versión de la API:** `v1` · 🔌 = lee/toca el servidor de BD destino · 🔒 = requiere sesión admin

---

## ⚠️ Corrección sobre un documento anterior

[`docs/frontend/plan-server-database-lifecycle.md`](frontend/plan-server-database-lifecycle.md)
declara, cerca del final:

> **`[SUPUESTO S8]` — No existe ningún endpoint que liste los `charset`/`collation`/`encoding`
> válidos del motor.** […] los campos se implementan como entrada de texto libre con
> **sugerencias estáticas** por motor […] nunca como un selector cerrado — un selector cerrado
> excluiría valores válidos del servidor concreto. Confirmar si se planea un endpoint de
> catálogo del motor.

**Ese supuesto ya no es válido.** Ahora existe un endpoint de catálogo
(`GET /charset-collation-options`) y, más importante, **el backend rechaza con `422` todo lo
que no esté en él**. Las consecuencias son directas:

1. Un campo de texto libre con sugerencias estáticas **produce errores evitables**: el usuario
   tipea `latin1_swedish_ci` (una sugerencia perfectamente razonable, y de hecho una fila
   sembrada del catálogo) y la creación falla porque esa fila está **deshabilitada**.
2. La objeción original del supuesto —*"un selector cerrado excluiría valores válidos del
   servidor concreto"*— **sigue siendo cierta y ahora es una decisión deliberada del backend**.
   El catálogo es una política del gateway: lo que no está habilitado no se puede elegir,
   aunque el motor lo soporte. La forma de agregar un valor válido ya no es tipearlo en el
   formulario, es darlo de alta en la pantalla de administración (§6).
3. La ayuda en línea de PostgreSQL sobre el locale **se mantiene tal cual**. El catálogo no
   reemplaza esa advertencia: ver [§3.4](#34-postgresql-el-catálogo-no-garantiza-que-el-locale-exista).

Los supuestos S1–S7 y S9–S10 de ese documento no se ven afectados.

---

## Índice

- [0. El problema: por qué existe el catálogo](#0-el-problema-por-qué-existe-el-catálogo)
- [1. Alcance: qué cubre y qué NO cubre](#1-alcance-qué-cubre-y-qué-no-cubre)
  - [1.1 Endpoints de OTROS módulos que estas pantallas necesitan](#11-endpoints-de-otros-módulos-que-estas-pantallas-necesitan)
- [2. Conceptos del catálogo](#2-conceptos-del-catálogo)
  - [2.1 `engine_family`: el frontend debe mapear el motor](#21-engine_family-el-frontend-debe-mapear-el-motor)
  - [2.2 `collation: null` no es lo mismo que "vacío"](#22-collation-null-no-es-lo-mismo-que-vacío)
  - [2.3 `enabled` vs. `is_default`](#23-enabled-vs-is_default)
  - [2.4 El seed inicial (qué va a ver el operador la primera vez)](#24-el-seed-inicial-qué-va-a-ver-el-operador-la-primera-vez)
- [3. Semántica de validación: los cuatro casos](#3-semántica-de-validación-los-cuatro-casos)
  - [3.1 Árbol de decisión](#31-árbol-de-decisión)
  - [3.2 Lo que se guarda NO es siempre lo que se envió](#32-lo-que-se-guarda-no-es-siempre-lo-que-se-envió)
  - [3.3 Dónde SÍ y dónde NO se aplica el enforcement](#33-dónde-sí-y-dónde-no-se-aplica-el-enforcement)
  - [3.4 PostgreSQL: el catálogo no garantiza que el locale exista](#34-postgresql-el-catálogo-no-garantiza-que-el-locale-exista)
- [4. El `422` de combinación no habilitada (forma exacta)](#4-el-422-de-combinación-no-habilitada-forma-exacta)
- [5. Endpoints del catálogo (`/charset-collation-options`)](#5-endpoints-del-catálogo-charset-collation-options)
  - [5.1 `GET /charset-collation-options`](#51-get-charset-collation-options-)
  - [5.2 `POST /charset-collation-options`](#52-post-charset-collation-options-)
  - [5.3 `PATCH /charset-collation-options/{option_id}`](#53-patch-charset-collation-optionsoption_id-)
  - [5.4 No hay `DELETE` (y no es un olvido)](#54-no-hay-delete-y-no-es-un-olvido)
- [6. Endpoints de creación de BD afectados](#6-endpoints-de-creación-de-bd-afectados)
  - [6.1 `POST /servers/{server_id}/databases`](#61-post-serversserver_iddatabases-)
  - [6.2 `POST /managed-databases`](#62-post-managed-databases-)
- [7. Lo que el `PATCH` de metadata NO hace](#7-lo-que-el-patch-de-metadata-no-hace)
- [8. Flujos completos, paso a paso](#8-flujos-completos-paso-a-paso)
  - [8.1 Crear una BD sin elegir nada](#81-crear-una-bd-sin-elegir-nada)
  - [8.2 Crear una BD eligiendo del selector](#82-crear-una-bd-eligiendo-del-selector)
  - [8.3 Combinación no habilitada: el `422` y cómo la UI se recupera](#83-combinación-no-habilitada-el-422-y-cómo-la-ui-se-recupera)
  - [8.4 Habilitar una combinación deshabilitada para poder usarla](#84-habilitar-una-combinación-deshabilitada-para-poder-usarla)
  - [8.5 Deshabilitar la que es default: el `422` de invariante](#85-deshabilitar-la-que-es-default-el-422-de-invariante)
  - [8.6 Agregar una combinación custom](#86-agregar-una-combinación-custom)
- [9. Interpretación visual: pantallas, estados y transiciones](#9-interpretación-visual-pantallas-estados-y-transiciones)
- [10. Tipos (referencia rápida)](#10-tipos-referencia-rápida)
- [11. Matriz de errores](#11-matriz-de-errores)
- [12. Supuestos sobre el frontend actual (a confirmar)](#12-supuestos-sobre-el-frontend-actual-a-confirmar)
- [13. Checklist de implementación](#13-checklist-de-implementación)

---

## 0. El problema: por qué existe el catálogo

Hasta este cambio, `charset` y `collation` eran **texto libre** en los endpoints de creación
de bases de datos. Lo que el usuario tipeaba viajaba —con una validación de forma mínima o
ninguna— hasta el `CREATE DATABASE` del servidor destino. Eso producía tres problemas
distintos, y solo uno de ellos era de seguridad:

**1. Deriva de configuración.** Nada impedía crear la BD de un cliente con `latin1`, la de
otro con `utf8mb3` y la de un tercero con `utf8mb4_general_ci`. Todas "funcionan" hasta que
alguien intenta guardar un emoji, o hasta que dos bases que deberían ser gemelas comparan
strings distinto. El gateway administra BDs que **replican blueprints**: una diferencia de
collation entre dos bases del mismo blueprint es una bomba de tiempo, no una preferencia.

**2. Errores tardíos y confusos.** El error de un charset inválido llegaba **desde el motor**,
después de abrir la conexión, en el idioma del motor y en cualquiera de sus formatos. En
PostgreSQL un locale inexistente todavía llega así (§3.4), pero un `utf8mb4_0900_ai_ci` contra
MariaDB —que no tiene esa collation— ya no debería llegar nunca, porque esa fila está
deshabilitada por defecto en el catálogo.

**3. Superficie de inyección en PostgreSQL.** En MySQL/MariaDB el charset y la collation viajan
al DDL como **identificadores** y el adapter los revalida contra una whitelist estricta. En
PostgreSQL el locale es un nombre del sistema operativo (`en_US.UTF-8`, `de_DE@euro`): lleva
puntos, guiones y arrobas, así que **no se puede whitelistear como identificador** y viaja como
literal de string. Acotarlo a un valor que salió de una tabla administrada cierra esa
superficie en vez de taparla con un patrón cada vez más permisivo.

La solución es un **catálogo global** —una allowlist administrable— que se consulta **antes de
tocar el motor**:

> El operador ve la lista completa de combinaciones, decide cuáles se pueden elegir, marca una
> como sugerida, y puede agregar las que su despliegue necesite. Todo lo que no esté
> **habilitado** recibe `422` y **el `CREATE DATABASE` nunca se emite**.

**Actor:** el admin único del gateway (sesión admin por cookie). No hay roles ni multi-tenant.

---

## 1. Alcance: qué cubre y qué NO cubre

### Cubre

- Un catálogo **global** (no por servidor) de combinaciones `charset` + `collation`, agrupadas
  por **familia de motor**.
- Lectura del catálogo, completa o filtrada, para poblar un selector.
- Administración: habilitar, deshabilitar, marcar como sugerida por defecto, y dar de alta
  combinaciones que el seed no trae.
- Validación **previa al motor** en los dos endpoints de creación de bases de datos, con un
  error que **enumera qué sí se puede elegir**.

### NO cubre

- **No es un catálogo por servidor.** Es global. Habilitar `utf8mb4_0900_ai_ci` (que solo
  existe en MySQL 8+) la habilita también para los servidores MariaDB del inventario, donde la
  creación fallará **en el motor**. El catálogo no sabe qué versión corre cada servidor.
- **No consulta el motor.** No hay ningún endpoint que devuelva `SHOW COLLATION` de un servidor
  concreto. El catálogo es una lista curada a mano, no un reflejo de la realidad remota.
- **No cambia bases ya creadas.** No hay ninguna operación en este módulo —ni en ningún otro—
  que altere el charset o la collation de una base de datos existente en el motor. Ver
  [§7](#7-lo-que-el-patch-de-metadata-no-hace), que es el punto más importante de este
  documento para no prometer de más.
- **No aplica al clonado ni a la adopción.** Ahí no se *elige* un charset: se replica o se
  registra el que ya existe. Ver [§3.3](#33-dónde-sí-y-dónde-no-se-aplica-el-enforcement).
- **No hay borrado de filas del catálogo.** Ver [§5.4](#54-no-hay-delete-y-no-es-un-olvido).
- **No hay paginación** en el listado del catálogo: devuelve la lista completa en `data`.

### 1.1 Endpoints de OTROS módulos que estas pantallas necesitan

Los tres endpoints de §5 son todo lo que este módulo expone. La pantalla de creación de BD
necesita además esto, que ya está documentado en otro lado y **cuyo contrato no se repite acá**:

| Para | Endpoint | Por qué | Documentado en |
|---|---|---|---|
| Saber el **motor** del servidor | `GET /servers` / `GET /servers/{id}` 🔒 | El campo `engine` (`mysql` \| `mariadb` \| `postgresql`) es lo que la UI mapea a `engine_family` **antes** de pedir el catálogo. Sin esto no se puede poblar el selector. | [`api-reference.md` §6](api-reference.md#6-servidores-servers) |
| Selector de **propietario** (si `register=true`) | `GET /server-users?server_id=` 🔒 | Requisito preexistente del formulario, no cambia. | [`api-reference.md` §7](api-reference.md#7-usuarios-del-motor-server-users-y-serversidusers) |
| Ver el charset **registrado** de una BD | `GET /managed-databases/{id}` 🔒 | Devuelve `charset`/`collation` tal como quedaron guardados. Ojo con lo que eso significa: [§3.2](#32-lo-que-se-guarda-no-es-siempre-lo-que-se-envió). | [`api-reference.md` §9](api-reference.md#9-bases-de-datos-gestionadas-managed-databases) |

---

## 2. Conceptos del catálogo

Una fila del catálogo es una **combinación permitida**, no un charset y una collation sueltos:

| Campo | Tipo | Qué es |
|---|---|---|
| `id` | `number` | Identificador de la fila. Es lo que va en la URL del `PATCH`. |
| `engine_family` | `string` | `"mysql"` o `"postgresql"`. Solo esos dos valores. |
| `charset` | `string` | MySQL/MariaDB: el `CHARACTER SET`. PostgreSQL: el `ENCODING`. |
| `collation` | `string \| null` | MySQL/MariaDB: el `COLLATE`. PostgreSQL: el **locale** del SO (fija `LC_COLLATE` y `LC_CTYPE`). `null` = "este charset sin collation específica". |
| `enabled` | `boolean` | Si el gateway permite **elegirla** al crear una BD. |
| `is_default` | `boolean` | Si es la **sugerencia** de su familia. A lo sumo una por familia. |
| `created_at` / `updated_at` | `datetime` | Solo lectura. |

### 2.1 `engine_family`: el frontend debe mapear el motor

Hay **dos** familias, no tres. MySQL y MariaDB comparten catálogo de charsets y collations, así
que caen en la misma:

| `engine` del servidor | → `engine_family` para el catálogo |
|---|---|
| `mysql` | `mysql` |
| `mariadb` | **`mysql`** |
| `postgresql` | `postgresql` |

> **Este mapeo es responsabilidad del frontend.** El endpoint del catálogo recibe
> `engine_family`, no `engine`. Mandar `?engine_family=mariadb` devuelve **`422`** con
> `public_context.allowed: ["mysql", "postgresql"]`, no una lista vacía. Es un error fácil de
> cometer y fácil de detectar: si el selector aparece vacío o rompe en un servidor MariaDB,
> revisar este mapeo primero.

Dónde etiquetarlo en la UI: la familia `mysql` es un detalle interno del catálogo. En la
pantalla de administración conviene mostrarla como **"MySQL / MariaDB"**, no como "mysql".

### 2.2 `collation: null` no es lo mismo que "vacío"

`collation: null` es una fila **legítima y completa** del catálogo: significa *"este charset,
con la collation que el motor decida"*. No es un dato faltante ni una fila a medio cargar.

- Al **listarla**: mostrar algo como `utf8mb4 — (collation por defecto del motor)`, nunca un
  guion suelto ni una celda vacía que parezca un error.
- Al **darla de alta**: se manda `collation: null` (o se omite el campo).
- Internamente la tabla guarda un centinela `""`, pero **la API siempre expone `null`**. El
  frontend nunca ve la cadena vacía.

### 2.3 `enabled` vs. `is_default`

Se confunden fácil y hacen cosas distintas:

| | `enabled` | `is_default` |
|---|---|---|
| Qué controla | Si la combinación **aparece** en el selector de creación | Cuál viene **preseleccionada** en ese selector |
| Cuántas por familia | Las que sean | **A lo sumo una** (puede no haber ninguna) |
| Efecto en el backend | Es lo que decide el `422` | **Ninguno.** Es solo una sugerencia para la UI |
| Invariante | — | Un default **debe** estar habilitado |

> 🚨 **`is_default` NO significa "lo que el gateway manda si no elegís nada".** El backend
> **nunca rellena** un valor que el cliente no envió. Si el usuario deja los campos vacíos, el
> request va con `charset: null, collation: null` y **el motor** aplica su propio default (que
> puede o no coincidir con la fila `is_default` del catálogo). La fila `is_default` es
> exclusivamente una instrucción para la UI: *"preseleccioná esta"*. Si la UI la preselecciona
> y la manda explícitamente, se aplica; si la UI no manda nada, no se aplica.

Corolario para el diseño: **preseleccionar la fila `is_default` y enviarla explícitamente** es
la forma de que la elección sea determinista. Dejar el selector vacío también es válido —es el
caso "no me importa"— pero entonces el resultado depende del motor, no del catálogo.

### 2.4 El seed inicial (qué va a ver el operador la primera vez)

El catálogo se siembra solo en el arranque, de forma idempotente y **sin pisar los toggles del
operador**: un reinicio nunca vuelve a habilitar lo que alguien deshabilitó. Estas son las
ocho filas iniciales, y son un buen conjunto de datos para maquetar la pantalla:

| Familia | charset | collation | `enabled` | `is_default` | Por qué |
|---|---|---|---|---|---|
| mysql | `utf8mb4` | `utf8mb4_unicode_ci` | ✅ | ⭐ | Recomendada |
| mysql | `utf8mb4` | `utf8mb4_general_ci` | ✅ | | Alternativa habitual |
| mysql | `utf8mb4` | `utf8mb4_0900_ai_ci` | ❌ | | Solo MySQL 8+; **rompe en MariaDB** |
| mysql | `utf8mb3` | `utf8mb3_general_ci` | ❌ | | Legado, no cubre Unicode completo |
| mysql | `latin1` | `latin1_swedish_ci` | ❌ | | Legado |
| postgresql | `UTF8` | `en_US.UTF-8` | ✅ | ⭐ | Recomendada |
| postgresql | `UTF8` | `C` | ❌ | | Ordenamiento binario |
| postgresql | `UTF8` | `C.UTF-8` | ❌ | | Depende del SO del servidor |

**Las filas deshabilitadas están ahí a propósito**: son referencia, no basura. Existen en el
motor, el operador las ve y decide explícitamente si quiere ofrecerlas. La pantalla de
administración debe mostrarlas —atenuadas, pero presentes— y no filtrarlas por default.

---

## 3. Semántica de validación: los cuatro casos

Esta es la sección que hay que leer dos veces. La regla **no** es "el par tiene que estar en la
lista": depende de cuántos de los dos campos se envían.

### 3.1 Árbol de decisión

```
¿Se envió charset?  ¿Se envió collation?
        NO                  NO        →  ✅ NO SE VALIDA NADA.
                                          El motor aplica su propio default
                                          (utf8mb4 en MySQL/MariaDB, UTF8 en PostgreSQL).
                                          Este caso NUNCA da 422.

        SÍ                  NO        →  ✅ si ALGUNA combinación habilitada usa ese charset.
                                          La collation la elige el motor.
                                          El gateway NO rellena la collation que no se pidió.

        NO                  SÍ        →  ✅ si ALGUNA combinación habilitada tiene esa
                                          collation exacta.
                                          El gateway NO rellena el charset que no se pidió.

        SÍ                  SÍ        →  ✅ solo si el PAR EXACTO está habilitado.
```

En tabla, con el seed de §2.4 sobre un servidor MySQL:

| Se envía | Resultado | Por qué |
|---|---|---|
| nada | **201** | No se valida |
| `charset: "utf8mb4"` | **201** | Hay filas habilitadas con ese charset |
| `charset: "UTF8MB4"` | **201** | El nombre del charset se compara **sin distinguir mayúsculas** |
| `collation: "utf8mb4_general_ci"` | **201** | Hay una fila habilitada con esa collation |
| `charset: "utf8mb4", collation: "utf8mb4_general_ci"` | **201** | El par está habilitado |
| `charset: "utf8mb4", collation: "utf8mb4_0900_ai_ci"` | **422** | La fila existe pero está **deshabilitada** |
| `charset: "latin1"` | **422** | Ninguna fila habilitada usa ese charset |
| `charset: "utf8mb4", collation: "latin1_swedish_ci"` | **422** | El par no existe |

> **Sobre mayúsculas:** el nombre del **charset** se compara sin distinguir mayúsculas en las
> dos familias. La **collation** se compara sin distinguir mayúsculas solo en la familia
> `mysql` (es un identificador del motor); en `postgresql` se compara **tal cual**, porque es
> el nombre de un locale del sistema operativo y ahí el case sí importa: `en_US.UTF-8` y
> `en_us.utf-8` no son lo mismo. Implicación para la UI: en PostgreSQL, **no normalices** el
> texto del locale antes de enviarlo.

### 3.2 Lo que se guarda NO es siempre lo que se envió

El backend responde y persiste la **forma canónica del catálogo**, no el texto del request. Eso
tiene dos consecuencias visibles para la UI:

1. **Normalización de mayúsculas.** Se envía `charset: "UTF8MB4"` → se guarda `"utf8mb4"` (el
   valor tal como está en la fila del catálogo). No es un bug: es lo que hace que al DDL siempre
   llegue un valor que salió de la tabla y no del teclado del usuario.
2. **El campo que no se envió queda `null` en el inventario.** Si se creó con `charset:
   "utf8mb4"` y sin collation, la BD queda registrada con `collation: null` — aunque el motor
   sí le haya aplicado una. El inventario guarda **lo que se eligió**, no lo que el motor
   resolvió.

> **Implicación de UI:** después de crear, no compares lo que mostrás con lo que enviaste.
> Refrescá desde la respuesta. Y si la pantalla de detalle de una BD muestra `collation: null`,
> el copy correcto es *"no se especificó (la definió el motor)"*, **no** *"sin collation"*.

Hay además un caso donde la respuesta **no dice nada**: `POST /servers/{id}/databases` con
`register: false` devuelve solo `{database, engine, registered, managed_database_id}` — **no
eco del charset aplicado**. Si la pantalla quiere confirmar visualmente qué se aplicó, tiene
que mostrar lo que el usuario eligió en el formulario, o crear con `register: true` (que sí
devuelve el objeto completo del inventario).

### 3.3 Dónde SÍ y dónde NO se aplica el enforcement

Esta tabla evita el error más costoso: asumir que el catálogo gobierna todo lo que tenga un
campo llamado `charset`.

| Operación | ¿Valida contra el catálogo? | Nota |
|---|---|---|
| `POST /servers/{id}/databases` (con y sin `register`) | ✅ **Sí** | Camino principal del formulario |
| `POST /managed-databases` (con y sin `?provision=true`) | ✅ **Sí** | También sin provisionar: no tiene sentido registrar una elección que el gateway rechazaría después |
| `PATCH /managed-databases/{id}` | ❌ **No** | Y **tampoco toca el motor**. Ver [§7](#7-lo-que-el-patch-de-metadata-no-hace) |
| `POST /managed-databases/adopt` | ❌ **No** | La BD ya existe; el charset es metadata informativa |
| Clonado de BDs (`/database-clones`) | ❌ **No** | La BD destino hereda el charset del **origen**: se replica, no se elige |

### 3.4 PostgreSQL: el catálogo no garantiza que el locale exista

El catálogo es **global** y el locale de PostgreSQL pertenece al **sistema operativo de cada
servidor**. Que `en_US.UTF-8` esté habilitada en el catálogo no significa que exista en el
servidor donde se está creando la base.

Si no existe, PostgreSQL responde `invalid locale name` y ese error nativo llega **del motor**,
después de que el catálogo dio el visto bueno.

> **La advertencia en línea del formulario de PostgreSQL sobre el locale sigue siendo
> obligatoria.** El catálogo reduce la superficie de error, no la elimina. El copy sugerido en
> [`plan-server-database-lifecycle.md`](frontend/plan-server-database-lifecycle.md) §4.2 sigue
> vigente; lo único que cambia es que ahora el campo es un selector y no un input libre.

---

## 4. El `422` de combinación no habilitada (forma exacta)

Este error es **la pieza central del manejo de fallos** de este módulo, porque trae consigo la
solución al problema que reporta.

Cuerpo exacto de la respuesta, **tal como llega en producción**:

```json
{
  "detail": {
    "msg": "La combinación charset/collation no está habilitada en el catálogo del gateway.",
    "type": "AppHttpException",
    "public_context": {
      "engine_family": "mysql",
      "requested": { "charset": "latin1", "collation": "latin1_swedish_ci" },
      "allowed": [
        { "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci", "is_default": true },
        { "charset": "utf8mb4", "collation": "utf8mb4_general_ci", "is_default": false }
      ],
      "truncated": false
    }
  }
}
```

| Campo | Para qué sirve en la UI |
|---|---|
| `detail.msg` | Mensaje ya redactado en español. Se puede mostrar tal cual. |
| `public_context.requested` | Lo que se pidió, **sin normalizar**. Útil para resaltar el campo culpable. |
| `public_context.allowed` | **Lo importante.** Las combinaciones habilitadas de esa familia, con su `is_default`. Es la misma información que devuelve `GET …?only_enabled=true`, así que sirve para **repoblar el selector en el acto** sin una segunda llamada. |
| `public_context.truncated` | `true` = la lista se cortó en 50 filas. Mostrar *"hay más opciones disponibles"* y remitir al selector completo. |

### Dos advertencias sobre el envelope de error

1. **`public_context` viaja en TODOS los ambientes.** Es deliberado: sin él, en producción el
   operador recibiría *"no está habilitada"* sin saber qué sí puede elegir. Es seguro
   depender de este campo.
2. **`context` y `loc` NO existen fuera de desarrollo.** Si alguna prueba interna mostró un
   `context` o un `loc` con la misma información, era porque el gateway corría con
   `APP_ENV=development`. **Nunca leas de ahí.** Todo lo que la UI necesita está en
   `public_context`.

Los demás errores de este módulo comparten la misma forma (`detail.msg` + `detail.type` +
`detail.public_context` opcional). Ver [§11](#11-matriz-de-errores).

---

## 5. Endpoints del catálogo (`/charset-collation-options`)

Ninguno de los tres toca un servidor de BD: operan solo sobre la base de metadatos del gateway.
Por eso llevan 🔒 y no 🔌. Los tres **auditan** (la modificación del catálogo cambia qué DDL
podrá emitir el gateway más adelante, aunque no emita ninguno ahora).

### 5.1 `GET /charset-collation-options` 🔒

Listado. **Sin paginación**: devuelve la lista completa en `data`.

**Query params**

| Param | Tipo | Default | Qué hace |
|---|---|---|---|
| `engine_family` | `string` | *(sin filtro)* | `mysql` \| `postgresql`. Cualquier otro valor → `422` |
| `only_enabled` | `boolean` | `false` | `true` = solo las que se pueden elegir |

**Los dos usos, que son distintos:**

| Pantalla | Llamada | Por qué |
|---|---|---|
| **Selector** del formulario de creación | `GET /charset-collation-options?engine_family=mysql&only_enabled=true` | Solo lo elegible, y solo del motor del servidor elegido |
| **Administración** del catálogo | `GET /charset-collation-options` | **Todo**, sin filtros: administrar es ver también lo deshabilitado |

**Orden de la respuesta:** `engine_family`, luego `charset`, luego `collation`. Es un orden
estable y agrupable: la pantalla de administración puede renderizarlo tal cual, agrupado por
familia y por charset, sin reordenar.

**Ejemplo — poblar el selector de un servidor MariaDB**

```http
GET /api/v1/charset-collation-options?engine_family=mysql&only_enabled=true
```

> Nótese `engine_family=mysql` para un servidor **MariaDB**: es el mapeo de [§2.1](#21-engine_family-el-frontend-debe-mapear-el-motor).

```json
{
  "data": [
    {
      "id": 2,
      "engine_family": "mysql",
      "charset": "utf8mb4",
      "collation": "utf8mb4_general_ci",
      "enabled": true,
      "is_default": false,
      "created_at": "2026-08-11T09:00:00Z",
      "updated_at": "2026-08-11T09:00:00Z"
    },
    {
      "id": 1,
      "engine_family": "mysql",
      "charset": "utf8mb4",
      "collation": "utf8mb4_unicode_ci",
      "enabled": true,
      "is_default": true,
      "created_at": "2026-08-11T09:00:00Z",
      "updated_at": "2026-08-11T09:00:00Z"
    }
  ]
}
```

**Ejemplo — pantalla de administración (todo el catálogo)**

```http
GET /api/v1/charset-collation-options
```

```json
{
  "data": [
    { "id": 5, "engine_family": "mysql", "charset": "latin1", "collation": "latin1_swedish_ci", "enabled": false, "is_default": false, "created_at": "2026-08-11T09:00:00Z", "updated_at": "2026-08-11T09:00:00Z" },
    { "id": 4, "engine_family": "mysql", "charset": "utf8mb3", "collation": "utf8mb3_general_ci", "enabled": false, "is_default": false, "created_at": "2026-08-11T09:00:00Z", "updated_at": "2026-08-11T09:00:00Z" },
    { "id": 3, "engine_family": "mysql", "charset": "utf8mb4", "collation": "utf8mb4_0900_ai_ci", "enabled": false, "is_default": false, "created_at": "2026-08-11T09:00:00Z", "updated_at": "2026-08-11T09:00:00Z" },
    { "id": 2, "engine_family": "mysql", "charset": "utf8mb4", "collation": "utf8mb4_general_ci", "enabled": true,  "is_default": false, "created_at": "2026-08-11T09:00:00Z", "updated_at": "2026-08-11T09:00:00Z" },
    { "id": 1, "engine_family": "mysql", "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci", "enabled": true,  "is_default": true,  "created_at": "2026-08-11T09:00:00Z", "updated_at": "2026-08-11T09:00:00Z" },
    { "id": 7, "engine_family": "postgresql", "charset": "UTF8", "collation": "C", "enabled": false, "is_default": false, "created_at": "2026-08-11T09:00:00Z", "updated_at": "2026-08-11T09:00:00Z" },
    { "id": 8, "engine_family": "postgresql", "charset": "UTF8", "collation": "C.UTF-8", "enabled": false, "is_default": false, "created_at": "2026-08-11T09:00:00Z", "updated_at": "2026-08-11T09:00:00Z" },
    { "id": 6, "engine_family": "postgresql", "charset": "UTF8", "collation": "en_US.UTF-8", "enabled": true, "is_default": true, "created_at": "2026-08-11T09:00:00Z", "updated_at": "2026-08-11T09:00:00Z" }
  ]
}
```

**Errores:** `422` si `engine_family` no es `mysql` ni `postgresql`:

```json
{
  "detail": {
    "msg": "engine_family inválida. Use: mysql (cubre MariaDB) o postgresql.",
    "type": "AppHttpException",
    "public_context": { "allowed": ["mysql", "postgresql"] }
  }
}
```

### 5.2 `POST /charset-collation-options` 🔒

Da de alta una combinación que el seed no trae. **Rate limit: `20/minute`.**

**Body**

| Campo | Tipo | Req. | Restricciones |
|---|---|---|---|
| `engine_family` | `string` | ✅ | `mysql` \| `postgresql` (máx. 16) |
| `charset` | `string` | ✅ | 1–64 chars. Debe empezar con letra; después letras, dígitos y `_` |
| `collation` | `string \| null` | ❌ | máx. 128. Default `null` = "sin collation específica" |
| `enabled` | `boolean` | ❌ | **Default `false`** |

Dos cosas que sorprenden:

1. **Nace deshabilitada.** Habilitar es un acto explícito y separado. Si el flujo de la UI es
   "agregar y usar ya", hay que mandar `enabled: true` en el alta, o encadenar un `PATCH`.
2. **No se puede marcar `is_default` en el alta.** El campo no existe en el body. Es
   deliberado: agregar una fila no debe desplazar por accidente el default vigente. Para que
   la nueva sea el default hace falta un `PATCH` posterior.

**Validación de forma de `collation`, distinta por familia:**

| Familia | Patrón aceptado | Ejemplos válidos |
|---|---|---|
| `mysql` | letra inicial, luego letras/dígitos/`_` | `utf8mb4_0900_as_cs` |
| `postgresql` | letra inicial, luego letras/dígitos/`_`/`.`/`@`/`-` | `en_US.UTF-8`, `de_DE@euro`, `C` |

Es una whitelist de forma, no de existencia: acepta cualquier locale con forma plausible, y el
motor sigue teniendo la última palabra.

**Ejemplo — agregar una collation case-sensitive de MySQL 8**

```http
POST /api/v1/charset-collation-options
Content-Type: application/json

{
  "engine_family": "mysql",
  "charset": "utf8mb4",
  "collation": "utf8mb4_0900_as_cs",
  "enabled": true
}
```

```json
{
  "message": "Combinación agregada al catálogo.",
  "data": {
    "id": 9,
    "engine_family": "mysql",
    "charset": "utf8mb4",
    "collation": "utf8mb4_0900_as_cs",
    "enabled": true,
    "is_default": false,
    "created_at": "2026-08-13T14:22:03Z",
    "updated_at": "2026-08-13T14:22:03Z"
  }
}
```

**Error `409` — la combinación ya existe.** Trae el `id` de la fila existente, lo que permite
una recuperación mucho mejor que un mensaje de error:

```json
{
  "detail": {
    "msg": "La combinación ya existe en el catálogo.",
    "type": "AppHttpException",
    "public_context": {
      "id": 5,
      "engine_family": "mysql",
      "charset": "latin1",
      "collation": "latin1_swedish_ci",
      "enabled": false
    }
  }
}
```

> 💡 **Aprovechá el `409`.** El caso real más frecuente es que el operador intente "agregar"
> algo que ya está en el catálogo pero **deshabilitado** (y por eso no lo ve en el selector de
> creación). Con `public_context.id` y `public_context.enabled` la UI puede responder
> *"Esa combinación ya está en el catálogo, pero está deshabilitada. ¿Querés habilitarla?"* con
> un botón que dispare el `PATCH` sobre ese `id` — en vez de un error rojo que deja al usuario
> sin salida.

**Error `422` — nombre inválido.** Trae el patrón en `public_context.pattern`:

```json
{
  "detail": {
    "msg": "Nombre de collation/locale inválido para esa familia de motor.",
    "type": "AppHttpException",
    "public_context": { "pattern": "^[A-Za-z][A-Za-z0-9_]{0,127}$" }
  }
}
```

### 5.3 `PATCH /charset-collation-options/{option_id}` 🔒

Habilita/deshabilita y/o marca como default. **Rate limit: `20/minute`.**

**Body** — parcial; se envían uno o los dos campos:

| Campo | Tipo | Qué hace |
|---|---|---|
| `enabled` | `boolean \| null` | Habilita o deshabilita |
| `is_default` | `boolean \| null` | Marca o desmarca como sugerida de su familia |

**Los dos campos se pueden mandar juntos**, y esa es la forma de resolver las dos situaciones
que de otro modo se traban:

```jsonc
{ "enabled": true, "is_default": true }    // habilitar Y marcar como default en UNA llamada
{ "enabled": false, "is_default": false }  // deshabilitar la que es default, quitándole el default
```

**Las reglas que la UI debe anticipar** (todas se validan sobre el estado **resultante**, no
sobre lo enviado):

| Regla | Qué pasa si se viola |
|---|---|
| A lo sumo **un** `is_default: true` por familia | No es un error: marcar una nueva **desmarca la anterior automáticamente**, en la misma transacción |
| Un default **debe** estar habilitado | `422` |
| Deshabilitar la fila que **es** el default | `422` (el estado resultante sería default + deshabilitada) |
| Marcar como default una fila **deshabilitada** | `422`, **salvo** que el mismo `PATCH` la habilite |
| Body vacío o con los dos campos ausentes | `422` — *"Nada para actualizar"* |
| `option_id` inexistente | `404` |

> ⚠️ **El desmarcado automático del default anterior es silencioso.** El backend no avisa qué
> fila perdió el default; solo devuelve la fila modificada. **La UI debe recargar el listado
> completo después de un `PATCH` con `is_default: true`**, o la fila que perdió la estrella se
> va a seguir mostrando con ella.

> **Una familia puede quedarse sin default.** Es un estado válido: `{"enabled": false,
> "is_default": false}` sobre la única fila default deja la familia sin sugerencia. La UI de
> creación tiene que tolerarlo (selector sin preselección) y la de administración debería
> avisarlo de forma no bloqueante.

**Ejemplo — habilitar y hacer default en una sola llamada**

```http
PATCH /api/v1/charset-collation-options/3
Content-Type: application/json

{ "enabled": true, "is_default": true }
```

```json
{
  "message": "Catálogo actualizado.",
  "data": {
    "id": 3,
    "engine_family": "mysql",
    "charset": "utf8mb4",
    "collation": "utf8mb4_0900_ai_ci",
    "enabled": true,
    "is_default": true,
    "created_at": "2026-08-11T09:00:00Z",
    "updated_at": "2026-08-13T14:31:47Z"
  }
}
```

*(Efecto colateral no visible en esta respuesta: la fila `id: 1` quedó con `is_default: false`.)*

**Error `422` — deshabilitar el default:**

```http
PATCH /api/v1/charset-collation-options/1
{ "enabled": false }
```

```json
{
  "detail": {
    "msg": "Una combinación marcada como default debe estar habilitada; habilítala o designa otro default primero.",
    "type": "AppHttpException",
    "public_context": { "enabled": false, "is_default": true }
  }
}
```

### 5.4 No hay `DELETE` (y no es un olvido)

Deshabilitar (`PATCH {"enabled": false}`) ya saca la combinación del selector de creación, que
es todo lo que se necesita. Conservar la fila mantiene legible el histórico de las bases que se
crearon con ella.

**Implicación de UI:** la pantalla de administración **no debe tener** un botón de eliminar, ni
un ícono de papelera, ni un "archivar". La acción destructiva de este módulo es el interruptor
de habilitado — y ni siquiera es destructiva.

---

## 6. Endpoints de creación de BD afectados

El contrato de estos dos endpoints **no cambió de forma**: los campos `charset` y `collation`
siguen siendo los mismos, opcionales, y en el mismo lugar. Lo que cambió es que ahora **pueden
responder `422` antes de tocar el motor**.

### 6.1 `POST /servers/{server_id}/databases` 🔌🔒

Crea la BD directamente en el servidor. Opcionalmente la registra en el inventario.
**Rate limit: `10/minute`.**

**Body**

| Campo | Tipo | Req. | Nota |
|---|---|---|---|
| `name` | `string` | ✅ | 1–64 |
| `charset` | `string \| null` | ❌ | Validado contra el catálogo |
| `collation` | `string \| null` | ❌ | Validado contra el catálogo |
| `owner` | `string \| null` | ❌ | Solo PostgreSQL (`OWNER` nativo); ignorado en MySQL/MariaDB |
| `register` | `boolean` | ❌ | Default `false`. Si `true`, además registra en el inventario |
| `owner_id` | `number \| null` | ❌ | **Obligatorio si `register: true`** (`422` si falta) |
| `notes` | `string \| null` | ❌ | Solo se guarda si `register: true` |

**Ejemplo — creación con combinación habilitada**

```http
POST /api/v1/servers/3/databases
Content-Type: application/json

{
  "name": "tienda_2026",
  "charset": "utf8mb4",
  "collation": "utf8mb4_unicode_ci",
  "register": true,
  "owner_id": 12,
  "notes": "Instancia de la tienda para el ciclo 2026"
}
```

```json
{
  "message": "Base de datos creada.",
  "data": {
    "database": "tienda_2026",
    "engine": "mariadb",
    "registered": true,
    "managed_database_id": 87
  }
}
```

> Recordá que con `register: false` la respuesta es la misma **sin** `managed_database_id`, y
> que en ningún caso hace eco del charset aplicado ([§3.2](#32-lo-que-se-guarda-no-es-siempre-lo-que-se-envió)).

**Ejemplo — la misma llamada con una combinación deshabilitada** → ver el `422` completo en
[§4](#4-el-422-de-combinación-no-habilitada-forma-exacta).

### 6.2 `POST /managed-databases` 🔌🔒

Crea el registro de inventario. **Aprovisiona en el motor solo con `?provision=true`.**

> ⚠️ **`provision` es un query param y su default es `false`.** Sin él, este endpoint **no
> crea nada en el motor**: solo registra la fila. Es un detalle fácil de pasar por alto y
> produce el síntoma *"la creé pero no existe en el servidor"*. La validación de charset
> contra el catálogo se aplica **en los dos casos**.

**Body**

| Campo | Tipo | Req. | Nota |
|---|---|---|---|
| `name` | `string` | ✅ | Patrón de identificador |
| `server_id` | `number` | ✅ | |
| `owner_id` | `number` | ✅ | `ServerUser` del **mismo** servidor |
| `model_id` | `number \| null` | ❌ | Blueprint a vincular |
| `model_version` | `string \| null` | ❌ | máx. 50 |
| `charset` | `string \| null` | ❌ | Validado contra el catálogo |
| `collation` | `string \| null` | ❌ | Validado contra el catálogo. **Acá sí acepta locales de PostgreSQL** con puntos/guiones/`@` (a diferencia del `PATCH`, ver §7) |
| `notes` | `string \| null` | ❌ | |

**Ejemplo**

```http
POST /api/v1/managed-databases?provision=true
Content-Type: application/json

{
  "name": "reportes_pg",
  "server_id": 5,
  "owner_id": 21,
  "charset": "UTF8",
  "collation": "en_US.UTF-8"
}
```

```json
{
  "message": "Base de datos creada y aprovisionada en el motor.",
  "data": {
    "id": 88,
    "name": "reportes_pg",
    "server_id": 5,
    "owner_id": 21,
    "model_id": null,
    "model_version": null,
    "charset": "UTF8",
    "collation": "en_US.UTF-8",
    "status": "active",
    "notes": null,
    "origin": "provisioned",
    "created_at": "2026-08-13T14:40:11Z",
    "updated_at": "2026-08-13T14:40:12Z"
  }
}
```

`GET /managed-databases` (paginado) y `GET /managed-databases/{id}` devuelven este mismo objeto:
son la fuente para mostrar el charset/collation **registrado** de una BD existente.

---

## 7. Lo que el `PATCH` de metadata NO hace

Esta sección existe para evitar que la UI prometa algo que el backend no da.

```http
PATCH /api/v1/managed-databases/{db_id}
{ "model_id": …, "model_version": …, "charset": …, "collation": …, "notes": … }
```

> 🚨 **Este `PATCH` NO ejecuta ningún DDL.** Cambiar `charset`/`collation` acá **cambia
> únicamente la metadata guardada en el inventario del gateway**. La base de datos real en el
> motor **queda exactamente como estaba**.

Consecuencias que la UI debe respetar:

1. **No ofrecer este `PATCH` como "cambiar el collation de una base existente".** Sería
   engañoso: el usuario creería que migró la base y en realidad solo desincronizó el
   inventario respecto de la realidad. Es la peor clase de bug de UI, porque no falla nada —
   simplemente el gateway pasa a mentir sobre el estado del motor.
2. **Si el formulario de edición expone estos campos, tiene que decir qué son.** Copy sugerido:
   *"Metadata del inventario. Editar esto no modifica la base de datos en el servidor."*
3. **Recomendación fuerte: no exponerlos en absoluto** en el formulario de edición, salvo que
   exista un caso de uso concreto de "corregir un registro mal cargado". No hay ninguna
   operación en el gateway que cambie el charset de una base ya creada; la vía real es crear
   una base nueva y migrar los datos (módulo de clonado).
4. **Este `PATCH` tampoco valida contra el catálogo** ([§3.3](#33-dónde-sí-y-dónde-no-se-aplica-el-enforcement)),
   así que puede dejar guardado un valor que la creación habría rechazado.

### Limitación conocida del backend: el patrón de `collation` es más estricto acá

`ManagedDatabaseUpdate` (body del `PATCH`) y `AdoptDatabaseIn` (body de
`POST /managed-databases/adopt`) validan `collation` con el patrón **viejo**
`^[A-Za-z0-9_]{1,64}$`, que **no admite puntos, guiones ni `@`**. `ManagedDatabaseCreate` sí
usa el patrón corregido.

Efecto concreto:

| Endpoint | `collation: "en_US.UTF-8"` |
|---|---|
| `POST /managed-databases` | ✅ Aceptado |
| `POST /servers/{id}/databases` | ✅ Aceptado (sin patrón; valida el catálogo) |
| `PATCH /managed-databases/{id}` | ❌ **`422` de validación Pydantic** |
| `POST /managed-databases/adopt` | ❌ **`422` de validación Pydantic** |

Es una **limitación conocida y no corregida del backend**, no un bug del frontend.

**Recomendación:** no ofrecer edición de `collation` vía `PATCH` para servidores PostgreSQL
hasta que el backend lo corrija. Si el formulario de adopción tiene campo de collation, dejarlo
vacío en PostgreSQL o aceptar que solo entren valores sin puntuación. **Reportar esta
limitación al equipo de backend** en vez de trabajarla en la UI (cualquier workaround del lado
del cliente sería peor que el problema).

> **Nota adicional sobre `adopt`:** ahí `charset`/`collation` son **puramente informativos**. No
> se aplica DDL, no se valida contra el catálogo y **tampoco se leen del motor**: se guarda
> literalmente lo que el cliente envíe. Si la UI los expone en el formulario de adopción, el
> copy debe dejar claro que es una anotación manual, no un dato verificado.

---

## 8. Flujos completos, paso a paso

### 8.1 Crear una BD sin elegir nada

El caso más común y el que **nunca** puede fallar por catálogo.

```
1. La UI ya tiene el servidor elegido (engine = "mariadb").
2. Mapea engine → engine_family = "mysql".
3. GET /charset-collation-options?engine_family=mysql&only_enabled=true
   → 2 filas; la de id=1 tiene is_default: true.
4. El selector se puebla y preselecciona la fila id=1 …
   … pero el usuario elige la opción "Usar el valor por defecto del motor".
5. POST /servers/3/databases
   { "name": "pruebas", "register": false }
   ← sin charset ni collation
6. → 201. El motor aplicó utf8mb4 con su collation por defecto.
```

**Punto de diseño:** el selector debería tener una opción explícita *"Usar el valor por defecto
del motor"* además de las combinaciones del catálogo. Es un caso legítimo y distinto de
"elegir la sugerida": no envía nada y por lo tanto no depende del catálogo.

### 8.2 Crear una BD eligiendo del selector

```
1..3. Igual que 8.1 → el selector trae utf8mb4/utf8mb4_unicode_ci (default) y
      utf8mb4/utf8mb4_general_ci.
4. El usuario deja la preseleccionada (la marcada como sugerida).
5. POST /servers/3/databases
   { "name": "tienda_2026", "charset": "utf8mb4", "collation": "utf8mb4_unicode_ci",
     "register": true, "owner_id": 12 }
6. → 201 { database, engine, registered: true, managed_database_id: 87 }
7. La UI navega al detalle de la BD (GET /managed-databases/87) para mostrar lo que
   quedó REGISTRADO — no lo que se tipeó (ver §3.2).
```

### 8.3 Combinación no habilitada: el `422` y cómo la UI se recupera

El escenario realista no es que el usuario tipee algo raro: es que el **selector se pobló hace
rato** y mientras tanto otro admin deshabilitó esa combinación. También ocurre si la UI todavía
usa el input de texto libre.

```
1. El usuario envía charset: "latin1", collation: "latin1_swedish_ci".
2. POST /servers/3/databases → 422
   detail.public_context.allowed = [ {utf8mb4/utf8mb4_unicode_ci, is_default:true},
                                     {utf8mb4/utf8mb4_general_ci, is_default:false} ]
3. La UI NO muestra un error genérico. Hace tres cosas:
   a) Marca el campo culpable usando public_context.requested.
   b) REPUEBLA el selector con public_context.allowed — sin una segunda llamada.
   c) Preselecciona la entrada con is_default: true.
4. Mensaje: "La combinación 'latin1 / latin1_swedish_ci' ya no está habilitada.
             Elegí una de las opciones disponibles."
   + enlace secundario: "Administrar el catálogo →"  (solo si el admin puede llegar ahí)
5. El usuario elige, reenvía, → 201.
```

**Regla:** ante este `422` **el formulario no se cierra ni se limpia**. Todo lo demás que el
usuario cargó (nombre, propietario, notas) se conserva. Solo el par charset/collation se
invalida y se re-ofrece.

Si `public_context.truncated` es `true`, agregar *"Se muestran las primeras 50 opciones;
consultá el catálogo completo para ver el resto."*

### 8.4 Habilitar una combinación deshabilitada para poder usarla

El puente entre las dos pantallas.

```
1. El operador necesita crear una BD con utf8mb4_0900_ai_ci y no la encuentra en el selector.
2. Va a la pantalla de administración del catálogo.
3. GET /charset-collation-options   ← SIN only_enabled: hay que ver lo deshabilitado
4. Encuentra la fila id=3, atenuada, con la advertencia "Solo MySQL 8+".
5. Activa el interruptor:
   PATCH /charset-collation-options/3   { "enabled": true }
   → 200, la fila vuelve con enabled: true, is_default: false.
6. La UI refresca el listado.
7. Vuelve al formulario de creación → el selector ya la ofrece.
```

**Aviso obligatorio en el paso 5** cuando se habilita una combinación que el seed marcó como
específica de un motor: *"Esta collation solo existe en MySQL 8 o superior. Crear una base con
ella en un servidor MariaDB fallará en el motor."* El catálogo es global y no distingue
versiones — la advertencia es lo único que evita ese error.

### 8.5 Deshabilitar la que es default: el `422` de invariante

```
1. El operador quiere dejar de ofrecer utf8mb4_unicode_ci (id=1), que es el default.
2. PATCH /charset-collation-options/1   { "enabled": false }
   → 422 "Una combinación marcada como default debe estar habilitada; habilítala o
           designa otro default primero."
      public_context: { enabled: false, is_default: true }
3. La UI reconoce el caso (la fila tenía is_default: true) y ofrece DOS salidas concretas,
   no un mensaje de error:

   a) "Marcar otra combinación como sugerida primero"
      → abre el selector de default sobre las filas habilitadas de la familia
      → PATCH /charset-collation-options/2  { "is_default": true }   (desmarca la 1 sola)
      → reintenta: PATCH /charset-collation-options/1 { "enabled": false }   → 200

   b) "Deshabilitarla y dejar la familia sin sugerencia"
      → PATCH /charset-collation-options/1  { "enabled": false, "is_default": false }
      → 200 en UNA llamada
```

**La opción (b) es la que suele faltar en este tipo de UI.** El `PATCH` acepta los dos campos
juntos, así que el operador no queda atrapado: si de verdad quiere sacarla y no tiene otra
candidata, puede. La consecuencia (la familia se queda sin sugerencia) debe explicitarse en el
diálogo.

**Anticipación en el cliente:** la UI ya sabe si una fila es `is_default`. Puede **prevenir** el
`422` mostrando el diálogo de las dos opciones **antes** de enviar, en vez de esperar el error.
El `422` sigue siendo la red de seguridad (otro admin pudo cambiar el estado mientras tanto).

### 8.6 Agregar una combinación custom

```
1. El operador necesita utf8mb4_0900_as_cs (case-sensitive), que no está sembrada.
2. Formulario de alta: familia = "MySQL / MariaDB", charset = "utf8mb4",
   collation = "utf8mb4_0900_as_cs", interruptor "Habilitar de inmediato" = ON.
3. POST /charset-collation-options
   { "engine_family": "mysql", "charset": "utf8mb4",
     "collation": "utf8mb4_0900_as_cs", "enabled": true }
   → 201, id=9.
4. La UI refresca el listado y resalta la fila nueva.

--- Camino alternativo: ya existía ---
3'. → 409  public_context: { id: 5, charset: "latin1",
                             collation: "latin1_swedish_ci", enabled: false }
4'. La UI NO muestra "error". Muestra:
    "Esa combinación ya está en el catálogo, pero está deshabilitada."
    [Ir a la fila]   [Habilitarla ahora]  ← dispara PATCH /charset-collation-options/5
                                             { "enabled": true }
```

**Notas del formulario de alta:**

- El interruptor "Habilitar de inmediato" debe existir y **su valor por defecto debería ser
  encendido** si el flujo típico es "la necesito ahora". El default del *backend* es `false`,
  así que si el interruptor está apagado hay que aclarar que la combinación quedará
  deshabilitada y no aparecerá en el selector.
- **No hay campo "marcar como sugerida"** en el alta: el backend no lo acepta. Si se quiere,
  es un `PATCH` posterior — y conviene decirlo, no dejar el campo ausente sin explicación.
- El campo `collation` debe poder quedar **vacío a propósito** (= `null`), con una etiqueta
  clara como *"Sin collation específica (la elige el motor)"*.
- Etiquetas por familia: en `postgresql`, `charset` es **"Encoding"** y `collation` es
  **"Locale"** — mismo criterio que ya usa el formulario de creación de BD.

---

## 9. Interpretación visual: pantallas, estados y transiciones

Conceptual — sin tecnología ni implementación. Un componente que se inserta en una pantalla
existente, y una pantalla nueva.

### 9.1 El selector, dentro del formulario de creación de BD

Reemplaza los dos inputs de texto libre. **No es un cambio de etiquetas: es un cambio de tipo
de control.**

```
[Formulario de creación — sección "Configuración del motor"]

  Campo: Juego de caracteres y ordenamiento     (un solo selector, no dos campos)
    ┌────────────────────────────────────────────────────────────┐
    │ utf8mb4 · utf8mb4_unicode_ci          ⭐ sugerida        ▾ │
    └────────────────────────────────────────────────────────────┘
    Opciones:
      ⭐ utf8mb4 · utf8mb4_unicode_ci
         utf8mb4 · utf8mb4_general_ci
      ─────────────────────────────────
         Usar el valor por defecto del motor        ← no envía nada
    Ayuda: "Solo se listan las combinaciones habilitadas por el administrador
            del gateway."
    Enlace secundario (solo si el usuario administra el catálogo):
            "¿Falta una opción? Administrar el catálogo →"
```

**Por qué UN selector y no dos.** La unidad del catálogo es la **combinación**, no el charset y
la collation por separado. Dos selectores encadenados invitan a construir pares que el catálogo
no tiene y producen `422` evitables. Si el diseño exige dos campos separados, el segundo debe
filtrarse por el primero y solo ofrecer collations que formen par habilitado con él.

**Etiquetas por motor** (se mantiene lo que ya definía el plan anterior):

| | MySQL / MariaDB | PostgreSQL |
|---|---|---|
| Etiqueta del campo | "Juego de caracteres y ordenamiento" | "Codificación y locale" |
| `charset` se llama | Character set | **Encoding** |
| `collation` se llama | Collation | **Locale** |
| Advertencia extra | — | ⚠️ **se mantiene**: *"El locale debe existir en el sistema operativo del servidor PostgreSQL"* ([§3.4](#34-postgresql-el-catálogo-no-garantiza-que-el-locale-exista)) |

**Estados del selector:**

| Estado | Qué se muestra |
|---|---|
| `sin servidor elegido` | Selector **deshabilitado** con la ayuda *"Elegí primero un servidor."* No se puede pedir el catálogo sin conocer el motor. |
| `cargando` | Selector deshabilitado con indicador de carga. **El resto del formulario sigue usable** — el nombre y el propietario no dependen de esta llamada. |
| `cargado, con default` | La fila `is_default` preseleccionada, marcada con ⭐ y el texto "sugerida". |
| `cargado, sin default` | Ninguna preselección; se muestra el texto vacío *"Elegí una combinación (opcional)"*. **Es un estado válido**, no un error ([§5.3](#53-patch-charset-collation-optionsoption_id-)). |
| `vacío` | Ninguna combinación habilitada para esa familia. Mensaje: *"No hay combinaciones habilitadas para este motor. La base se creará con el valor por defecto del servidor."* El formulario **sigue siendo enviable** (sin charset). Enlace a administrar el catálogo. |
| `error al cargar` | El selector cae a la opción única *"Usar el valor por defecto del motor"* + aviso discreto con reintento. **No bloquea la creación**: el caso "no elegí nada" nunca falla por catálogo. |
| `rechazado (422)` | Selector resaltado, repoblado con `public_context.allowed`, mensaje de [§8.3](#83-combinación-no-habilitada-el-422-y-cómo-la-ui-se-recupera). El resto del formulario **intacto**. |

**Cambio de servidor = recarga del selector.** Si el usuario cambia de servidor y la familia
cambia (MariaDB → PostgreSQL), la selección previa se **descarta** y el catálogo se vuelve a
pedir. Conservarla produce un `422` garantizado.

### 9.2 Pantalla nueva: administración del catálogo

```
[Barra superior]
  Catálogo de charsets y collations           [+ Agregar combinación]
  Ayuda: "Define qué combinaciones se pueden elegir al crear una base de datos.
          No afecta bases ya creadas."

[Filtros]
  Familia: (Todas ▾ | MySQL / MariaDB | PostgreSQL)    ☐ Ver solo habilitadas

[Listado — agrupado por familia y por charset, en el orden que devuelve la API]

  ── MySQL / MariaDB ──────────────────────────────────────────────────────
   Habilitada   Combinación                        Sugerida    Acciones
   ─────────────────────────────────────────────────────────────────────────
   [ ●—— ]      utf8mb4 · utf8mb4_unicode_ci        ⭐          —
   [ ●—— ]      utf8mb4 · utf8mb4_general_ci                    [Marcar sugerida]
   [ ——○ ]      utf8mb4 · utf8mb4_0900_ai_ci                    —
                ⚠️ Solo MySQL 8+; falla en MariaDB
   [ ——○ ]      utf8mb3 · utf8mb3_general_ci                    —
                ⓘ Legado: no cubre Unicode completo
   [ ——○ ]      latin1 · latin1_swedish_ci                      —

  ── PostgreSQL ───────────────────────────────────────────────────────────
   [ ●—— ]      UTF8 · en_US.UTF-8                  ⭐          —
   [ ——○ ]      UTF8 · C                                        —
   [ ——○ ]      UTF8 · C.UTF-8                                  —
                ⚠️ El locale debe existir en el SO de cada servidor

[Aviso al pie]
  "El catálogo es global: se aplica a todos los servidores del inventario. No hay
   forma de borrar una combinación — deshabilitarla ya la saca del menú de creación."
```

**Reglas del listado:**

- **Las filas deshabilitadas se muestran siempre**, atenuadas, no ocultas. Administrar es
  poder ver lo que está apagado. El filtro "Ver solo habilitadas" arranca **desactivado**.
- **`is_default` se muestra como estrella, no como un segundo interruptor.** Es exclusiva por
  familia: la acción es *"marcar esta"*, no *"activar/desactivar"*. La fila que ya es la
  sugerida no ofrece la acción.
- **No hay botón de eliminar.** Ver [§5.4](#54-no-hay-delete-y-no-es-un-olvido).
- Si una familia queda **sin ninguna fila sugerida**, mostrar un aviso no bloqueante en su
  encabezado: *"Esta familia no tiene combinación sugerida: el selector de creación no
  preseleccionará ninguna."*
- Si una familia queda **sin ninguna fila habilitada**, el aviso es más fuerte: *"No hay
  combinaciones habilitadas: las bases de este motor se crearán con el valor por defecto del
  servidor."*

**Estados de la pantalla:**

| Estado | Qué se muestra |
|---|---|
| `cargando` | Esqueleto del listado. |
| `cargado` | Listado agrupado. |
| `guardando fila` | Solo **esa fila** en estado de carga; el resto del listado sigue interactivo. |
| `conflicto de invariante (422)` | Diálogo con las dos salidas de [§8.5](#85-deshabilitar-la-que-es-default-el-422-de-invariante). El interruptor **vuelve a su posición original** hasta que se resuelva. |
| `rate limit (429)` | *"Demasiados cambios seguidos. Esperá un momento."* El límite es `20/minute` para `POST` y `PATCH` — alcanza de sobra para uso manual, pero no para un guardado optimista por cada clic. |
| `error de sistema` | Rojo, con el `request_id` visible, y reintento. |

### 9.3 Diálogo de alta de combinación

```
[Modal]
  Título: Agregar combinación al catálogo

  Familia de motor:  ( ) MySQL / MariaDB    ( ) PostgreSQL
    Ayuda: "MySQL y MariaDB comparten catálogo."

  [Etiquetas según la familia elegida]
  Character set / Encoding:  [ ______________ ]   requerido
  Collation / Locale:        [ ______________ ]   opcional
    ☐ Sin collation específica (la elige el motor)

  ☑ Habilitar de inmediato
    Ayuda (cuando está desmarcado): "Se agregará deshabilitada y no aparecerá en el
    selector de creación hasta que la habilites."

  ⓘ "Para que sea la sugerida por defecto, marcala después desde el listado."

  [Cancelar]   [Agregar]
```

### 9.4 Transiciones

```
Formulario de creación de BD
  → [elegir servidor] → GET catálogo (engine_family mapeada) → selector poblado
  → [cambiar de servidor a otra familia] → descartar selección → GET catálogo de nuevo
  → [enviar]
       → 201  → detalle de la BD / listado (con notificación de éxito)
       → 422 catálogo → mismo formulario, selector repoblado con `allowed`, resto intacto
       → 422 validación → mismo formulario, campo marcado
       → 5xx / error de motor → mismo formulario + request_id

  → [clic "Administrar el catálogo →"] → Catálogo (pantalla nueva)

Catálogo
  → [interruptor de una fila] → PATCH → recarga de la fila
       → 422 invariante de default → diálogo de dos opciones → PATCH corregido → recarga
  → [marcar sugerida] → PATCH {is_default:true} → **recarga del listado COMPLETO**
                        (el default anterior cambió en silencio)
  → [+ Agregar combinación] → Diálogo de alta
       → 201 → cierra + recarga + resalta la fila nueva
       → 409 → el diálogo se convierte en "ya existe, ¿la habilitamos?" con el id recibido
  → [volver] → Formulario de creación (que debe RE-PEDIR el catálogo al volver)
```

---

## 10. Tipos (referencia rápida)

```ts
type EngineFamily = "mysql" | "postgresql";      // MariaDB va en "mysql"
type ServerEngine = "mysql" | "mariadb" | "postgresql";

// El mapeo que el frontend debe hacer antes de consultar el catálogo:
//   "mariadb" -> "mysql";  el resto, igual.

interface CharsetCollationOptionOut {
  id: number;
  engine_family: EngineFamily;
  charset: string;
  collation: string | null;        // null = sin collation específica (NO es un dato faltante)
  enabled: boolean;                // aparece en el selector de creación
  is_default: boolean;             // sugerencia de la familia; a lo sumo una
  created_at: string;              // ISO 8601
  updated_at: string;
}

interface CharsetCollationOptionCreate {
  engine_family: EngineFamily;     // requerido, máx. 16
  charset: string;                 // requerido, 1..64
  collation?: string | null;       // máx. 128, default null
  enabled?: boolean;               // default FALSE — nace deshabilitada
  // NO existe `is_default` en el alta: es un PATCH posterior.
}

interface CharsetCollationOptionUpdate {
  enabled?: boolean | null;
  is_default?: boolean | null;
  // Se pueden enviar los dos juntos. Enviar ninguno -> 422.
}

// --- Creación de BD (los campos relevantes; contrato completo en §6) ---

interface DatabaseCreateIn {          // POST /servers/{id}/databases
  name: string;                       // 1..64
  charset?: string | null;            // validado contra el catálogo
  collation?: string | null;          // validado contra el catálogo
  owner?: string | null;              // solo PostgreSQL
  register?: boolean;                 // default false
  owner_id?: number | null;           // obligatorio si register=true
  notes?: string | null;
}

interface DatabaseCreateOut {
  database: string;
  engine: ServerEngine;
  registered: boolean;
  managed_database_id: number | null;
  // OJO: no hace eco del charset/collation aplicado.
}

interface ManagedDatabaseCreate {     // POST /managed-databases?provision=true|false
  name: string;
  server_id: number;
  owner_id: number;
  model_id?: number | null;
  model_version?: string | null;
  charset?: string | null;            // validado contra el catálogo
  collation?: string | null;          // acepta locales de PG (puntos/guiones/@)
  notes?: string | null;
}

interface ManagedDatabaseOut {
  id: number; name: string; server_id: number; owner_id: number;
  model_id: number | null; model_version: string | null;
  charset: string | null;             // forma CANÓNICA del catálogo, no el texto enviado
  collation: string | null;           // null = no se eligió (el motor la definió)
  status: "pending" | "active" | "error";
  notes: string | null;
  origin: "provisioned" | "adopted";
  created_at: string; updated_at: string;
}

// --- Envelope de error del gateway ---

interface ApiErrorBody {
  detail: {
    msg: string;
    type: string;                     // "AppHttpException" | "RequestValidationError" | …
    public_context?: unknown;         // presente en TODOS los ambientes
    context?: unknown;                // SOLO en desarrollo — no dependas de esto
    loc?: unknown;                    // SOLO en desarrollo
  };
}

// public_context del 422 de combinación no habilitada:
interface CharsetRejectedContext {
  engine_family: EngineFamily;
  requested: { charset: string | null; collation: string | null };
  allowed: { charset: string; collation: string | null; is_default: boolean }[];
  truncated: boolean;                 // true = cortada en 50 filas
}

// public_context del 409 de alta duplicada:
interface CharsetDuplicateContext {
  id: number;                         // la fila EXISTENTE — usala para el PATCH de habilitar
  engine_family: EngineFamily;
  charset: string;
  collation: string | null;
  enabled: boolean;
}
```

---

## 11. Matriz de errores

```
--- Catálogo ---
422 — GET/POST: engine_family no es "mysql" ni "postgresql"
      public_context: { allowed: ["mysql", "postgresql"] }
422 — POST: nombre de charset inválido
      public_context: { pattern: "^[A-Za-z][A-Za-z0-9_]{0,63}$" }
422 — POST: nombre de collation/locale inválido para esa familia
      public_context: { pattern: <el patrón de la familia> }
409 — POST: la combinación ya existe
      public_context: { id, engine_family, charset, collation, enabled }
      → NO es un callejón sin salida: ofrecer habilitar la fila `id`
404 — PATCH: option_id inexistente
422 — PATCH: body sin `enabled` ni `is_default` ("Nada para actualizar")
422 — PATCH: el estado RESULTANTE sería default + deshabilitada
      public_context: { enabled, is_default }
429 — rate limit: 20/minute en POST y PATCH (el GET no tiene límite propio)

--- Creación de BD ---
422 — combinación charset/collation no habilitada  ← el error central de este módulo
      public_context: { engine_family, requested, allowed[], truncated }
422 — validación Pydantic del body (nombre de BD, patrones, campos faltantes)
      type: "RequestValidationError"; el detalle por campo SOLO llega en desarrollo
422 — register=true sin owner_id
409 — ya existe una BD con ese nombre en el servidor
429 — rate limit: 10/minute en POST /servers/{id}/databases
5xx — error del MOTOR tras pasar el catálogo (p. ej. PostgreSQL "invalid locale name",
      o una collation habitada que ese motor no soporta). El catálogo NO lo previene.

--- Metadata (§7) ---
422 — PATCH /managed-databases/{id} con una collation con puntos/guiones/@
      (limitación conocida del backend, no del frontend)
```

---

## 12. Supuestos sobre el frontend actual (a confirmar)

No tenemos acceso al repositorio de frontend. Lo siguiente **se asume** a partir de
[`docs/frontend/plan-server-database-lifecycle.md`](frontend/plan-server-database-lifecycle.md),
que es la especificación que ese equipo recibió. **Confirmar antes de estimar el trabajo.**

- **`[SUPUESTO F1]` — El formulario de creación de BD implementa `charset`/`collation` como
  entrada de texto libre con sugerencias estáticas.** Es lo que dicta el `[SUPUESTO S8]` de ese
  plan. *Si además se implementaron sugerencias distintas por motor*, esa lista estática hay
  que **eliminarla**, no ampliarla: ahora la fuente de verdad es el catálogo.

- **`[SUPUESTO F2]` — El formulario ya conoce el `engine` del servidor elegido** (lo necesita
  para las etiquetas Encoding/Locale y para mostrar u ocultar el campo `owner`). Si es así, el
  mapeo a `engine_family` es trivial. Si el formulario recibe solo el `server_id`, hace falta
  una llamada adicional a `GET /servers/{id}`.

- **`[SUPUESTO F3]` — Existe un formulario de edición de BD gestionada que expone
  `charset`/`collation`.** Si existe, aplica todo [§7](#7-lo-que-el-patch-de-metadata-no-hace)
  y la recomendación es **quitar esos campos**. Si no existe, no hay nada que hacer más que no
  agregarlos.

- **`[SUPUESTO F4]` — Existe un formulario de adopción de BD que expone `charset`/`collation`.**
  Mismo caso, con el agravante del patrón estricto de `collation` (§7).

- **`[SUPUESTO F5]` — No hay hoy ninguna pantalla de "configuración del gateway" donde encaje
  naturalmente el catálogo.** La pantalla de §9.2 es nueva; dónde vive en la navegación es una
  decisión de producto. Sugerencia: junto al catálogo de privilegios y a los perfiles de
  permisos, que son los otros dos catálogos globales del gateway.

- **`[SUPUESTO F6]` — El frontend no cachea el catálogo entre pantallas.** Si lo cachea, hay
  que **invalidar ese caché al volver de la pantalla de administración**, o el selector va a
  seguir mostrando el estado viejo y produciendo `422` inexplicables.

### Estado del backend (no es algo que la UI deba comunicar al usuario)

El módulo está implementado y verificado con tests de lógica pura, CRUD del catálogo y
enforcement en los dos caminos de creación (con el adapter mockeado), más el ciclo de migración
contra SQLite. **Falta la corrida contra motores reales**: que una `utf8mb4_0900_ai_ci`
habilitada efectivamente falle en MariaDB, y que los locales sembrados existan en el SO del
PostgreSQL destino. Es relevante para el equipo de frontend en un solo sentido: **el contrato
podría tener ajustes menores**, así que conviene aislar el mapeo de estas respuestas en un solo
lugar del código de UI.

---

## 13. Checklist de implementación

**Selector en la creación de BD**

- [ ] **Mapear `engine` → `engine_family` antes de llamar al catálogo** (`mariadb` → `mysql`).
      Es el error nº 1 de este módulo y se manifiesta como un selector vacío o un `422` en
      MariaDB.
- [ ] **Pedir el catálogo con `only_enabled=true`** en el formulario de creación. Sin ese
      filtro se ofrecen combinaciones que el backend rechaza.
- [ ] **Un solo selector de combinación**, no dos campos independientes. Si el diseño exige
      dos, el segundo se filtra por el primero.
- [ ] **Incluir la opción explícita "Usar el valor por defecto del motor"**, que no envía
      `charset` ni `collation`. Es el caso que nunca falla.
- [ ] **Preseleccionar la fila `is_default` y enviarla explícitamente.** `is_default` no hace
      nada del lado del backend: si no se envía, no se aplica.
- [ ] **Tolerar que no haya default y que no haya ninguna habilitada.** Los dos son estados
      válidos del catálogo, no errores.
- [ ] **Ante el `422`, repoblar el selector con `public_context.allowed`** en vez de mostrar un
      error genérico, y **no limpiar el resto del formulario**.
- [ ] **Mostrar `truncated: true`** como "hay más opciones", nunca en silencio.
- [ ] **Recargar el catálogo al cambiar de servidor**, y descartar la selección si cambia la
      familia.
- [ ] **Eliminar las sugerencias estáticas de charset/collation** que existieran. Conservarlas
      junto al selector produce errores evitables.
- [ ] **Mantener la advertencia de locale de PostgreSQL.** El catálogo no garantiza que exista
      en el SO del servidor destino.
- [ ] **No normalizar el texto del locale de PostgreSQL** (mayúsculas incluidas): ahí el case
      importa.

**Pantalla de administración del catálogo**

- [ ] **Pedir el catálogo SIN filtros** (`GET /charset-collation-options` a secas). Con
      `only_enabled=true` la pantalla no puede hacer su trabajo.
- [ ] **Mostrar las filas deshabilitadas, atenuadas.** Son referencia deliberada, no ruido.
- [ ] **Recargar el listado COMPLETO tras un `PATCH` con `is_default: true`.** El default
      anterior se desmarca en silencio y el backend no lo informa.
- [ ] **Anticipar el `422` de "el default debe estar habilitado"** con el diálogo de dos
      salidas ([§8.5](#85-deshabilitar-la-que-es-default-el-422-de-invariante)), incluida la
      opción de deshabilitar y quitar el default en **un solo `PATCH`**.
- [ ] **Convertir el `409` del alta en una acción**, usando `public_context.id`: *"ya existe y
      está deshabilitada, ¿la habilitamos?"*.
- [ ] **No incluir botón de eliminar.** No existe `DELETE` y no debería parecer que falta.
- [ ] **Advertir al habilitar combinaciones específicas de un motor** (`utf8mb4_0900_ai_ci` solo
      MySQL 8+; locales de PostgreSQL que dependen del SO). El catálogo es global y no
      distingue versiones.
- [ ] **Respetar el rate limit de `20/minute`** en `POST`/`PATCH`: nada de guardado optimista
      por cada movimiento del interruptor.
- [ ] **Etiquetar la familia `mysql` como "MySQL / MariaDB"** en toda la pantalla.

**Metadata y límites**

- [ ] **No ofrecer `PATCH /managed-databases/{id}` como "cambiar el collation de una base".**
      No ejecuta DDL: solo desincroniza el inventario respecto del motor.
- [ ] **No ofrecer edición de `collation` vía ese `PATCH` en servidores PostgreSQL**, por el
      patrón estricto del backend, y **reportar la limitación** en vez de trabajarla en la UI.
- [ ] **Refrescar desde la respuesta después de crear**, no reutilizar lo que se tipeó: el
      backend guarda la forma canónica y deja en `null` el campo que no se envió.
- [ ] **Mostrar `collation: null` como "no se especificó (la definió el motor)"**, nunca como
      "sin collation".
- [ ] **Recordar que `POST /managed-databases` necesita `?provision=true`** para crear algo en
      el motor.
- [ ] **Mostrar el `request_id`** en los errores 5xx, para soporte.
- [ ] **Nunca leer `detail.context` ni `detail.loc`**: no existen fuera de desarrollo. Todo lo
      necesario está en `detail.public_context`.
- [ ] **Aislar el mapeo de estas respuestas en un solo lugar**: el contrato puede tener ajustes
      menores hasta la validación contra motores reales.
