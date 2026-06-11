# Gestión de Servidores e Introspección

Funcionalidad central del gateway: registrar **servidores de base de datos destino**
y operar sobre ellos (probar conexión e inspeccionar estructura). Es la capa visible
de la API; por debajo se apoya en la [capa de conexión remota](remote-connections.md),
el [cifrado de credenciales](encryption.md) y la [autenticación](authentication.md).

## Concepto: los dos planos

- **Inventario (BD del gateway):** el modelo `Server` guarda *cómo* conectarse a cada
  servidor destino (host, puerto, motor y la credencial pseudo-root **cifrada**).
- **Servidor destino:** el motor real. El gateway nunca guarda sus datos; se conecta
  bajo demanda para administrar o introspeccionar.

## Modelo `Server`

`app/models/server.py` (tabla `servers`):

| Campo | Tipo | Notas |
|---|---|---|
| `id` | int PK | |
| `name` | str(100) único | alias legible |
| `host` / `port` | str / int | endpoint del motor |
| `engine` | `EngineType` | `mysql` · `mariadb` · `postgresql` |
| `root_username` | str(128) | usuario pseudo-root |
| `root_password_encrypted` | text | **cifrado Fernet**, nunca se expone |
| `status` | `ServerStatus` | `active` · `inactive` · `unreachable` |
| `is_active` | bool | soft-disable |
| `notes` | text | opcional |
| `created_at` / `updated_at` | datetime | de `TimestampMixin` |

Restricción: `UniqueConstraint(host, port)` — no se registra dos veces el mismo endpoint.

Los enums viven en `app/models/enums.py` y se almacenan como `VARCHAR`
(`native_enum=False`) para portabilidad entre motores.

## Flujo de la feature (MVC)

```
routes/v1/servers.py  →  controllers/server_controller.py  →  ORM (Server)        (inventario)
                                          └──────────────────→  services/db_admin   (motor destino)
```

- El **controller** cifra al crear y descifra en memoria al operar, arma un
  `ServerTarget` y delega en un `ServerAdapter` (vía `get_adapter`).
- La credencial descifrada **nunca** se persiste, serializa ni loguea.

## Endpoints

> Todos requieren sesión de administrador (dependencia `AdminDep`).

### CRUD (solo BD del gateway)

```http
GET    /api/v1/servers                 # lista paginada (?page=&size=)
POST   /api/v1/servers                 # registra (cifra root_password)
GET    /api/v1/servers/{id}            # detalle (sin credencial)
PATCH  /api/v1/servers/{id}            # actualiza (re-cifra si llega root_password)
DELETE /api/v1/servers/{id}            # elimina del inventario
```

**Crear:**

```bash
curl -b cookies.txt -X POST http://localhost:8000/api/v1/servers \
  -H 'Content-Type: application/json' \
  -d '{
        "name": "mysql-prod",
        "host": "10.0.0.5",
        "port": 3306,
        "engine": "mysql",
        "root_username": "root",
        "root_password": "s3cr3t"
      }'
```

Respuesta (nótese que **no** aparece el password; sí `has_root_password`):

```json
{
  "data": {
    "id": 1, "name": "mysql-prod", "host": "10.0.0.5", "port": 3306,
    "engine": "mysql", "root_username": "root", "status": "active",
    "is_active": true, "notes": null, "has_root_password": true,
    "created_at": "2026-06-11T13:31:30", "updated_at": "2026-06-11T13:31:30"
  },
  "message": "Servidor registrado exitosamente."
}
```

### Operaciones sobre el motor destino

```http
POST /api/v1/servers/{id}/test-connection
GET  /api/v1/servers/{id}/databases
GET  /api/v1/servers/{id}/users
GET  /api/v1/servers/{id}/databases/{database}/tables
GET  /api/v1/servers/{id}/databases/{database}/tables/{table}/schema
```

**Probar conexión** — actualiza `status` a `active` o `unreachable`:

```json
{ "data": { "ok": true, "dialect": "mysql", "server_version": "8.0.36" } }
```

**Esquema de una tabla** (solo estructura, nunca filas):

```json
{
  "data": {
    "database": "app", "table": "users",
    "columns": [
      {"name": "id", "type": "INTEGER", "nullable": false, "primary_key": true, "autoincrement": true, "default": null, "comment": null},
      {"name": "email", "type": "VARCHAR(255)", "nullable": false, "primary_key": false}
    ],
    "primary_key": ["id"],
    "foreign_keys": [{"name": "fk_users_role", "columns": ["role_id"], "referred_table": "roles", "referred_columns": ["id"]}],
    "indexes": [{"name": "ix_users_email", "columns": ["email"], "unique": true}]
  }
}
```

## Códigos de error

Los errores remotos se traducen a HTTP por la capa de conexión:

| Situación | Código |
|---|---|
| Servidor no responde | `502` |
| Timeout de conexión/sentencia | `504` |
| Recurso ya existe / dependencias | `409` |
| Recurso inexistente (BD/tabla) | `404` |
| Credencial del gateway sin permiso | `403` |
| Identificador inválido (anti-inyección) | `422` |
| Servidor inexistente en el inventario | `404` |

Todos con el formato `{"detail": {"msg": "...", "context": {...}}}` (el `context`
solo aparece en `APP_ENV=development` y **nunca** contiene credenciales).

## Schemas Pydantic

`app/schemas/server.py`:

- `ServerCreate` — incluye `root_password` (texto plano de entrada; el controller lo cifra).
- `ServerUpdate` — todos los campos opcionales; si llega `root_password`, se re-cifra.
- `ServerOut` — **sin** `root_password_encrypted`; expone `has_root_password: bool`.

## Buenas prácticas

- Tras editar host/credencial de un servidor, el controller llama
  `remote_engine.invalidate_server(id)` para descartar engines cacheados con datos viejos.
- `GET /servers/{id}` y los listados nunca exponen la credencial: el `ServerOut` no la incluye.
- Para introspeccionar una BD concreta, su nombre y el de la tabla pasan por la
  validación de identificadores (ver [seguridad](remote-connections.md#seguridad-de-identificadores)).

## Próximos pasos

La creación real de **usuarios del motor**, **bases de datos** y **permisos** (y el
modelado de propietarios/`ManagedDatabase`) se aborda en la
[Iteración 2](../plans/01-iteracion-2-inventario-y-aprovisionamiento.md). Los métodos
de escritura ya existen en los adaptadores.

---

**Siguiente**: [Capa de conexión remota](remote-connections.md)
