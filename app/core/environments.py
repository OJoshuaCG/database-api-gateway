import ipaddress
import os
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ROOT_DIR = Path(__file__).parent.parent.parent
APP_DIR = ROOT_DIR / "app"

# ======= Application variables ======= #
APP_ENV = os.getenv("APP_ENV", "development")
APP_NAME = os.getenv("APP_NAME", "FastAPI Project")
SECRET_KEY = os.getenv("SECRET_KEY")


# ======= Logger variables ======= #
LOGGER_LEVEL = os.getenv("LOGGER_LEVEL", "INFO")
LOGGER_MIDDLEWARE_ENABLED = (
    os.getenv("LOGGER_MIDDLEWARE_ENABLED", "True").lower() == "true"
)
LOGGER_MIDDLEWARE_SHOW_HEADERS = (
    os.getenv("LOGGER_MIDDLEWARE_SHOW_HEADERS", "False").lower() == "true"
)
LOGGER_MIDDLEWARE_SHOW_QUERY_PARAMS = (
    os.getenv("LOGGER_MIDDLEWARE_SHOW_QUERY_PARAMS", "True").lower() == "true"
)
LOGGER_MIDDLEWARE_SHOW_BODY = (
    os.getenv("LOGGER_MIDDLEWARE_SHOW_BODY", "True").lower() == "true"
)
LOGGER_MIDDLEWARE_SHOW_PATH_PARAMS = (
    os.getenv("LOGGER_MIDDLEWARE_SHOW_PATH_PARAMS", "True").lower() == "true"
)
LOGGER_EXCEPTIONS_ENABLED = (
    os.getenv("LOGGER_EXCEPTIONS_ENABLED", "False").lower() == "true"
)
LOGGER_MIDDLEWARE_ERRORS_ONLY = (
    os.getenv("LOGGER_MIDDLEWARE_ERRORS_ONLY", "False").lower() == "true"
)

# ======= Docs variables ======= #
DOCS_ENABLED = os.getenv("DOCS_ENABLED", "True").lower() == "true"
DOCS_PASSWORD_ENABLED = os.getenv("DOCS_PASSWORD_ENABLED", "False").lower() == "true"
DOCS_USER = os.getenv("DOCS_USER", "admin")
DOCS_PASSWORD = os.getenv("DOCS_PASSWORD", "")

# ======= Rate limiting variables ======= #
RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "100/minute")
RATE_LIMIT_REDIS_ENABLED = os.getenv("RATE_LIMIT_REDIS_ENABLED", "False").lower() == "true"
RATE_LIMIT_REDIS_URL = os.getenv("RATE_LIMIT_REDIS_URL", "redis://localhost:6379")

# ======= Pagination variables ======= #
# Máximo de elementos por página. Hardcap en código: 200.
# Si PAGINATION_MAX_SIZE supera 200, se ignora y se usa 200.
PAGINATION_MAX_SIZE: int = min(int(os.getenv("PAGINATION_MAX_SIZE", "50")), 200)

# ======= Request size variables ======= #
REQUEST_MAX_SIZE_MB: float = float(os.getenv("REQUEST_MAX_SIZE_MB", "10"))

# ======= CORS variables ======= #
_cors_origins_raw = os.getenv("CORS_ORIGINS", "*")
CORS_ORIGINS: list[str] = [
    origin.strip() for origin in _cors_origins_raw.split(",") if origin.strip()
]

# ======= Database variables ======= #
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_USER = os.getenv("DB_USER", "username")
DB_PASS = os.getenv("DB_PASS", "password")
DB_NAME = os.getenv("DB_NAME", "database")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_ENGINE = os.getenv("DB_ENGINE", "sqlite")

# ======= Crypto variables ======= #
# Sal NO secreta usada para derivar la clave Fernet desde SECRET_KEY (HKDF).
# Cambiarla invalida todos los secretos ya cifrados.
CRYPTO_KEY_SALT = os.getenv("CRYPTO_KEY_SALT", "db-gateway-static-salt")

