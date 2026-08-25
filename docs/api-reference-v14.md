# API Reference v14 — Una versión de blueprint se descongela al revertirla

Addendum al contrato consolidado (`api-reference.md`) para el módulo de migraciones de
blueprints. **No hay endpoints nuevos ni campos que desaparezcan**: cambia el criterio de dos
409 que ya existían, y esos 409 pasan a traer datos estructurados con los que la SPA puede
explicar el bloqueo y ofrecer la salida correcta.

Ataca un **callejón sin salida**: una versión revertida correctamente en todas las bases
quedaba imborrable e ineditable para siempre.

---

## 1. El criterio: "aplicada hoy", no "aplicada alguna vez"

| | Antes | Ahora |
|---|---|---|
| Qué congela una versión | Existe una fila `applied` en el historial de aplicación | Alguna BD gestionada está **en esa versión o en una posterior** |
| Una versión revertida en todas las BDs | Congelada **para siempre** | Editable y borrable |
| Una versión que solo falló | Editable y borrable | Igual (sin cambios) |

### Por qué

El historial (`database_migration_history`) es un **log de eventos**, no un estado: la misma
fila `status='applied'` la escribe el `apply` **y** el `rollback`, y no hay columna
`direction`. Esa fila no se revoca nunca.

Con ese criterio, revertir dejaba la versión congelada sin ninguna salida: no hay purga de
historial, el `DELETE` no acepta `force`, y el borrado en cascada de esas filas cuelga del
borrado de la migración — que es justo lo que el historial bloqueaba. El único camino era
acumular versiones fix-forward que describían cambios ya inexistentes en cualquier motor.

Las migraciones son **forward-only encadenadas**: una BD en `0007` tiene aplicadas todas las
`<= 0007`. Por eso el criterio es `>=` y no `==` — con igualdad, una base al día dejaría borrar
todas las versiones intermedias que sí describen su esquema.

---

## 2. Los dos 409, ahora con código estable

Los códigos viajan en `public_context.code`, que se envía **en todos los entornos** (a
diferencia de `context`, visible solo en `development`).

| HTTP | `public_context.code` | Cuándo | Qué ofrecer |
|---|---|---|---|
| 409 | `model_migration.sql_frozen` | `PATCH /database-models/{id}/migrations/{version}` que cambia el SQL efectivo (`up_sql` o un override por motor) de una versión vigente. | «Crear versión correctiva» (fix-forward). Como alternativa, revertir las BDs que la bloquean. **No hay `force`.** |
| 409 | `model_migration.still_applied` | `DELETE /database-models/{id}/migrations/{version}` de una versión vigente. | Igual: revertir primero, o crear una migración compensatoria. Borrarla no revierte nada en el motor. |

**Dos códigos y no uno**, a propósito: el CTA difiere (corregir el SQL vs. eliminar la versión)
y el código existe para elegirlo sin leer prosa.

### Forma del `public_context`

```jsonc
// 409 — DELETE /api/v1/database-models/3/migrations/0003
{
  "detail": {
    "msg": "No se puede eliminar la versión: BD 7 está en la versión 0005. Eliminarla no revierte nada en el motor: …",
    "type": "AppHttpException",
    "public_context": {
      "code": "model_migration.still_applied",
      "version": "0003",
      "blocking_databases": [
        { "managed_database_id": 7, "reason": "still_applied", "current_version": "0005" },
        { "managed_database_id": 9, "reason": "unreadable" }
      ]
    }
  }
}
```

| Campo | Tipo | Notas |
|---|---|---|
| `code` | `string` | Uno de los dos de la tabla anterior. |
| `version` | `string` | La versión que se intentó tocar. |
| `blocking_databases` | `array` | Nunca vacío en un 409 (si estuviera vacío, la operación habría sido un 200). |
| `blocking_databases[].managed_database_id` | `int` | Enlazable a `GET /managed-databases/{id}`. |
| `blocking_databases[].reason` | `string` | Vocabulario cerrado, tabla de abajo. |
| `blocking_databases[].current_version` | `string` \| ausente | Solo con `reason: "still_applied"`. |

### Los cuatro `reason`

