# Proyectos — agrupación de blueprints

Un **proyecto** es una entidad deliberadamente vacía: **nombre** + **descripción larga**
(hasta 5000 caracteres) y una lista de blueprints. No tiene servidor, ni motor, ni versión,
ni credenciales, ni estado de despliegue. Su única función es dar nombre al conjunto de
blueprints que participan de una misma iniciativa.

## Por qué existe

Los blueprints estaban **sueltos**: una lista plana donde no se veía qué bases pertenecen a
qué iniciativa. Un proyecto "Citas" puede involucrar 2 tipos de base de datos distintos — 2
blueprints — y un "Omnicanal" 4. Esa pertenencia solo existía en la cabeza de quien operaba.

## El modelo: N:M, opcional en los dos sentidos

| Lado | Cardinalidad |
| --- | --- |
| Un proyecto | 0..N blueprints |
| Un blueprint | 0..N proyectos |

Un blueprint **no necesita** pertenecer a ningún proyecto, y puede pertenecer a varios. Ese
segundo punto es el que decide la implementación: una columna `project_id` en
`database_models` forzaría "un blueprint, un proyecto" y habría que migrarla el primer día
que dos iniciativas compartan una base — que es el caso normal, no la excepción. De ahí la
tabla pivote `project_database_models`, con **clave primaria compuesta** `(project_id,
model_id)`: el par ES la identidad de la fila, así que un vínculo duplicado es imposible
incluso ante un bug del controller.

## REGLA DURA: borrar un proyecto NO borra blueprints

`DELETE /projects/{id}` borra la entidad y sus **vínculos**. Los blueprints quedan intactos
—con sus migraciones, sus BDs gestionadas y su historial—, y lo mismo vale al revés: borrar
un blueprint suelta su pertenencia a los proyectos, no borra proyectos.

No es una casualidad de la implementación, es una regla del módulo. Un blueprint es el
esquema que replican N bases de datos reales con datos reales; que un **agrupador** pueda
arrastrarlas sería una pérdida de datos causada por una operación de organización. Está
sostenida en tres capas:

1. Los dos `ondelete="CASCADE"` de la pivote apuntan al **vínculo**, nunca al objeto del
   otro lado.
2. El controller borra los vínculos **explícitamente** antes de la fila, en la misma
   transacción. No es redundante: SQLite no aplica claves foráneas salvo que se active
   `PRAGMA foreign_keys`, así que en los entornos de test el cascade del motor no dispara y
   quedarían filas huérfanas.
3. `tests/test_api_projects.py::test_delete_project_keeps_blueprints` y su simétrico
   `test_delete_blueprint_keeps_projects`. Si alguna vez fallan, hay pérdida de datos, no un
   detalle de API.

Por eso el borrado tampoco pide `confirm_target_name` ni `confirm_token` como los borrados
destructivos del gateway: acá no se pierde nada que no se recupere con dos llamadas.

## Endpoints

Todos requieren sesión de admin (`AdminDep`) y ninguno toca un motor destino: es CRUD sobre
la BD de metadatos del gateway.

| Método | Ruta | Qué hace |
| --- | --- | --- |
| `GET` | `/projects` | Lista paginada (`?page=&size=`), con `blueprint_count` por proyecto |
| `POST` | `/projects` | Crea. `model_ids` opcional vincula en el mismo alta |
| `GET` | `/projects/{id}` | Detalle |
| `PATCH` | `/projects/{id}` | Cambia `name` y/o `description` |
| `DELETE` | `/projects/{id}` | Borra proyecto + vínculos. **Blueprints intactos** |
| `GET` | `/projects/{id}/blueprints` | Blueprints del proyecto (`DatabaseModelOut`) |
| `POST` | `/projects/{id}/blueprints` | Vincula uno o varios (`{"model_ids": [1,2]}`) |
| `DELETE` | `/projects/{id}/blueprints/{model_id}` | Suelta un vínculo |
| `GET` | `/database-models/{id}/projects` | Vista **inversa**: proyectos de un blueprint |

### `POST /projects`

```json
{ "name": "Omnicanal", "description": "…hasta 5000 caracteres…", "model_ids": [3, 7, 11, 12] }
```

`name` se recorta en los extremos y es **único** (409 si se repite). `description` es
opcional y nulable.

### `POST /{id}/blueprints` — idempotente y todo-o-nada

```json
{ "model_ids": [3, 7] }
```

```json
{ "data": { "project_id": 1, "linked": [7], "already_linked": [3], "blueprint_count": 2 } }
```

Dos comportamientos que conviene tener claros:

- Un blueprint **ya vinculado no es un error**: sale en `already_linked` con 200. Reenviar la
  selección completa desde la UI es la operación natural y no debe fallar.
- Un id **inexistente** devuelve **422** con `missing_model_ids` y **no vincula ninguno**.
  Vincular los válidos e ignorar el resto dejaría al cliente creyendo que su selección entró
  completa; es la misma política de selección explícita que usa schema-comparisons.

### `PATCH /projects/{id}`

Parcial: sin campos no cambia nada. `description: null` **sí limpia** la descripción — es la
única forma de distinguir "no enviado" de "vaciar", así que se asigna por **presencia** de la
clave. `name: null` no significa nada y se ignora.

## Gotchas

- **El tope de 5000 caracteres vive en el schema Pydantic**, no en la columna, que es `Text`.
  Subirlo es cambiar la constante `DESCRIPTION_MAX_LENGTH` en `app/schemas/project.py`, sin
  migración. Un `VARCHAR(5000)` en MySQL/MariaDB además consume presupuesto del límite de
  65 535 bytes por fila, y con `utf8mb4` son 4 bytes por carácter.
- **`GET /{id}/blueprints` no está paginado** a propósito: son unidades por proyecto, no
  miles.
- `blueprint_count` sale de **una sola query** para toda la página (`GROUP BY` sobre la
  pivote). Contarlo por fila serían 21 consultas para pintar una tabla de 20 proyectos.
- **El nombre es único.** Dos proyectos "Citas" son indistinguibles en cualquier selector, y
  el 409 es más útil que el duplicado silencioso.
- Auditoría: `project.create`, `project.update`, `project.delete` (con cuántos vínculos se
  soltaron), `project.blueprints.link`, `project.blueprints.unlink`. Todas con
  `touched_engine=False`: ninguna operación de este módulo abre una conexión a un motor.

## Archivos

- `app/models/project.py` — `Project` + `ProjectDatabaseModel` (pivote)
- `app/schemas/project.py` — `DESCRIPTION_MAX_LENGTH`, `ProjectCreate/Update/Out`,
  `ProjectBlueprintsIn`, `ProjectBlueprintsLinkOut`
- `app/controllers/project_controller.py`
- `app/routes/v1/projects.py` — `router` (`/projects`) + `model_router` (`/database-models`)
- Migración `d8e9f0a1b2c3`
- `tests/test_api_projects.py` — 22 casos
