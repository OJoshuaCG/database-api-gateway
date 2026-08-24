# API Reference v13 — La captura de SELECT pasa de tres llaves a dos

Addendum al contrato consolidado (`api-reference.md` §8/§9) y **supersede en parte a
[`api-reference-v9.md`](api-reference-v9.md)**: concretamente sus §2, §3.2, §3.3 y §3.7, que
describen un consentimiento por corrida que ya no existe. El resto de v9 sigue vigente.

Ataca un problema de **usabilidad que era también de seguridad**: crear bases nuevas a partir de
un blueprint chocaba con un 409 por versiones históricas con captura, incluso estando aprobadas.

---

## 1. Se elimina `allow_result_capture`

**Afecta a tres endpoints**, que dejan de aceptar el query param:

| Endpoint | Antes | Ahora |
|---|---|---|
| `POST /managed-databases/{id}/migrations/apply` | `?allow_result_capture=` obligatorio si alguna pendiente capturaba | No existe |
| `POST /managed-databases/{id}/migrations/rollback` | Ídem sobre el camino a revertir | No existe |
| `POST /database-models/{id}/migrations/apply-all` | Uno solo para todo el lote | No existe |

### Por qué

La feature tenía **tres llaves**: opt-in por versión (`capture_selects`), aprobación
(`reviewed=true`) y consentimiento por corrida. La tercera se retira porque no era una llave:

1. **Su premisa no aplica a este gateway.** Se justificaba con *«un blueprint se replica sobre N
   BDs de dueños potencialmente distintos, y quien aplica sobre UNA tiene que saber»*. Esos dueños
   son los usuarios **del motor** de cada base destino; el gateway es **single-admin**, sin roles
   ni permisos por usuario. La misma persona activa la captura, la aprueba y la aplica: no había
   un segundo par de ojos, solo un segundo momento.
2. **No dejaba rastro.** Pasar el flag no se registraba en `audit_log`. Lo único auditado es la
   escritura efectiva, que ocurre con o sin el gate. Era fricción sin evidencia forense.
3. **`apply-all` ya lo contradecía.** Un único query param autorizaba N bases de entornos
   distintos — lo contrario de «conciencia de ESTA base».
4. **Saltaba donde el riesgo era menor.** Una base nueva recibe la cadena completa de versiones,
   así que arrastra las históricas con captura; sobre una base vacía esos `SELECT` devuelven cero
   filas. Un gate que se dispara sobre todo en el caso inofensivo entrena el «siempre que sí», y
   ese reflejo después se aplica en producción.

### Compatibilidad: **no es breaking**

FastAPI ignora los query params que no declara. Un cliente sin actualizar que siga mandando
`?allow_result_capture=true` **recibe 200**: la única diferencia observable es que una llamada que
antes fallaba con 409 ahora funciona. Ningún cliente puede romperse por eso.

**Orden de despliegue: backend primero.** Al revés no rompe nada tampoco, pero si la SPA sale
antes deja de mandar el flag contra un backend que todavía lo exige, y ya no tiene la rama de UI
que sabía explicar ese 409.

---

## 2. El 409 que queda, ahora con código estable

El gate de **aprobación** se conserva intacto y es el único. Ahora emite
`public_context.code`.

| HTTP | `public_context.code` | Cuándo | Qué ofrecer |
|---|---|---|---|
| 409 | `migration.capture_unreviewed` | `apply` / `apply-all` / `rollback`: alguna versión del camino tiene `capture_selects: true` y `reviewed: false`. **No se ejecuta ninguna sentencia.** | Enlace al blueprint para revisar la consulta y aprobarla (`PATCH .../migrations/{version}` con `reviewed: true`). **No hay `force`**: la salida es aprobar. |
| 409 | `migration.capture_unreviewed_stamp` | `stamp` sobre una versión con captura sin revisar. | Aprobar, **o** repetir con `force=true` — acá sí es un escape legítimo (una versión aplicada hace meses a la que después se le activó la captura queda `reviewed=false`, y una BD que perdió su puntero necesita re-stampearse). |

**Dos códigos y no uno**, a propósito: el remedio difiere, y el código existe para elegir el CTA.
Con un código único la UI ofrecería «Forzar» donde no sirve.

