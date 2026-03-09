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

# ======= Docs variables ======= #
DOCS_ENABLED = os.getenv("DOCS_ENABLED", "True").lower() == "true"

# ======= Rate limiting variables ======= #
RATE_LIMIT_DEFAULT = os.getenv("RATE_LIMIT_DEFAULT", "100/minute")

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
