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
# Timeout (milisegundos) para operaciones de VOLCADO MASIVO (copia de datos del clon):
# el timeout interactivo de 15s es demasiado corto para insertar/leer lotes de tablas
# grandes → las cancelaría y dejaría datos parciales. Default 1 hora. ``0`` = sin límite
# (útil para tablas enormes, pero un clon colgado nunca se autocancela).
REMOTE_BULK_STATEMENT_TIMEOUT_MS = int(os.getenv("REMOTE_BULK_STATEMENT_TIMEOUT_MS", "3600000"))
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

# ¿La fase de datos del clon lee las N tablas de UNA sola foto del origen?
#
# Sin esto cada tabla abre su propio read view, o sea N fotos distintas: si el origen inserta un
# padre y su hijo entre que se copia `padre` y se copia `hijo`, el destino queda con un huérfano
# —y como el clon apaga las FKs del destino y nunca las revalida, lo reporta como exitoso.
# Reproducido contra MySQL 8.0.
#
# El costo, que hay que conocer antes de apagarlo o dejarlo: sostener el read view impide el
# purge del undo log en el ORIGEN —producción de un tercero— y retiene MDL compartido sobre las
# tablas leídas, o sea bloquea su DDL mientras dura el clon. Es lo mismo que hace
# `mysqldump --single-transaction`. Para clones de segundos o minutos no se nota; sobre un origen
# muy escrito y un clon de horas, el tablespace del cliente crece.
CLONE_CONSISTENT_SNAPSHOT = os.getenv("CLONE_CONSISTENT_SNAPSHOT", "True").lower() != "false"
# Kill switch de la copia BULK NATIVA (COPY FROM STDIN en PostgreSQL, LOAD DATA LOCAL
# INFILE en MySQL/MariaDB). True (default) usa el protocolo bulk del motor; False vuelve
# al INSERT parametrizado por lotes (executemany) sin necesidad de re-desplegar código.
CLONE_BULK_COPY_ENABLED = os.getenv("CLONE_BULK_COPY_ENABLED", "True").lower() == "true"
# Cuántos LOTES de clonación corren a la vez. Las filas DENTRO de un lote van siempre en
# serie, y eso no es configurable: el paralelismo entre bases multiplicaría el daño de un
# error y la carga sobre el servidor destino sin un beneficio medido. Executor PROPIO, no el
# de CLONE_MAX_WORKERS, para que un lote largo no deje sin turno a los clones sueltos.
CLONE_BATCH_MAX_WORKERS = int(os.getenv("CLONE_BATCH_MAX_WORKERS", "1"))
# Cuántas BASES DE DATOS puede pedir un lote. Es un límite de prudencia, no técnico: el costo
# de planear no crece con este número (la lista de bases se consulta una vez por servidor, no
# por fila), así que lo que acota es cuánto puede autorizar UNA sola confirmación sobre bases
# de terceros. No confundir con CLONE_DATA_BATCH_ROWS, que sí cuenta filas de datos.
CLONE_BATCH_MAX_DATABASES = int(os.getenv("CLONE_BATCH_MAX_DATABASES", "25"))

# ======= Conversión de charset/collation (collation conversions) ======= #
# Vida útil (horas) de un plan de conversión. Tras expirar, execute exige replanear.
COLLATION_CONVERSION_TTL_HOURS = int(os.getenv("COLLATION_CONVERSION_TTL_HOURS", "24"))
# Workers del pool in-process que ejecutan las conversiones en segundo plano. Igual que el
# clon, NO es una cola durable: si el proceso se reinicia, los jobs en curso quedan
# 'interrupted' (barrido en el lifespan) y se reintentan a mano. Default 1 a propósito: un
# ``ALTER TABLE ... CONVERT TO CHARACTER SET`` reescribe la tabla completa y varios en
# paralelo saturan el I/O del servidor destino.
COLLATION_CONVERSION_MAX_WORKERS = int(os.getenv("COLLATION_CONVERSION_MAX_WORKERS", "1"))