# ======= Remote server connection variables ======= #
# Timeout (segundos) para abrir conexión TCP a un servidor destino.
REMOTE_CONNECT_TIMEOUT = int(os.getenv("REMOTE_CONNECT_TIMEOUT", "10"))
# Timeout (milisegundos) de ejecución de una sentencia remota (DDL/DCL/introspección).
REMOTE_STATEMENT_TIMEOUT_MS = int(os.getenv("REMOTE_STATEMENT_TIMEOUT_MS", "15000"))
# Política TLS hacia los motores DESTINO (la credencial pseudo-root viaja por aquí).
# Vacío/None/"disable" => sin TLS (comportamiento histórico). Recomendado en producción:
#   - PostgreSQL: "require" | "verify-ca" | "verify-full" (psycopg lo aplica nativamente).
#   - MySQL/MariaDB: cualquier valor distinto de "disable" fuerza TLS cifrando el
#     transporte (sin verificación de CA todavía; ver docs/plans/00).
# Aplica como política GLOBAL a todos los servidores destino.
REMOTE_SSL_MODE = (os.getenv("REMOTE_SSL_MODE", "") or "").strip() or None

# ======= Snapshot selectivo: guardrails de datos-semilla ======= #
# El snapshot puede incluir OPCIONALMENTE datos de tablas de catálogo/tipo (opt-in por
# tabla) como INSERT idempotente. NO es una herramienta de ETL: estos topes protegen la
# BD de metadatos del gateway y su memoria. Hay TECHOS DUROS en código
# (app/services/db_admin/snapshot_data.py) que estas variables no pueden exceder.
SNAPSHOT_DATA_MAX_ROWS_PER_TABLE = int(os.getenv("SNAPSHOT_DATA_MAX_ROWS_PER_TABLE", "1000"))
SNAPSHOT_DATA_MAX_BYTES_PER_TABLE = int(
    os.getenv("SNAPSHOT_DATA_MAX_BYTES_PER_TABLE", str(1024 * 1024))  # 1 MB
)
SNAPSHOT_DATA_MAX_TABLES = int(os.getenv("SNAPSHOT_DATA_MAX_TABLES", "25"))
SNAPSHOT_DATA_BATCH_ROWS = int(os.getenv("SNAPSHOT_DATA_BATCH_ROWS", "500"))
# Tope de SQL por versión generada (estructura o datos). Distinto de _MAX_SQL (256 KB,
# solo creación manual); un snapshot legítimo puede ser mayor y la columna es LONGTEXT.
SNAPSHOT_MAX_SQL_PER_VERSION = int(
    os.getenv("SNAPSHOT_MAX_SQL_PER_VERSION", str(4 * 1024 * 1024))  # 4 MB
)

# ======= Diff estructural entre BDs (schema comparisons) ======= #
# Vida útil (horas) de una comparación persistida. Tras expirar, adopt/execute exigen
# recalcular: una comparación vieja describe un estado del motor que ya no existe.
SCHEMA_COMPARISON_TTL_HOURS = int(os.getenv("SCHEMA_COMPARISON_TTL_HOURS", "24"))
# Tope de sentencias por comparación. Un diff con miles de ítems suele indicar dos BDs
# no comparables (o drift masivo); se rechaza (422) para no materializar payloads enormes.
SCHEMA_COMPARISON_MAX_ITEMS = int(os.getenv("SCHEMA_COMPARISON_MAX_ITEMS", "2000"))
# Tope de bytes del DDL total renderizado de una comparación (protege memoria/BD del gateway).
SCHEMA_COMPARISON_MAX_SQL_BYTES = int(
    os.getenv("SCHEMA_COMPARISON_MAX_SQL_BYTES", str(8 * 1024 * 1024))  # 8 MB
)

