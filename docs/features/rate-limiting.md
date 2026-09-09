# Rate Limiting

Mecanismo que limita el número de solicitudes que un cliente puede realizar en un período de tiempo. Se aplica **por IP** de forma automática a todas las rutas, sin requerir cambios en los endpoints.

Implementado con [SlowAPI](https://github.com/laurentS/slowapi), un wrapper de `limits` para FastAPI/Starlette.

---

## Cómo funciona

El sistema se compone de tres partes que trabajan juntas:

```
app/core/limiter.py          ← Singleton Limiter (instancia compartida)
app/core/versioned_app.py    ← Registra SlowAPIMiddleware + app.state.limiter
app/exceptions/              ← Handler para RateLimitExceeded (429)
```

**Flujo de una request rechazada:**

```
Request (IP: 1.2.3.4)
  ↓
SlowAPIMiddleware → consulta contador en memoria para esa IP
  ↓ (si superó el límite)
Lanza RateLimitExceeded
  ↓
rate_limit_handler → devuelve HTTP 429
```

### Singleton: `app/core/limiter.py`

```python
from slowapi import Limiter
from slowapi.util import get_remote_address
from app.core.environments import RATE_LIMIT_DEFAULT

limiter = Limiter(
    key_func=get_remote_address,       # Clave de límite = IP del cliente
    default_limits=[RATE_LIMIT_DEFAULT],  # Límite global por defecto
)
```

- `key_func=get_remote_address` — cada IP tiene su propio contador independiente
- `default_limits` — se aplica a **todas** las rutas automáticamente, sin decoradores
- La instancia es un **singleton** importado directamente en cualquier parte del código

### Registro en sub-app: `app/core/versioned_app.py`

```python
# SlowAPI requiere dos cosas en la sub-app:

versioned.add_middleware(SlowAPIMiddleware)   # Intercepta responses y aplica límites
versioned.state.limiter = limiter            # Expone el limiter para que el middleware lo encuentre
versioned.add_exception_handler(RateLimitExceeded, rate_limit_handler)  # Maneja el 429
```

Esto se configura automáticamente en `create_versioned_app()`. No se necesita tocar nada al crear nuevas versiones de API.

---

## Configuración

En el archivo `.env`:

```env
RATE_LIMIT_DEFAULT=100/minute
```

Formatos válidos:

| Formato | Descripción |
|---|---|
| `10/second` | 10 solicitudes por segundo |
| `100/minute` | 100 solicitudes por minuto |
| `1000/hour` | 1000 solicitudes por hora |
| `10000/day` | 10000 solicitudes por día |

El valor se lee en `app/core/environments.py` y se pasa al singleton en `app/core/limiter.py`.

---

## Límite global (automático)

El `default_limits` del `Limiter` aplica a todas las rutas sin ninguna anotación adicional. Cada IP tiene su propio contador que se resetea al finalizar el período.

```python
# ✅ Este endpoint ya está limitado por RATE_LIMIT_DEFAULT, sin ningún decorador
@router.get("/users")
async def list_users(pagination: PaginationDep):
    ...
```

---

## Límite por ruta (decorador)

Para sobrescribir el límite global en un endpoint específico, se usa `@limiter.limit()`.

> **Requisito**: el endpoint debe recibir `request: Request` como parámetro. SlowAPI lo necesita para leer la IP.

```python
from fastapi import Request
from app.core.limiter import limiter

@router.post("/login")
@limiter.limit("5/minute")          # Más estricto: solo 5 intentos por minuto
async def login(request: Request, credentials: LoginSchema):
    ...

@router.get("/public-stats")
@limiter.limit("500/minute")        # Más permisivo: ruta de solo lectura pública
async def get_public_stats(request: Request):
    ...
```

### Múltiples límites en un mismo endpoint

Se pueden apilar varios `@limiter.limit()` para definir ventanas distintas simultáneamente:

```python
@router.post("/register")
@limiter.limit("3/minute")          # Máx. 3 por minuto
@limiter.limit("10/hour")           # Y además máx. 10 por hora
async def register(request: Request, body: RegisterSchema):
    ...
```

Ambos límites deben cumplirse. Si alguno se supera, la request es rechazada.

---

## Respuesta al superar el límite

```
HTTP 429 Too Many Requests
```

```json
{
  "detail": {
    "msg": "Demasiadas solicitudes. Límite: 100 per 1 minute",
    "type": "RateLimitExceeded"
  }
}
```

El handler está en `app/exceptions/HandlerExceptions.py`:

```python
async def rate_limit_handler(request: Request, exc: RateLimitExceeded):
    detail_error = {
        "msg": f"Demasiadas solicitudes. Límite: {exc.detail}",
        "type": "RateLimitExceeded",
    }
    ...
    return JSONResponse(status_code=429, content={"detail": detail_error})
```

---

## Logging

Si `LOGGER_EXCEPTIONS_ENABLED=True` en `.env`, cada request rechazada genera un log en nivel `WARNING`:

```
[WARNING] a1b2c3d4e5f6g7h8 | Exception: RateLimitExceeded | Limit: 100 per 1 minute | IP: 1.2.3.4
```

Campos del log:

| Campo | Descripción |
|---|---|
| `a1b2c3d4e5f6g7h8` | Request ID generado por ContextMiddleware |
| `Limit` | Límite que fue superado (global o por ruta) |
| `IP` | IP del cliente que superó el límite |

---

## Consideraciones con proxies y load balancers

`get_remote_address` lee la IP directamente de la conexión TCP (`request.client.host`). Si la app está detrás de un proxy (nginx, Traefik, AWS ALB), la IP real del cliente llega en el header `X-Forwarded-For`, no en la conexión directa.

**Sin configuración extra, el rate limiting aplicará a la IP del proxy, no del cliente real.**

Para solucionarlo, configurar el número de proxies confiables en el `Limiter`:

```python
# app/core/limiter.py
from slowapi import Limiter
from slowapi.util import get_remote_address

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    # Leer la IP real desde X-Forwarded-For (1 proxy confiable)
    headers_enabled=True,
)
```

> Verificar también que el proxy esté configurado para inyectar `X-Forwarded-For` correctamente.

---

## Backend de almacenamiento (memoria vs Redis)

Por defecto, los contadores se mantienen **en memoria del proceso**. Con múltiples workers (gunicorn, uvicorn con `--workers N`), cada proceso tiene su propio contador independiente — un cliente podría hacer hasta `N × RATE_LIMIT_DEFAULT` requests antes de ser bloqueado.

El backend se controla con una variable de entorno, sin tocar código:

```env
# Memoria (default) — sin dependencias externas, válido para desarrollo y un solo worker
RATE_LIMIT_REDIS_ENABLED=False

# Redis — contadores compartidos entre todos los workers
RATE_LIMIT_REDIS_ENABLED=True
RATE_LIMIT_REDIS_URL=redis://localhost:6379
```

`app/core/limiter.py` selecciona el backend automáticamente al arrancar:

```python
limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[RATE_LIMIT_DEFAULT],
    storage_uri=RATE_LIMIT_REDIS_URL if RATE_LIMIT_REDIS_ENABLED else "memory://",
)
```

## En quién se confía para leer `X-Forwarded-For`

Detrás de un proxy reverso, la IP que ve uvicorn es la del proxy, así que la clave del rate
limit sale de `X-Forwarded-For`. **Y esa cabecera la manda el cliente.**

El entrypoint pasaba `--forwarded-allow-ips "*"`, o sea confiaba en la cabecera de cualquier
origen: con eso el cliente **elige su propia clave de rate limit y la rota por request**. Los
5/min del login y los 3/min del `DROP DATABASE` se evadían con un header.

```env
# Solo localhost (default) — el default de uvicorn, no spoofeable
TRUSTED_PROXY_IPS=127.0.0.1

# Detrás de nginx en otro contenedor: la IP o el CIDR del proxy
TRUSTED_PROXY_IPS=10.0.0.0/24
```

En **producción la app se niega a arrancar con `*`**. Y ojo con el otro extremo: si el proxy no
está declarado, `X-Forwarded-For` se ignora y **todos los clientes comparten la clave del
proxy** — más restrictivo, no spoofeable, pero un solo usuario puede agotar la cuota de todos.
Declarar el CIDR real es lo correcto; el default solo es el lado seguro de no haberlo hecho.
Para usar Redis en producción:

```bash
uv add redis
```

```yaml
# docker-compose.yml
services:
  redis:
    image: redis:7-alpine
    ports:
      - "6379:6379"
```

---

## Desactivar el límite en una ruta

Para excluir un endpoint completamente del rate limiting:

```python
@router.get("/internal/health")
@limiter.exempt
async def internal_health():
    return {"status": "ok"}
```

> Usar con cuidado. Solo apropiado para rutas internas no expuestas públicamente.
