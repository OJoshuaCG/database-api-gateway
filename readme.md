# Database API Gateway

> **Gateway de administración de servidores de bases de datos.** Permite registrar
> múltiples servidores remotos (MySQL, MariaDB, PostgreSQL) y, mediante una credencial
> *pseudo-root*, gestionar usuarios del motor, bases de datos y permisos, además de
> inspeccionar la **estructura** de las tablas (nunca los datos).

Construido sobre una plantilla profesional de FastAPI (arquitectura MVC, versionado de
API, middlewares, manejo de errores y utilidades). El estado actual corresponde a la
**Iteración 1** del [roadmap](docs/plans/README.md).

---

## ¿Qué hace?

El sistema separa dos planos:

- **Plano de control (este gateway):** FastAPI + su propia base de datos de metadatos
  (ORM + Alembic). Guarda el *inventario*: servidores, y a futuro usuarios, bases de
  datos y modelos. **No** guarda datos de las BDs gestionadas.
- **Plano gestionado (servidores destino):** los servidores reales MySQL/MariaDB/
  PostgreSQL. El gateway se conecta con la credencial pseudo-root para administrar
  usuarios, bases de datos, permisos e introspeccionar estructura.

```
                 ┌──────────────────────────────┐
   Admin  ──────▶│   Database API Gateway        │
 (sesión)        │   FastAPI + BD de metadatos   │
                 └───────────────┬───────────────┘
                                 │ pseudo-root (cifrada)
              ┌──────────────────┼──────────────────┐
              ▼                  ▼                  ▼
        MySQL/MariaDB       PostgreSQL          MySQL ...
        (servidor 1)        (servidor 2)        (servidor N)
```

## Capacidades (Iteración 1)

| Categoría | Funcionalidad |
|---|---|
| **Servidores** | Registrar/editar/eliminar servidores destino; probar conexión (`test-connection`) |
| **Introspección** | Listar bases de datos y usuarios reales; listar tablas y ver el **schema** de una tabla (columnas, PK, FK, índices) — sin leer datos |
| **Multi-motor** | Adaptadores para MySQL, MariaDB y PostgreSQL tras una interfaz común |
| **Seguridad SQL** | Validación + quoting de identificadores (anti-inyección); valores parametrizados o escapados |
| **Credenciales** | Cifrado en reposo con Fernet derivado de `SECRET_KEY` |
| **Autenticación** | Sesión httpOnly firmada + administrador único, detrás de `get_current_admin` |
| **Base (template)** | `ApiResponse[T]`, `AppHttpException`, middlewares, rate limiting, paginación, logging con Request ID |

> Lo que se construye después (usuarios/BDs/modelos, migraciones, aprovisionamiento,
> SSH, clonado) está documentado en [`docs/plans/`](docs/plans/README.md).

---

## Inicio rápido

### 1. Instalar dependencias

```bash
# uv (gestor de paquetes) — si no lo tienes:
curl -LsSf https://astral.sh/uv/install.sh | sh

uv sync
```

### 2. Configurar entorno

```bash
cp .env.example .env
```

Edita `.env`. Variables mínimas para arrancar:

```env
APP_ENV=development
SECRET_KEY=...            # python -c "import secrets; print(secrets.token_hex(32))"
ADMIN_USERNAME=admin
ADMIN_PASSWORD=cambia-esto   # obligatorio en producción

# BD de METADATOS del gateway (usar MySQL/MariaDB en producción)
DB_ENGINE=mysql+pymysql
DB_HOST=localhost
DB_USER=gateway
DB_PASS=...
DB_NAME=gateway
DB_PORT=3306
```

### 3. Migrar y arrancar

```bash
uv run alembic upgrade head          # crea las tablas del inventario + admin
uv run uvicorn main:app --reload
```

Al arrancar se **siembra el administrador** desde `ADMIN_USERNAME`/`ADMIN_PASSWORD`
si no existe.

### 4. Usar la API