# ======= Exportación de bases de datos (database exports) ======= #
# KILL SWITCH global del módulo. False = ningún endpoint de exportación funciona (409),
# ni siquiera planear. Existe porque una exportación es, por definición, una EXTRACCIÓN
# masiva de datos en claro (no hay enmascarado, ver §9.6 del diseño): si el gateway pasa a
# tratar datos regulados hay que poder apagar la vía de salida sin re-desplegar código.
EXPORT_ENABLED = os.getenv("EXPORT_ENABLED", "True").lower() == "true"
# Vida útil (horas) de un PLAN de exportación. Tras expirar, preview/execute exigen
# replanear (410): un plan viejo describe un catálogo del motor que ya no existe, y su
# ``source_fingerprint`` dejaría de ser una defensa anti-TOCTOU real.
EXPORT_TTL_HOURS = int(os.getenv("EXPORT_TTL_HOURS", "24"))
# Tope de bytes de la entrega EN LÍNEA (``output.delivery='inline'``: texto plano para
# copiar al portapapeles). Protege la MEMORIA DEL GATEWAY y la del navegador: el modo
# inline no es un flujo, se materializa entero. El preview publica
# ``inline_delivery_viable`` contra este valor para que el cliente lo sepa ANTES de lanzar
# el job; nunca se trunca en silencio (un script truncado que alguien pega y ejecuta es
# peor que un fallo).
EXPORT_INLINE_MAX_BYTES = int(os.getenv("EXPORT_INLINE_MAX_BYTES", str(1024 * 1024)))
# Techo del corte de una sentencia de datos (``data.max_statement_bytes``). El corte real
# se manda por BYTES y no por filas porque una tabla con LONGTEXT revienta cualquier
# límite basado en conteo. Un spec que pida más que esto se recorta a este valor: un
# INSERT gigante supera el ``max_allowed_packet`` del destino y el artefacto no se puede
# ejecutar.
EXPORT_MAX_STATEMENT_BYTES = int(
    os.getenv("EXPORT_MAX_STATEMENT_BYTES", str(1024 * 1024))
)
# Filas por sentencia de datos por DEFECTO (``data.rows_per_statement``). Es un techo
# SUPERIOR, no el corte real (manda ``max_statement_bytes``): sirve para que un INSERT no
# tenga miles de tuplas aunque cada fila sea diminuta.
EXPORT_ROWS_PER_STATEMENT = int(os.getenv("EXPORT_ROWS_PER_STATEMENT", "200"))
# Retención (minutos) del ARTEFACTO una vez que el job termina. El artefacto es un objeto
# sensible en reposo (§9.3): "no se conserva" (§19.7 del prompt) se implementa como TTL
# corto + purga garantizada. Lo CONSUME el almacenamiento de F4; se publica ya en
# /export-capabilities para que el cliente no adivine cuánto tiempo tiene para descargar.
EXPORT_ARTIFACT_TTL_MINUTES = int(os.getenv("EXPORT_ARTIFACT_TTL_MINUTES", "30"))
# Duración máxima (segundos) de una corrida antes del aborto duro. Una exportación
# sostiene una transacción de lectura larga contra el ORIGEN: en PostgreSQL bloquea el
# VACUUM y en la familia MySQL infla el historial de undo, así que una exportación de
# horas DEGRADA el servidor de un tercero. Lo consume el runner de F4; se publica acá por
# el mismo motivo que el TTL del artefacto.
EXPORT_MAX_DURATION_SECONDS = int(os.getenv("EXPORT_MAX_DURATION_SECONDS", "14400"))
# Timeout de SENTENCIA (ms) de la conexión dedicada de una exportación. Se pasa EXPLÍCITO a
# ``database_connection`` en vez de heredar el par interactivo/bulk: el interactivo (15s)
# cancelaría el SELECT de una tabla grande a mitad, y el bulk (1 h) es un techo pensado para
# la copia de datos del clon, no para una lectura que además sostiene un snapshot. ``0`` =
# sin límite (desaconsejado: una consulta colgada mantiene viva la transacción de lectura).
EXPORT_STATEMENT_TIMEOUT_MS = int(
    os.getenv("EXPORT_STATEMENT_TIMEOUT_MS", str(30 * 60 * 1000))
)
# Timeout de TRANSACCIÓN OCIOSA (ms) — solo PostgreSQL. ``remote_engine`` ata
# ``idle_in_transaction_session_timeout`` al ``statement_timeout`` en los ``connect_args``
# del engine, así que sin esto un export estancado sostendría el snapshot de PG (y bloquearía
# el VACUUM del origen) todo lo que dure el timeout de sentencia. ``export_session`` lo
# DESACOPLA con un ``SET`` a nivel de sesión sobre su propia conexión — que gana sobre el
# ``-c`` de la URL— para no cambiar la clave de cache de engines ni el comportamiento de los
# otros consumidores de ``remote_engine``. ``0`` = sin límite.
EXPORT_IDLE_TRANSACTION_TIMEOUT_MS = int(
    os.getenv("EXPORT_IDLE_TRANSACTION_TIMEOUT_MS", str(5 * 60 * 1000))
)
# Filas por lote del cursor en streaming (``yield_per``) al leer el ORIGEN. Acota la memoria
# del gateway: el writer nunca materializa una tabla entera (§8.1). Subirlo reduce viajes de
# red a costa de RAM por lote; con columnas LONGTEXT/BLOB conviene bajarlo.
EXPORT_BATCH_ROWS = int(os.getenv("EXPORT_BATCH_ROWS", "1000"))
# Workers del pool in-process que generan los artefactos. Default 1 A PROPÓSITO: una
# exportación LEE LA BASE ENTERA del origen y sostiene una transacción larga; varias en
# paralelo saturan el I/O del servidor de un tercero. Igual que el clon y la conversión de
# collation, NO es una cola durable: si el proceso se reinicia, los jobs en curso quedan
# 'interrupted' (barrido en el lifespan) y se relanzan a mano.
EXPORT_MAX_WORKERS = int(os.getenv("EXPORT_MAX_WORKERS", "1"))
# Techo de exportaciones EN EJECUCIÓN a la vez (§9.7). Con un solo worker la cola ya
# serializa, pero sin este tope un cliente puede encolar cientos de jobs y dejar al origen
# leyéndose durante días: es un vector de degradación y de exfiltración lenta.
EXPORT_MAX_CONCURRENT_GLOBAL = int(os.getenv("EXPORT_MAX_CONCURRENT_GLOBAL", "2"))
# Directorio de SPOOL del artefacto. Se crea con modo 0700 (§9.3): el archivo contiene los
# datos del origen EN CLARO —no hay enmascarado (§9.6)— así que es un objeto sensible en
# reposo y no puede quedar legible para otros usuarios del contenedor. Debe ser un volumen
# propio (``exports_data:/app/exports``), NO el de uploads: aquel es un buzón de entrada sin
# TTL y mezclar ambos ciclos de vida es cómo un artefacto sobrevive a su purga.
EXPORT_ARTIFACT_DIR = os.getenv("EXPORT_ARTIFACT_DIR", "/app/exports")
# Espacio libre MÍNIMO (bytes) que debe quedar en el disco del spool DESPUÉS de la
# estimación del preview. Llenar el disco del gateway no degrada la exportación: tumba el
# gateway entero (y con él la BD de metadatos si comparten volumen). ``0`` desactiva el
# chequeo (desaconsejado).
EXPORT_DISK_MIN_FREE_BYTES = int(
    os.getenv("EXPORT_DISK_MIN_FREE_BYTES", str(512 * 1024 * 1024))
)
# Tope DURO del tamaño del artefacto (§9.7). Al superarlo la corrida aborta y el archivo
# parcial se borra: es preferible un job fallido con un motivo claro a un disco lleno.
# ``0`` = sin tope.
EXPORT_ARTIFACT_MAX_BYTES = int(
    os.getenv("EXPORT_ARTIFACT_MAX_BYTES", str(5 * 1024 * 1024 * 1024))
)
# Descarga de UN SOLO USO (§10.1): al completarse la entrega el archivo se borra y el
# artefacto pasa a ``consumed``. Es la implementación de "el artefacto no se conserva"
# (§19.7) que no depende de que el TTL llegue a tiempo. ``False`` permite re-descargar
# hasta que venza el TTL (útil en desarrollo; en producción alarga la exposición).
EXPORT_SINGLE_USE_DOWNLOAD = (
    os.getenv("EXPORT_SINGLE_USE_DOWNLOAD", "True").lower() == "true"
)
# Cada cuántos minutos corre la purga de artefactos vencidos. Hacerlo SOLO en el arranque
# volvería el TTL una promesa falsa en un proceso que corre semanas — el mismo error que ya
# se corrigió en la purga de capturas de SELECT. ``0`` desactiva la tarea periódica (la del
# arranque sigue corriendo).
EXPORT_PURGE_INTERVAL_MINUTES = int(os.getenv("EXPORT_PURGE_INTERVAL_MINUTES", "10"))
# Tope de archivos DENTRO de un artefacto (un archivo por objeto + fragmentos de
# ``output.split_max_bytes``). El tope por tamaño no alcanza: con un ``split_max_bytes`` de
# 1 KB, un artefacto perfectamente legal genera decenas de miles de entradas en el zip —
# lento de escribir, inmanejable de descomprimir y con el directorio central creciendo en
# memoria. Al superarlo el job falla con un motivo accionable.
EXPORT_MAX_PARTS = int(os.getenv("EXPORT_MAX_PARTS", "500"))

