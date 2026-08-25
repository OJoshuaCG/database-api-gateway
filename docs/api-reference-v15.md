# API Reference v15 — Editar una versión de blueprint que ya está aplicada

Addendum al contrato consolidado (`api-reference.md`) y continuación directa de
[`v14`](api-reference-v14.md), que definió cuándo una versión está congelada. Acá se agrega
la **única vía para atravesar ese freeze**: un endpoint nuevo, dos campos nuevos en el `PATCH`,
un código de error nuevo y una bandera nueva en el listado.

> **⚠️ Corrige a v14.** Ese documento decía «**No hay `force`**» y «No ofrecer «Forzar»» para
> los dos 409. Sigue siendo cierto para `model_migration.still_applied` (el `DELETE`), pero
> **ya no** para `model_migration.sql_frozen` (el `PATCH`): ahora tiene una vía de excepción
> con doble factor. Los checklists de v14 que dicen lo contrario quedan corregidos por éste.

---

## 1. Por qué existe la vía

El freeze supone que la salida siempre es fix-forward, y para un cambio de comportamiento eso
es cierto. Pero hay correcciones cuyo valor está en que las **BDs nuevas** no repitan el
defecto. Caso testigo: un `COLLATE` hardcodeado en el DDL de las primeras versiones. Describir
la corrección con una versión al final de la cadena obliga a toda base nueva a **crearse mal y
convertirse después**.

### Lo que se paga, y hay que decirlo en la UI

Editar `up_sql` **no re-ejecuta nada**. Las BDs que ya aplicaron la versión conservan
**físicamente** lo que corrió, así que su puntero de versión pasa a nombrar un SQL que no es el
que se les aplicó. La divergencia es **real e irreversible**.

Corolario que la pantalla tiene que dejar claro: **las BDs viejas siguen necesitando la
corrección por otra vía.** Para collation es el módulo de conversión de charset/collation, que
además recrea rutinas, triggers y vistas —congelan la collation de la sesión que las creó— y
evita el `Illegal mix of collations`.

---

## 2. Endpoint nuevo — `POST /database-models/{id}/migrations/{version}/edit-preview`

Rate limit **20/min**. Auditado. Body: los **mismos campos de SQL** que va a llevar el `PATCH`
(`up_sql`, `down_sql`, `up_sql_mysql`, `up_sql_postgresql`; los no enviados conservan su valor).

```jsonc
// 200
{
  "data": {
    "model_id": 3,
    "version": "0001",
    "requires_confirmation": true,
    "blocking_databases": [
      { "managed_database_id": 7, "reason": "still_applied", "current_version": "0005" }
    ],
    "resulting_checksum": "d6ee954f…",
    "confirm_version": "0001",
    "confirm_token": "1787620000.9f3c…",
    "expires_at": "2026-08-24T23:59:00Z"
  }
}
```

- `requires_confirmation: false` ⇒ ninguna BD tiene la versión vigente, `confirm_token` viene
  **`null`** y el `PATCH` común ya la edita. **No mandes los campos de confirmación en ese
  caso.**
- `blocking_databases[]` se lee **del motor**, no de la caché del inventario: es exactamente
  el conjunto de bases que van a quedar divergentes, y es la información por la que existe
  esta llamada. Mismos cuatro `reason` de v14 (incluido `unreadable`, que **cuenta como
  bloqueante**: un motor caído no prueba que la base ya no tenga la versión).

---

## 3. Campos nuevos en `PATCH /database-models/{id}/migrations/{version}`

| Campo | Tipo | Para qué |
|---|---|---|
| `confirm_version` | `string` | Debe ser **exactamente** la versión de la ruta. Obliga a identificar conscientemente qué se toca (molde de `confirm_target_name`). |
| `confirm_token` | `string` | El emitido por `edit-preview`. TTL corto; atado al **SQL exacto** que se previsualizó. |

**Van los dos o no va ninguno.** Cubren cosas distintas: el primero, *qué* versión; el segundo,
frescura, anti-replay y *qué SQL*. Sin el `subject` del token, `(operación, model_id, version)`
sería igual para cualquier edición de esa versión, así que se podría previsualizar una
corrección inocua —viendo un `blocking_databases` tranquilizador— y mandar otra en el `PATCH`.

Si el SQL del `PATCH` no es el que se previsualizó, el token **no valida** (422). Reenviar el
preview es la salida.

---

## 4. Errores

| HTTP | `public_context.code` | Cuándo |
|---|---|---|
| 409 | `model_migration.sql_frozen` | Se cambió el SQL de una versión vigente **sin** los dos factores. Trae además **`override_available: true`**. |
| 422 | `model_migration.edit_confirm_mismatch` | `confirm_version` no coincide con la versión de la ruta. |
| 409 | `model_migration.partial_application` | Hay una aplicación **a medias** sin resolver. Trae `incomplete_progress[]` con la BD y cuántas sentencias alcanzó. |
| 409 | `model_migration.stale_overrides` | Se cambió `up_sql` dejando overrides por motor que quedarían obsoletos. Trae `stale_overrides[]` con los nombres de campo. |
| 422 | *(sin code)* | El token no corresponde a esta versión/SQL, o está malformado. |
| 410 | *(sin code)* | El token expiró. |