`public_context.unreviewed_capture` (la lista de versiones) **se conserva** — el código se suma,
no reemplaza.

### En `apply-all` el rechazo viaja **dentro de un 200**

El guard corre **por BD** dentro del bucle, así que el 409 no aborta el lote: se convierte en un
ítem de una respuesta 200. Para ese ítem, el `public_context` de la respuesta HTTP **no existe**.
Por eso el código se copia a `error_code`, y las versiones a `unreviewed_capture` del propio ítem:

```jsonc
{
  "data": {
    "results": [
      { "managed_database_id": 5, "ok": true, "applied": [ /* … */ ] },
      {
        "managed_database_id": 7,
        "ok": false,
        "error": "El blueprint tiene versiones con captura de resultados SIN revisar (0010)…",
        "error_code": "migration.capture_unreviewed",   // ← antes salía null
        "unreviewed_capture": ["0010"]
      }
    ]
  }
}
```

El rechazo se audita como `migration.capture_review_denied`, igual que
`migration.environment_denied` para el guard de entorno.

---

## 3. Campos nuevos de respuesta

Todos **aditivos**, con default seguro.

### `will_capture_versions` — solo en dry-run

En `MigrationApplyOut`. Es el reemplazo del gate: la información se da en la llamada que existe
para **decidir**, no trabando la que existe para **ejecutar**. Mismo molde que `blocked_by`
(informativo, el plan no falla).

```jsonc
// POST /managed-databases/5/migrations/apply?dry_run=true
{ "data": { "dry_run": true, "pending_versions": ["0018", "0022"],
            "will_capture_versions": ["0018", "0022"] } }
```

### `captured_versions` — en la corrida real

En `MigrationApplyOut`, `MigrationRollbackOut` y en cada ítem de `apply-all`. Son las versiones en
las que **esa corrida** escribió capturas.

**Arregla un bug concreto**: para enlazar a `GET .../migrations/{version}/select-results` el
cliente adivinaba con `to_version` (la última aplicada). En un `apply` 0005→0010 cuya captura
ocurrió en 0007, ese enlace llevaba a `…/0010/select-results`, que está **vacío**.

```jsonc
{ "data": { "from_version": "0005", "to_version": "0010",
            "captured_select_count": 3, "captured_versions": ["0007"],
            "select_results_available": true } }
```

`captured_select_count` y `select_results_available` no cambian.

---

## 4. Auditoría: la eliminación AUMENTA la evidencia

Era condición para retirar el gate, no un extra.

- El `audit_log` de **intento** (`migration.apply` / `migration.rollback` con
  `status="attempt"`, escrito **antes** de tocar el motor) ahora nombra las versiones con captura:
  `apply hasta 0022 (captura: 0018, 0022)`. Queda incluso si la migración muere a mitad, o si
  termina capturando cero filas — un caso que antes era indistinguible de «nunca se intentó».
- El de **escritura** (`migration.select_results.write`) pasa de contar versiones a nombrarlas.
- El rechazo del guard en `apply-all` se registra (`migration.capture_review_denied`).

Sigue sin viajar ni una fila en la auditoría: conteos y versiones, nunca valores.

---

## 5. Checklist para la SPA

- [ ] Quitar `allow_result_capture` de las tres llamadas y sus interruptores de UI.
- [ ] Reemplazar el interruptor por un **aviso** (no un control) con las versiones que van a
      capturar. Acotarlo a las **pendientes** de esa base: un aviso que sale siempre no se lee.
- [ ] Segundo aviso, distinto, para las que están **sin aprobar**: esas no van a capturar, van a
      ser rechazadas.
- [ ] Clasificar el 409 por `public_context.code`, no por prosa. En `apply-all`, por
      `item.error_code` dentro del 200.
- [ ] Enlazar a lo capturado con `captured_versions`, no con `to_version`.
- [ ] Un solo predicado compartido para «¿esta versión captura?»: tenerlo duplicado por pantalla
      es cómo divergió antes (una usaba `reviewed === true`, otra `reviewed !== false`, y con la
      primera el aviso no aparecía nunca si el backend omitía el campo).
