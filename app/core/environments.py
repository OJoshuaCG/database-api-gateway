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

# La autenticación es por cookie de sesión (allow_credentials=True). Con CORS_ORIGINS="*"
# el navegador rechaza enviar credenciales y reflejar el origin sería inseguro (CSRF
# asistido por CORS). En producción EXIGIMOS orígenes explícitos.
if APP_ENV == "production" and "*" in CORS_ORIGINS:
    raise ValueError(
        "CORS_ORIGINS no puede ser '*' en producción: la auth por cookie requiere una "
        "lista explícita de orígenes (p. ej. CORS_ORIGINS=https://panel.midominio.com)."
    )