| `reason` | Significa | Qué mostrar |
|---|---|---|
| `still_applied` | El motor reporta que esa BD está en la versión, o en una posterior. Caso normal. | «La BD *X* está en la versión *N*» + enlace al rollback de esa base. |
| `unreadable` | No se pudo leer la versión de esa BD: motor caído, base sin aprovisionar, credenciales rotas. **Fail-closed**: bloquea igual. | «No se pudo verificar la BD *X*» + enlace a `test-connection` / `provision`. Es un problema a resolver, no un permiso a forzar. |
| `unknown_database` | Hay historial contra una BD que ya no está en el inventario. No debería ocurrir. | Mensaje genérico; es un caso de inconsistencia interna. |
| `unknown_blueprint` | No se pudo resolver el blueprint de la migración. | Ídem. |

**Traten `unreadable` como bloqueo legítimo, no como error transitorio a reintentar en bucle.**
Tratar un fallo de lectura como «esa BD ya no la tiene» convertiría un corte de red en
autorización para destruir metadata.

### El `message` nunca trae el error del motor

Por el criterio R4 del módulo: el mensaje nativo puede llevar host, usuario o fragmentos de
sentencia. El `message` y el `public_context` solo nombran el id de la BD y un motivo del
vocabulario cerrado; el detalle queda en el log del gateway, correlacionado por Request ID.
**No intenten parsear el mensaje para sacar el motivo** — está en `reason`.

---

## 3. `sql_frozen` / `deletable` / `block_reason` no cambian de forma

Los tres campos de `GET /database-models/{id}/migrations` (listado y detalle) siguen igual.
Cambia **cuándo** valen `true`:

- `sql_frozen: true` ahora significa "alguna BD la tiene aplicada hoy **o** hay una aplicación
  parcial a medias", no "alguna vez corrió".
- `block_reason` conserva su vocabulario: `"applied"` \| `"partial"` \| `"not_tip"` \| `null`.

**Estos flags salen de la caché de versión del inventario** (`ManagedDatabase.model_version`),
porque se calculan por cada fila de cada página y abrir una conexión por BD para pintar un botón
no se sostiene. El 409 de `PATCH`/`DELETE` lee **el motor en vivo**.

**Consecuencia para la SPA**: los flags son una *predicción*, no una garantía. Si la caché quedó
atrasada, el botón habilitado puede terminar en 409 — y la UI tiene que manejarlo mostrando el
`public_context`, no asumiendo que "si el botón estaba habilitado, no puede fallar". La
divergencia inversa (botón deshabilitado y operación permitida) solo puede ocurrir si la caché
sobreestima la versión, o sea congelando de más: nunca abre nada indebidamente.

---

## 4. ⚠️ `stamp` sigue siendo la puerta trasera

`POST /managed-databases/{id}/migrations/stamp` mueve el puntero de versión **sin ejecutar ni
deshacer una sola sentencia**. Una base stampeada hacia atrás reporta una versión anterior
mientras **conserva físicamente** los cambios de la versión que dejó de nombrar.

Sobre esa base, este guard deja editar o borrar la descripción de cambios que siguen en el
motor. Es el mismo límite que tiene cualquier control basado en la versión, y está aceptado
explícitamente. **La UI del `stamp` debería decirlo**: no es "corregir un número", es declarar
un estado que el gateway no puede verificar.

---

## 5. Checklist para la SPA

- [ ] Clasificar el 409 por `public_context.code`, no por prosa del `message`.
- [ ] Renderizar `blocking_databases[]` como lista accionable (una fila por BD, con su
      `reason` traducido y enlace a la base).
- [ ] Distinguir `still_applied` de `unreadable`: el primero se resuelve revirtiendo, el
      segundo arreglando la conexión. Ofrecer el mismo CTA para ambos manda al operador al
      lugar equivocado.
- [ ] No ofrecer «Forzar»: ninguno de los dos 409 tiene escape.
- [ ] Aceptar que `deletable: true` puede terminar en 409 y mostrar el detalle en vez de un
      error genérico.
- [ ] Revisar los textos que decían «ya fue aplicada con éxito»: ahora el criterio es «alguna
      BD la tiene aplicada **hoy**», y la diferencia importa — es exactamente lo que habilita
      revertir para poder corregir.
