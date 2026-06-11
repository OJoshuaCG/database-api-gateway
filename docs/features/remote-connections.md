# Capa de Conexión Remota y Adaptadores

Es el "motor" del gateway: cómo se conecta a servidores **destino** y cómo ejecuta
operaciones de administración e introspección de forma segura y multi-motor.

Se compone de dos piezas:

1. **`app/core/remote_engine.py`** — fábrica dinámica de conexiones SQLAlchemy por servidor.
2. **`app/services/db_admin/`** — adaptadores por dialecto + utilidades de seguridad.

> ⚠️ No confundir con `app/core/database.py` (clase `Database`): ese es un **singleton
> de una sola conexión** para la BD de metadatos del gateway. Para los servidores
> destino se usa **siempre** esta capa.

---

## 1. `remote_engine.py` — conexión dinámica

### `ServerTarget`

Estructura inmutable con los datos para conectarse. El `admin_password` llega **ya
descifrado** (el controller lo descifra en memoria) y esta capa nunca lo loguea.

```python
ServerTarget(server_id=1, dialect="mysql", host="10.0.0.5",
             port=3306, admin_user="root", admin_password="...")
```

### Fábrica y cache de engines

```python
from app.core import remote_engine

engine = remote_engine.get_engine(target)              # conexión "a nivel servidor"
engine = remote_engine.get_engine(target, "mi_basede") # conexión a una BD concreta
```

- **Driver por dialecto:** `mysql+pymysql` (mysql/mariadb), `postgresql+psycopg` (postgresql).
- **`NullPool`:** no se mantienen pools persistentes contra cada servidor destino (serían
  decenas de conexiones `sleep`). Se **cachea el engine** (caro de construir), no las
  conexiones; cada operación abre y cierra su conexión real.
- **Conexión a nivel servidor vs. BD concreta:** `database=None` → MySQL conecta sin BD;
  PostgreSQL conecta a `postgres`. Una BD concreta se usa para introspección y grants de PG.
- **Timeouts por dialecto:** `REMOTE_CONNECT_TIMEOUT` (TCP) y `REMOTE_STATEMENT_TIMEOUT_MS`
  (ejecución). En MySQL vía `read_timeout`; en PostgreSQL vía `statement_timeout`/`lock_timeout`.

### Context managers

```python
with remote_engine.server_connection(target) as conn:     # AUTOCOMMIT (DDL/DCL)
    conn.execute(text("SHOW DATABASES"))

with remote_engine.database_connection(target, "app") as conn:  # BD concreta
    ...
```

`server_connection` usa `AUTOCOMMIT` (requerido por PostgreSQL para `CREATE/DROP DATABASE`
y consistente para DCL en MySQL).

### Ciclo de vida del cache

```python
remote_engine.invalidate_server(server_id)  # al rotar credencial / borrar servidor
remote_engine.dispose_all()                 # en el shutdown (lifespan de main.py)
```

### Traducción de errores → `AppHttpException`

`map_driver_error()` convierte errores del driver en respuestas HTTP coherentes,
**sin filtrar la credencial ni la URL**:

| Origen | HTTP |
|---|---|
| No conecta / acceso denegado del admin | `502` |
| Timeout (statement/lost connection) | `504` |
| Objeto ya existe / dependencias | `409` |
| Objeto inexistente | `404` |
| Permiso insuficiente | `403` |
| Otro | `500` |

Detecta el código por `errno` (pymysql) o `SQLSTATE` (psycopg). Los mensajes largos del
driver **no** se vuelcan en el `context` (solo códigos cortos).

---

## 2. `db_admin/` — adaptadores por dialecto

### `ServerAdapter` (contrato)

`base_adapter.py` define la interfaz común y `get_adapter(target)` (en `factory.py`)
devuelve la implementación correcta:

```python
from app.services.db_admin import get_adapter

adapter = get_adapter(target)          # MySQLAdapter | MariaDBAdapter | PostgresAdapter
adapter.test_connection()              # ConnectionInfo
adapter.list_databases()               # list[str]
adapter.list_users()                   # list[EngineUserInfo]
adapter.list_tables("app")             # list[str]
adapter.get_table_schema("app", "users")  # TableSchema
```