# ======= Consola SQL (ejecución de queries ad-hoc) ======= #
# MODO SEGURO. True (default) = toda sentencia que no sea lectura pura exige el ciclo
# preview→confirmación (token firmado + nombre de la BD), y la lista de sentencias
# PROHIBIDAS (DCL, acceso a archivos del host, estado global del servidor, tablas
# internas del gateway) se rechaza incluso confirmando. Ponerlo en False NO desactiva
# los bloqueos: solo salta la confirmación de write/DDL. Pensado para el día que exista
# un segundo factor; hoy debe quedar en True.
QUERY_SAFE_MODE = os.getenv("QUERY_SAFE_MODE", "True").lower() == "true"
# Tope de filas devueltas por sentencia. Protege la MEMORIA DEL GATEWAY: un
# ``SELECT * FROM tabla_de_50M`` sin tope se materializa entero y se serializa a JSON.
# Al superarlo, la respuesta viene con truncated=true.
QUERY_MAX_ROWS = int(os.getenv("QUERY_MAX_ROWS", "1000"))
# Timeout por sentencia (ms). El interactivo general (REMOTE_STATEMENT_TIMEOUT_MS, 15s)
# es corto para una consola; este lo reemplaza solo en este camino.
QUERY_TIMEOUT_MS = int(os.getenv("QUERY_TIMEOUT_MS", "30000"))
# Techo del timeout que un request puede pedir. Evita que la consola sea una vía para
# dejar consultas colgadas indefinidamente en un servidor de producción.
QUERY_MAX_TIMEOUT_MS = int(os.getenv("QUERY_MAX_TIMEOUT_MS", "300000"))
# Tope de tamaño del SQL aceptado por request (bytes).
QUERY_MAX_SQL_BYTES = int(os.getenv("QUERY_MAX_SQL_BYTES", str(256 * 1024)))
# Tope de caracteres por CELDA devuelta. Un BLOB/TEXT grande se recorta con marca.
QUERY_MAX_CELL_CHARS = int(os.getenv("QUERY_MAX_CELL_CHARS", "4096"))
# Tope de caracteres del SQL que se PERSISTE en el historial (con contraseñas redactadas).
QUERY_HISTORY_SQL_MAX_CHARS = int(os.getenv("QUERY_HISTORY_SQL_MAX_CHARS", "16384"))