# ======= Clonado de bases de datos (database clones) ======= #
# Vida útil (horas) de un plan de clonación. Tras expirar, execute exige replanear.
CLONE_TTL_HOURS = int(os.getenv("CLONE_TTL_HOURS", "24"))
# Workers del pool in-process que ejecutan los jobs de clonación en segundo plano.
# NO es una cola durable: si el proceso se reinicia, los jobs en curso quedan
# 'interrupted' (barrido en el lifespan) y se reintentan a mano.
CLONE_MAX_WORKERS = int(os.getenv("CLONE_MAX_WORKERS", "2"))
# Filas por lote en la copia de datos por streaming (lectura yield_per + escritura executemany).
CLONE_DATA_BATCH_ROWS = int(os.getenv("CLONE_DATA_BATCH_ROWS", "1000"))

# ======= Anti-SSRF (validación de host destino) ======= #
# Si True (default), al registrar/editar un Server se rechazan destinos peligrosos
# (loopback, link-local/metadata 169.254.169.254, multicast, reservados). Los rangos
# privados se permiten por defecto (las BD suelen ser internas).
REMOTE_SSRF_GUARD_ENABLED = os.getenv("REMOTE_SSRF_GUARD_ENABLED", "True").lower() == "true"
# Allowlist OPCIONAL de CIDRs. Si se define, el host destino DEBE resolver dentro de
# alguno (allowlist estricta). Vacío = sin allowlist (solo aplica la denylist de arriba).
# Ej: REMOTE_ALLOWED_CIDRS=10.0.0.0/8,192.168.0.0/16
_allowed_cidrs_raw = os.getenv("REMOTE_ALLOWED_CIDRS", "")
REMOTE_ALLOWED_CIDRS = [
    ipaddress.ip_network(c.strip(), strict=False)
    for c in _allowed_cidrs_raw.split(",")
    if c.strip()
]

# ======= Admin / Session variables ======= #
# Admin único que se siembra al arrancar si no existe ninguno en la BD.
ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD")
# Secreto para firmar la cookie de sesión. Si está vacío, se deriva de SECRET_KEY.
SESSION_SECRET = os.getenv("SESSION_SECRET") or SECRET_KEY or "insecure-dev-session-secret"
# Duración de la sesión en segundos (default 8 horas).
SESSION_MAX_AGE = int(os.getenv("SESSION_MAX_AGE", "28800"))

# ======= Startup validation ======= #
if not SECRET_KEY:
    if APP_ENV == "production":
        raise ValueError(
            "SECRET_KEY no está definido. "
            "Establece la variable de entorno SECRET_KEY antes de iniciar en producción."
        )
    import logging as _logging
    _logging.warning(
        "SECRET_KEY no está definido. Define SECRET_KEY en tu .env para evitar este aviso."
    )

if not ADMIN_PASSWORD and APP_ENV == "production":
    raise ValueError(
        "ADMIN_PASSWORD no está definido. "
        "Establece ADMIN_PASSWORD para sembrar el administrador antes de iniciar en producción."
    )

# En producción la firma de la cookie de sesión NO debe derivarse de SECRET_KEY: si una
# sola clave se filtra, comprometería sesión y cifrado a la vez. Exigimos un secreto
# de sesión independiente y explícito.
if APP_ENV == "production" and not os.getenv("SESSION_SECRET"):
    raise ValueError(
        "SESSION_SECRET no está definido. "
        "En producción SESSION_SECRET debe ser independiente de SECRET_KEY."
    )

# La autenticación es por cookie de sesión (allow_credentials=True). Con CORS_ORIGINS="*"
# el navegador rechaza enviar credenciales y reflejar el origin sería inseguro (CSRF
# asistido por CORS). En producción EXIGIMOS orígenes explícitos.
if APP_ENV == "production" and "*" in CORS_ORIGINS:
    raise ValueError(
        "CORS_ORIGINS no puede ser '*' en producción: la auth por cookie requiere una "
        "lista explícita de orígenes (p. ej. CORS_ORIGINS=https://panel.midominio.com)."
    )
