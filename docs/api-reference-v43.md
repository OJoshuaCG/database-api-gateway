# API v43 — Capacidades puntuales: decisión masiva y alta masiva multi-capacidad

Addendum de [v42](api-reference-v42.md). Dos cambios sobre `capability_grants`, ambos solo para
`access_admin` (`access.admin`, con step-up). Por qué la decisión masiva es de mejor esfuerzo y el
alta es todo o nada, en `docs/development/decisiones-e-incidentes.md`.

**Step-up.** El step-up es una **ventana de 5 minutos** por sesión, no una confirmación por
request: una sola contraseña cubre toda la llamada masiva y las siguientes dentro de la ventana.
Con la ventana cerrada, ambas rutas responden `403 access.step_up_required` antes de cualquier efecto.

## 1. `POST /api/v1/capability-grants/decisions` (decisión masiva)

Aprueba o rechaza varias solicitudes pendientes con una sola decisión. **Límite: 10/minute.**

Request:

```json
{ "decision": "approve", "ids": [41, 42, 43], "reason": "lote del viernes" }
```

| Campo | Tipo | Regla |
|---|---|---|
| `decision` | `"approve" \| "reject"` | obligatorio |
| `ids` | `int[]` | 1 a 100, cada uno >= 1; los repetidos se colapsan conservando el primero |
| `reason` | `string \| null` | opcional, <= 500; se guarda como `decision_reason` de cada fila decidida |

Pasar de 100 ids, una lista vacía, un id < 1 o un `decision` desconocido es `422` de validación.

**Siempre `200`** (si el cuerpo es válido y la persona es `access_admin`). Cada ítem se decide por
separado con las reglas del endpoint individual (`approve`/`reject`, que comparten `_block_reason`):
un ítem bloqueado no frena ni revierte a los demás.

```json
{
  "data": {
    "requested": 3,
    "succeeded": 2,
    "failed": 1,
    "results": [
      { "id": 41, "ok": true,  "grant": { "...": "CapabilityGrantOut" } },
      { "id": 42, "ok": false, "code": "access.self_approval_forbidden",
        "message": "No puedes aprobar una solicitud que pediste tú: ..." },
      { "id": 43, "ok": true,  "grant": { "...": "CapabilityGrantOut" } }
    ]
  },
  "message": "2 de 3 solicitudes aprobadas. 1 con error."
}
```

- `results` va en el **orden pedido** (ya deduplicado). `grant` solo viene si `ok`; `code` y
  `message` solo si no.
- Códigos por ítem (los mismos que el endpoint individual): `access.grant_not_found` (id
  inexistente), `access.grant_not_pending` (ya decidida, vencida, cancelada, o el solicitante
  perdió `access_admin`), `access.self_approval_forbidden`, `access.self_modification_forbidden`,
  `access.grant_user_inactive`, `access.grant_scope_not_found`, `access.capability_not_grantable`,
  `access.not_assignable`, `access.sod_conflict` (sin `context`: abrir la solicitud individual
  para ver reglas y fuentes).
- `access.grant_decision_failed`: error **inesperado** en ese ítem. Mensaje fijo, sin detalle
  interno; el tipo de la excepción queda en el log. Reintentar es seguro.
- **Idempotente:** repetir el mismo lote devuelve `access.grant_not_pending` en lo ya decidido.
- Rechazar nunca da acceso: quien pidió también puede rechazar sus propias solicitudes en lote.
- Errores de la llamada entera: `401`, `403 access.forbidden`, `403 access.step_up_required`, `429`.

**Auditoría.** Cada ítem conserva sus filas de siempre (`capability_grant.approved|rejected`,
éxito y fallo) con un `bulk_id` común en el `detail`, más **una** fila agregada
`capability_grant.bulk_decided` con `{bulk_id, decision, requested, succeeded, failed}`. El id
inexistente no genera fila por ítem (igual que en el endpoint individual).

## 2. `POST /api/v1/gateway-users/{id}/capability-grants/bulk` (multi-capacidad)

Retrocompatible. El cuerpo admite **exactamente una** de dos formas (ninguna o ambas: `422`):

```json
{ "capability": "databases.write", "scope_type": "environment", "scope_ids": [1, 2] }
{ "capabilities": ["databases.write", "exports.download"], "scope_type": "environment",
  "scope_ids": [1, 2], "reason": "...", "sod_override": null }
```

- `capabilities`: 1 a 100 textos únicos de 1 a 64 caracteres (los repetidos se colapsan).
- **Tope de pares:** `len(capabilities) x len(scope_ids) <= 100`. Si no, `422
  access.grant_bulk_too_large`. El tope es inclusivo.
- Sigue siendo **todo o nada**: se valida cada par capacidad x destino (alcance existe, no
  duplicado, separación de deberes) y se recogen TODOS los fallos. Si hay alguno, `409
  access.grant_bulk_failed` y no se inserta nada; si no, todo entra en una transacción.
- Los errores **de la persona o de la capacidad** (auto-otorgamiento, persona desactivada,
  `access.capability_not_grantable`, `access.not_assignable`) no son de un par: se lanzan directo
  con el primero que falle, como en `POST /{id}/capability-grants`. No traen `failures`.

`409 access.grant_bulk_failed` — `detail.public_context.failures[]` suma un campo aditivo (también
con el cuerpo viejo `capability`):

```json
{ "scope_id": 999, "capability": "databases.write",
  "code": "access.grant_scope_not_found", "message": "...", "context": { } }
```

(`context` solo existe para `access.sod_conflict`, como antes.)

`201` — la forma de la respuesta no cambia:

```json
{ "data": { "count": 4, "pending": true, "grants": [ { "...": "CapabilityGrantOut" } ] } }
```

- `grants` va en orden capacidad-mayor: primero todos los destinos de la 1ª capacidad, luego los
  de la 2ª.
- El estado se decide **por capacidad**: una sensible nace `pending` (vence a los 7 días) y una
  común `active`, en el mismo pedido (salvo `ACCESS_FOUR_EYES=False` o la ventana de arranque,
  que las dejan activas y auditan `access.elevation_unapproved`/`access.bootstrap_assignment` una vez
  por pedido). Por eso `pending` es **`true` si alguna fila nació pendiente**; el estado exacto de
  cada una está en `grants[].status`.
- Auditoría: una fila por grant (`capability_grant.requested` o `.created`, según la capacidad)
  con **un** `bulk_id` para todo el pedido.