**Métodos de escritura** (implementados, sin endpoint aún — Iteración 2):
`create_database`, `drop_database`, `create_user`, `drop_user`, `change_password`,
`grant_database`, `revoke_database`.

### Diferencias entre motores que encapsula

| Tema | MySQL / MariaDB | PostgreSQL |
|---|---|---|
| Usuario | `'usuario'@'host'` | ROLE con `LOGIN` |
| Permiso sobre BD | `GRANT ... ON db.* TO ...` | `GRANT CONNECT ON DATABASE` + `GRANT ... ON SCHEMA/TABLES` (dos niveles) |
| Propiedad de BD | concepto lógico (metadatos + grants) | `OWNER` nativo (`ALTER DATABASE ... OWNER TO`) |
| Listar BDs | `INFORMATION_SCHEMA.SCHEMATA` (excl. system) | `pg_database` (excl. templates + `postgres`) |
| Listar usuarios | `mysql.user` | `pg_roles` con `rolcanlogin` |
| `CREATE/DROP DATABASE` | normal | requiere AUTOCOMMIT |

### Introspección (solo estructura)

`get_table_schema`/`list_tables` usan el **`Inspector` de SQLAlchemy** (`inspect(conn)`),
que es read-only por diseño y cross-dialect: nunca lee filas. Devuelve DTOs
(`dtos.py`): `ColumnInfo`, `ForeignKeyInfo`, `IndexInfo`, `TableSchema`, etc.

---

## Seguridad de identificadores

`app/services/db_admin/identifiers.py` — **pieza crítica anti-inyección.** Los nombres
de objetos (BD, usuario, tabla, host) **no** pueden ir como bind params en DDL/DCL: van
interpolados. La defensa es en profundidad:

1. **Validación por whitelist** estricta: `^[A-Za-z_][A-Za-z0-9_]{0,62}$` (longitud por
   dialecto). Rechaza espacios, `;`, comillas, backticks, no-ASCII → `AppHttpException(422)`.
   El valor crudo **no** se incluye en el error (evita reflejar el payload).
2. **Quoting por dialecto** con escape del delimitador: backtick en MySQL, comilla doble
   en PostgreSQL.
3. **Valores** (passwords): parametrizados donde el dialecto lo permite; donde no
   (`IDENTIFIED BY '...'`), `quote_string_literal()` escapa y rechaza bytes nulos.

```python
from app.services.db_admin import identifiers

identifiers.validate_identifier("mi_bd", "mysql")     # OK → "mi_bd"
identifiers.validate_identifier("bad;name", "mysql")  # → AppHttpException 422
identifiers.quote_identifier("mi_bd", "mysql")        # → `mi_bd`
identifiers.quote_identifier("Tbl", "postgresql")     # → "Tbl"
```

> Nota: la whitelist también rechaza nombres válidos con `-` o `.`. Si necesitas
> introspeccionar BDs preexistentes con esos nombres, ver
> [deuda técnica #3](../plans/00-deuda-tecnica-y-pendientes.md).

---

## Concurrencia y rendimiento

- El cache de engines está protegido por un `threading.Lock`.
- Los endpoints que tocan el motor se declaran `def` (no `async def`): FastAPI los
  ejecuta en el threadpool, de modo que un timeout remoto (hasta 10–15 s) **no bloquea
  el event loop** ni al resto de peticiones.

## Pruebas relacionadas

- `tests/test_remote_engine.py` — URLs por dialecto, cache/invalidate, mapeo de errores, no-fuga de credenciales.
- `tests/test_identifiers.py` — validación y quoting (incluye rechazo de inyección).
- `tests/test_introspection.py` — parsing de columnas/PK/FK/índices con `Inspector` real (sobre SQLite).

---

**Siguiente**: [Cifrado de credenciales](encryption.md)