# ======= Captura de resultados de SELECT en migraciones de blueprint ======= #
# KILL SWITCH global. False = ninguna migración captura resultados, ni con
# capture_selects=true en la versión (el SQL se ejecuta exactamente igual que hoy: la
# captura nunca cambia lo que corre en el motor). Es la salida de emergencia si esta
# feature —la única que persiste datos de negocio en el gateway— hay que apagarla sin
# tocar cada blueprint.
MIGRATION_CAPTURE_ENABLED = (
    os.getenv("MIGRATION_CAPTURE_ENABLED", "True").lower() == "true"
)
# Tope de filas capturadas por sentencia. El recorte es SOLO de la captura: el SQL que se
# ejecuta jamás se reescribe (a diferencia de la consola SQL, que empuja un LIMIT al
# motor) — el texto tiene que coincidir byte a byte con el del checksum de la migración.
MIGRATION_CAPTURE_MAX_ROWS = int(os.getenv("MIGRATION_CAPTURE_MAX_ROWS", "100"))
# Tope de caracteres por CELDA capturada (un BLOB/TEXT grande se recorta con marca).
MIGRATION_CAPTURE_MAX_CELL_CHARS = int(
    os.getenv("MIGRATION_CAPTURE_MAX_CELL_CHARS", "1024")
)
# Tope de bytes del JSON en claro de UNA captura (protege memoria del proceso y la BD del
# gateway; el cifrado Fernet + base64 infla el valor almacenado ~1.4×).
MIGRATION_CAPTURE_MAX_BYTES = int(
    os.getenv("MIGRATION_CAPTURE_MAX_BYTES", str(256 * 1024))
)
# Tope de caracteres del SQL que se guarda junto a la captura (con contraseñas redactadas).
MIGRATION_CAPTURE_SQL_MAX_CHARS = int(
    os.getenv("MIGRATION_CAPTURE_SQL_MAX_CHARS", "4096")
)
# Retención (horas) de las capturas. Son datos de negocio: no se guardan indefinidamente.
# La purga corre en el arranque (lifespan), se REPITE periódicamente mientras el proceso vive
# (ver MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES) y también puede forzarse por endpoint.
MIGRATION_CAPTURE_TTL_HOURS = int(os.getenv("MIGRATION_CAPTURE_TTL_HOURS", "168"))
# Cada cuánto se repite la purga por TTL mientras el proceso corre. Solo con la purga del
# arranque, un gateway que vive semanas (lo normal) NUNCA volvía a purgar: el TTL era una
# promesa falsa en producción y las capturas se acumulaban indefinidamente. `0` desactiva la
# tarea periódica (queda solo la del arranque + la purga manual por endpoint).
MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES = int(
    os.getenv("MIGRATION_CAPTURE_PURGE_INTERVAL_MINUTES", "60")
)

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
# Flag `Secure` de la cookie de sesión. Por defecto sigue a APP_ENV=="production"
# (comportamiento histórico). Se puede fijar explícitamente (True/False) para
# desacoplarlo de APP_ENV, p. ej. mientras se termina de configurar TLS delante del
# gateway: NO desactivar en un despliegue real, la cookie viajaría sin cifrar.
_session_cookie_secure_raw = os.getenv("SESSION_COOKIE_SECURE", None)
SESSION_COOKIE_SECURE = (
    APP_ENV == "production"
    if _session_cookie_secure_raw is None
    else _session_cookie_secure_raw.lower() == "true"
)

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

if APP_ENV == "production" and not SESSION_COOKIE_SECURE:
    import logging as _logging
    _logging.warning(
        "SESSION_COOKIE_SECURE=False en producción: la cookie de sesión viaja SIN el "
        "flag Secure y el navegador la acepta por HTTP sin cifrar. Usar solo mientras "
        "se termina de configurar TLS delante del gateway (ver docs/dokploy-deployment.md)."
    )