```bash
# Login (guarda la cookie de sesión)
curl -c cookies.txt -X POST http://localhost:8000/api/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"cambia-esto"}'

# Registrar un servidor destino
curl -b cookies.txt -X POST http://localhost:8000/api/v1/servers \
  -H 'Content-Type: application/json' \
  -d '{"name":"mysql-prod","host":"10.0.0.5","port":3306,"engine":"mysql","root_username":"root","root_password":"***"}'

# Probar conexión e introspeccionar
curl -b cookies.txt -X POST http://localhost:8000/api/v1/servers/1/test-connection
curl -b cookies.txt http://localhost:8000/api/v1/servers/1/databases
```

| URL | Descripción |
|---|---|
| `GET /health` | Health check (público) |
| `http://localhost:8000/api/v1/docs` | Swagger UI v1 |
| `http://localhost:8000/api/v1/redoc` | ReDoc v1 |

---

## API v1

Todos los endpoints (salvo `login`) requieren sesión de administrador.

| Método | Ruta | Descripción | Toca el motor |
|---|---|---|---|
| POST | `/api/v1/auth/login` | Inicia sesión (rate-limit 5/min) | — |
| POST | `/api/v1/auth/logout` | Cierra sesión | — |
| GET | `/api/v1/auth/me` | Admin actual | — |
| GET | `/api/v1/servers` | Lista servidores (paginado) | no |
| POST | `/api/v1/servers` | Registra un servidor | no |
| GET | `/api/v1/servers/{id}` | Detalle (sin credencial) | no |
| PATCH | `/api/v1/servers/{id}` | Actualiza (re-cifra password si se envía) | no |
| DELETE | `/api/v1/servers/{id}` | Elimina del inventario | no |
| POST | `/api/v1/servers/{id}/test-connection` | Prueba la conexión | **sí** |
| GET | `/api/v1/servers/{id}/databases` | Lista BDs reales | **sí** |
| GET | `/api/v1/servers/{id}/users` | Lista usuarios del motor | **sí** |
| GET | `/api/v1/servers/{id}/databases/{db}/tables` | Lista tablas | **sí** |
| GET | `/api/v1/servers/{id}/databases/{db}/tables/{tabla}/schema` | Estructura de la tabla | **sí** |

Las respuestas usan el envelope estándar `ApiResponse[T]`; los errores devuelven
`{"detail": {...}}`. Ver [Formato de Respuestas](docs/features/response-format.md).

---

## Variables de entorno

Resumen de las **propias del gateway** (las del template están en `.env.example`):

```env
# ======= Crypto =======
CRYPTO_KEY_SALT=db-gateway-static-salt   # sal NO secreta para derivar la clave Fernet

# ======= Conexión a servidores destino =======
REMOTE_CONNECT_TIMEOUT=10                # segundos para abrir conexión TCP
REMOTE_STATEMENT_TIMEOUT_MS=15000        # ms de ejecución de una sentencia remota

# ======= Admin / Sesión =======
ADMIN_USERNAME=admin
ADMIN_PASSWORD=                          # OBLIGATORIO en producción
SESSION_SECRET=                          # si vacío, se deriva de SECRET_KEY
SESSION_MAX_AGE=28800                    # duración de la sesión (s); 8h
```

`SECRET_KEY` es obligatoria en producción y se usa también para derivar la clave de
cifrado de credenciales.

---

## Estructura del proyecto

Sobre la base del template, la Iteración 1 añade (✚):