Los dos últimos **no llevan `code`** porque salen del servicio de tokens, compartido con otros
módulos del gateway. No hace falta: clasificá por **status**, y en los dos casos el CTA es el
mismo — **volver a pedir el preview** (en el 410 porque venció, en el 422 porque el SQL cambió
respecto del previsualizado). Nunca re-previsualices en silencio: el usuario tiene que volver a
ver a quién deja divergente.

### `model_migration.partial_application` NO se abre con el doble factor

Es un 409 distinto y hay que tratarlo distinto. Acá el problema no es la divergencia: es que un
`resume` posterior interpretaría los índices del checkpoint contra un SQL que no es el que
corrió. La salida es reintentar el `apply` sobre esa BD (retoma solo) o limpiarlo con
`stamp?force=true`. **No ofrezcas «Editar igual» en este 409** — no existe override.

### `model_migration.stale_overrides` debería ser imposible desde la UI

Si el formulario ya sabe qué overrides por motor tiene la versión, puede exigir que se
reenvíen corregidos o se limpien (`null`) en la misma llamada. Llegar a este 409 es un
formulario incompleto, no un caso de negocio.

---

## 4.bis ⚠️ `down_sql` NO está congelado

El freeze mira **solo** `up_sql` y los overrides por motor. **`down_sql` se puede editar
siempre**, incluso con `sql_frozen: true` y sin ningún factor de confirmación.

No es un descuido: confirmar el rollback **después** de haber aplicado es un flujo soportado —
el 409 de `rollback` pide exactamente eso. Si la UI deshabilita todo el formulario cuando
`sql_frozen` es `true`, **cierra la única salida de ese otro 409** y deja la versión sin forma
de revertirse nunca.

**Regla para la UI:** con `sql_frozen: true`, deshabilitá `up_sql` y los overrides; dejá
`down_sql` **habilitado**.

Dos efectos de editar `down_sql` que conviene saber: purga las capturas de `SELECT` de
dirección `down` (sus índices de sentencia dejarían de apuntar a lo mismo), y en una versión
con `capture_selects` **revoca la aprobación** (`reviewed` vuelve a `false`), porque lo que se
aprobó era una consulta concreta.

`override_available` es el campo que permite distinguir **"no se puede"** de **"se puede
confirmando"** sin interpretar la prosa del `message`. Cuando es `true`, el CTA correcto ya no
es solo «Crear versión correctiva»: se suma «Editar igual (asumiendo divergencia)».

---

## 5. Bandera nueva — `sql_diverged`

Presente en el listado (`ModelMigrationSummary`) y en el detalle (`ModelMigrationOut`).

`true` = el SQL de esta versión **se editó después de que alguna BD la aplicara**, así que ya
no describe el plano de todas sus bases.

- **No restringe nada.** El SQL nuevo es el que se aplica de acá en más; la bandera es señal,
  no bloqueo. No deshabilites controles por esto.
- Es un **hecho persistido**, derivado del log de auditoría (acción
  `migration.sql_edited_after_apply`), no un dato de la respuesta del `PATCH`: sobrevive a
  recargas y a que las BDs divergentes se den de baja.
- El detalle de **qué** bases divergieron, cuándo y con qué checksum previo está en la
  auditoría, no en este campo.

---

## 6. El `DELETE` no cambia

`model_migration.still_applied` **sigue sin override**. Borrar la descripción de cambios que
están físicamente en una BD deja esa base con objetos que **ninguna** versión del blueprint
describe, y eso no se arregla con un rastro. Todo lo que v14 dice del `DELETE` sigue vigente.

---

## 7. Checklist para la SPA

- [ ] En el 409 `sql_frozen`, leer **`override_available`**. Si es `true`, ofrecer la segunda
      salida además de «Crear versión correctiva».
- [ ] Esa segunda salida **no** es un switch: es un flujo de dos pasos (preview → confirmar)
      que muestra `blocking_databases[]` antes de pedir la confirmación. Un botón «Forzar» de
      un click convierte en trámite lo que es una decisión irreversible.
- [ ] En la pantalla de confirmación, decir explícitamente que **las BDs listadas conservan lo
      que ya se les aplicó** y que necesitan corrección por otra vía. Es el malentendido más
      probable de toda la feature.
- [ ] Reenviar en el `PATCH` **el mismo SQL** que se mandó al preview. Si el usuario lo
      retoca, volver a pedir preview (si no: 422).
- [ ] Manejar 410 (token vencido) reabriendo el preview, sin perder lo que el usuario escribió.
- [ ] Mostrar `sql_diverged` como insignia informativa en el listado y el detalle. **No**
      deshabilitar acciones por ella.
- [ ] Cuando `requires_confirmation` sea `false`, **no** mandar `confirm_version`/`confirm_token`.
- [ ] Con `sql_frozen: true`, deshabilitar `up_sql` y los overrides pero **dejar `down_sql`
      habilitado** (§4.bis). Deshabilitarlo entero cierra la salida del 409 de rollback.
- [ ] No ofrecer «Editar igual» en `model_migration.partial_application`: ese 409 no tiene
      override. Su CTA es reintentar el `apply` sobre la BD que `incomplete_progress` nombra.
- [ ] Hacer imposible `model_migration.stale_overrides` desde el formulario: si la versión
      tiene overrides, exigir reenviarlos o limpiarlos en la misma llamada.
