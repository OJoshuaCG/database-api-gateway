# API v31 — Estado de acceso de agentes en `ManagedDatabaseOut`

Addendum que cierra el pendiente de [v24 §10.1](api-reference-v24.md): el estado de acceso de
agentes (MCP) de cada base gestionada, que se escribía con `PUT /managed-databases/{id}/agent-access`
pero no se devolvía en ninguna respuesta.

## Resumen para el frontend

| Cambio | Dónde | Capacidad |
|---|---|---|
| Dos campos nuevos en `ManagedDatabaseOut` | toda ruta que devuelve una base | `databases.read` |

Es un **cambio de forma del envelope**, aditivo: no hay rutas nuevas y ningún campo existente
cambia. Ver la advertencia de la cabecera de v24 sobre cambios de forma.

## `ManagedDatabaseOut`: dos campos nuevos

```jsonc
{
  "agent_access_allowed": false,  // opt-in por base; default false
  "agent_access_blocked": false   // veto de emergencia; default false
}
```

- `agent_access_allowed`: el opt-in. Es el eje que **decide** el alcance: sin esto en `true`,
  ningún agente ve la base aunque su entorno los permita.
- `agent_access_blocked`: el veto de emergencia. **Gana** sobre `allowed` y no tiene override. Con
  `allowed: true, blocked: true` la base está cerrada; el campo crudo muestra igual los dos ejes.
- Los dos tienen default `false`: una base recién creada está cerrada a agentes y un cliente que
  no los conoce los ignora sin romperse. Un cliente nuevo debe leer un campo ausente como `false`
  (falla cerrado), nunca como «abierta».

Aparecen en: `POST`/`GET`/`PATCH` de `/managed-databases`, la lista, las bases de un usuario de
servidor, las bases de un modelo (`ModelDatabaseStatusOut`) y la respuesta del propio
`PUT .../agent-access`, que ahora confirma el estado resultante.

## Quién los ve

Todo el que tenga `databases.read`. No hay filtro por `environments.write`: el incremento de
información es chico (el entorno ya expone `allows_agent_access` y `list_databases` del MCP
revela las abiertas). **Escribirlos** sigue siendo `environments.write` (`security_officer`) con
step-up.

## Habilitar un entorno no abre ninguna base

`PATCH /environments/{id}` con `allows_agent_access: true` (que exige `?confirm_slug=`) solo
habilita la **posibilidad**. El default-deny es por base: cada una necesita su propio
`agent_access_allowed = true`. Una pantalla debe decirlo, porque `allows_agent_access` en `true`
con todas las bases en `false` significa que ningún agente ve nada.

## Auditoría

`managed_database.agent_access_open` y `managed_database.agent_access_close` incluyen ahora en el
`detail` el estado de acceso previo (`antes`). La semántica no cambia: abrir sigue siendo
fail-closed.

## Orden de despliegue

**Backend primero, frontend después.** El frontend define los campos con default `false`, así que
contra un backend viejo todo se lee como cerrado; el orden inverso mostraría «cerrada» sobre
bases que quizá estén abiertas.