```
database-api-gateway/
├── app/
│   ├── core/
│   │   ├── crypto.py          ✚ Cifrado Fernet de credenciales
│   │   ├── remote_engine.py   ✚ Conexión dinámica a servidores destino
│   │   ├── auth.py            ✚ Sesión + get_current_admin + bootstrap admin
│   │   ├── database.py          Conexión a la BD de metadatos del gateway (singleton)
│   │   ├── environments.py      Variables de entorno centralizadas
│   │   ├── limiter.py / logger.py / context.py / versioned_app.py
│   ├── services/
│   │   └── db_admin/          ✚ Administración de servidores destino
│   │       ├── base_adapter.py   ServerAdapter (ABC) + introspección
│   │       ├── mysql_adapter.py  MySQLAdapter / MariaDBAdapter
│   │       ├── postgres_adapter.py PostgresAdapter
│   │       ├── identifiers.py     Validación/quoting anti-inyección
│   │       ├── dtos.py / factory.py
│   ├── models/
│   │   ├── server.py          ✚ Modelo ORM Server
│   │   ├── enums.py           ✚ EngineType, ServerStatus
│   │   ├── base.py / user.py / user_model.py / __init__.py
│   ├── controllers/
│   │   ├── server_controller.py ✚ / auth_controller.py ✚
│   ├── routes/v1/
│   │   ├── servers.py         ✚ / auth.py ✚ / routes.py / test.py
│   ├── schemas/               ✚ Schemas Pydantic (server, auth)
│   └── utils/
│       ├── security.py        ✚ Hashing Argon2
│       ├── dict_utils.py        Sanitización de secretos (logs/errores)
│       └── response.py / pagination.py / file_upload.py
├── tests/                     ✚ Suite pytest (85 tests)
├── docs/
│   ├── features/                Documentación por feature
│   └── plans/                 ✚ Roadmap (Iteraciones 2+)
├── alembic/                     Migraciones
├── main.py                      Punto de entrada (lifespan: bootstrap admin)
└── pyproject.toml
```

---

## Testing

```bash
uv run pytest            # 85 tests (identifiers, crypto, remote_engine, auth, API, ...)
uv run pytest -q tests/test_api_servers.py
```

La suite usa SQLite como BD de metadatos (no requiere infraestructura). La
introspección contra MySQL/PostgreSQL reales debe validarse en tu entorno
(ver [deuda técnica](docs/plans/00-deuda-tecnica-y-pendientes.md)).

---

## Documentación

### Módulos del gateway (Iteración 1)
- [Gestión de servidores e introspección](docs/features/server-management.md)
- [Capa de conexión remota y adaptadores](docs/features/remote-connections.md)
- [Cifrado de credenciales](docs/features/encryption.md)
- [Autenticación (sesión + admin)](docs/features/authentication.md)

### Base (template)
- [Inicio Rápido](docs/getting-started.md) · [Estructura](docs/project-structure.md)
- [API Versionada](docs/features/api-versioning.md) · [Respuestas](docs/features/response-format.md)
- [Paginación](docs/features/pagination.md) · [Rate Limiting](docs/features/rate-limiting.md)
- [CORS](docs/features/cors.md) · [Middlewares](docs/features/middlewares.md)
- [Logging](docs/features/logging.md) · [Excepciones](docs/features/exceptions.md)
- [Base de Datos](docs/features/database.md)

### Roadmap
- [Planes futuros (Iteraciones 2+)](docs/plans/README.md)

---

## Tecnologías

- **[FastAPI](https://fastapi.tiangolo.com/)** + **[SQLAlchemy 2.0](https://docs.sqlalchemy.org/)** + **[Alembic](https://alembic.sqlalchemy.org/)**
- **[Pydantic v2](https://docs.pydantic.dev/)** · **[slowapi](https://github.com/laurentS/slowapi)** (rate limiting)
- **[cryptography](https://cryptography.io/)** (Fernet) · **[argon2-cffi](https://argon2-cffi.readthedocs.io/)** (hashing)
- **[PyMySQL](https://pymysql.readthedocs.io/)** (MySQL/MariaDB) · **[psycopg 3](https://www.psycopg.org/psycopg3/)** (PostgreSQL)
- **[pytest](https://docs.pytest.org/)** + **[httpx](https://www.python-httpx.org/)** · **[uv](https://github.com/astral-sh/uv)** · Python 3.13+
