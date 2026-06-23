"""
Datos de llenado del catálogo `privileges`.

Construye una fila por (motor, privilegio). Dos grupos:

  - CONTROLADOS (is_active=True): derivados de ``privileges.controlled_tokens()``, de
    modo que lo ACTIVO en la tabla coincide SIEMPRE con lo validable en código (sin
    drift). La descripción/contexto vienen de los mapas de abajo.
  - NO CONTROLADOS (is_active=False, category='admin'): privilegios administrativos /
    globales que EXISTEN en el motor pero la plataforma no gestiona. Se siembran para
    que el catálogo sea honesto ("existen 30, controlamos 10"), pero no se exponen al
    pedir solo los activos.

El test de consistencia (tests/test_privilege_catalog.py) garantiza que todo token
controlado tenga descripción y aparezca como activo.
"""

from app.services.db_admin.privileges import controlled_tokens, token_is_sensitive

# (contexto, descripción) para los privilegios CONTROLADOS de MySQL/MariaDB.
_DESC_MYSQL: dict[str, tuple[str, str]] = {
    "SELECT": ("Tablas, columnas", "Leer filas de tablas o columnas"),
    "INSERT": ("Tablas, columnas", "Insertar filas"),
    "UPDATE": ("Tablas, columnas", "Actualizar filas existentes"),
    "DELETE": ("Tablas", "Eliminar filas"),
    "CREATE": ("Bases de datos, tablas", "Crear bases de datos y tablas"),
    "DROP": ("Bases de datos, tablas", "Eliminar bases de datos y tablas"),
    "ALTER": ("Tablas", "Modificar la estructura de tablas"),
    "INDEX": ("Tablas", "Crear o eliminar índices"),
    "REFERENCES": ("Tablas, columnas", "Crear claves foráneas hacia la tabla"),
    "CREATE VIEW": ("Vistas", "Crear o redefinir vistas"),
    "SHOW VIEW": ("Vistas", "Ver la definición de vistas (SHOW CREATE VIEW)"),
    "TRIGGER": ("Tablas", "Crear, eliminar y ejecutar triggers"),
    "CREATE ROUTINE": ("Bases de datos", "Crear procedimientos y funciones almacenadas"),
    "ALTER ROUTINE": ("Rutinas", "Modificar o eliminar rutinas almacenadas"),
    "EXECUTE": ("Rutinas", "Ejecutar procedimientos y funciones almacenadas"),
    "CREATE TEMPORARY TABLES": ("Bases de datos", "Crear tablas temporales"),
    "LOCK TABLES": ("Bases de datos", "Bloquear tablas con LOCK TABLES"),
    "EVENT": ("Bases de datos", "Crear y gestionar eventos del scheduler"),
    "DELETE HISTORY": ("Tablas", "Borrar historial de tablas versionadas por sistema"),
    "ALL PRIVILEGES": ("Todos", "Todos los privilegios del nivel (excluye GRANT OPTION)"),
    "GRANT OPTION": ("Delegación", "Otorgar a otros los privilegios que uno posee"),
}

# (contexto, descripción) para los privilegios CONTROLADOS de PostgreSQL.
_DESC_PG: dict[str, tuple[str, str]] = {
    "SELECT": ("Tablas, columnas, secuencias", "Leer datos de tablas, columnas o secuencias"),
    "INSERT": ("Tablas, columnas", "Insertar filas"),
    "UPDATE": ("Tablas, columnas, secuencias", "Actualizar datos"),
    "DELETE": ("Tablas", "Eliminar filas"),
    "TRUNCATE": ("Tablas", "Vaciar tablas con TRUNCATE"),
    "REFERENCES": ("Tablas, columnas", "Crear claves foráneas hacia la tabla"),
    "TRIGGER": ("Tablas", "Crear triggers sobre la tabla"),
    "CONNECT": ("Bases de datos", "Conectarse a la base de datos"),
    "CREATE": ("Bases de datos, esquemas", "Crear objetos en la base de datos o esquema"),
    "TEMPORARY": ("Bases de datos", "Crear tablas temporales"),
    "USAGE": ("Esquemas, secuencias", "Acceder al esquema o usar secuencias"),
    "EXECUTE": ("Funciones, procedimientos", "Ejecutar funciones y procedimientos"),
    "MAINTAIN": ("Tablas", "Mantenimiento: VACUUM, ANALYZE, REINDEX, CLUSTER, REFRESH (PG17)"),
    "ALL PRIVILEGES": ("Todos", "Todos los privilegios aplicables al objeto"),
}

# Privilegios NO controlados (is_active=False). (nombre, contexto, descripción).
_INACTIVE_MYSQL: list[tuple[str, str, str]] = [
    ("SUPER", "Servidor", "Operaciones administrativas del servidor (en desuso)"),
    ("FILE", "Servidor", "Leer/escribir archivos en el host del servidor"),
    ("PROCESS", "Servidor", "Ver los procesos de todos los usuarios"),
    ("RELOAD", "Servidor", "FLUSH y recarga de configuración"),
    ("SHUTDOWN", "Servidor", "Apagar el servidor"),
    ("CREATE USER", "Servidor", "Crear, renombrar y eliminar usuarios"),
    ("SHOW DATABASES", "Servidor", "Listar todas las bases de datos"),
    ("REPLICATION CLIENT", "Servidor", "Consultar el estado de replicación"),
    ("REPLICATION SLAVE", "Servidor", "Leer el binlog para replicación"),
    ("CREATE TABLESPACE", "Servidor", "Crear, alterar y eliminar tablespaces"),
    ("CREATE ROLE", "Servidor", "Crear roles"),
    ("DROP ROLE", "Servidor", "Eliminar roles"),
]

# MariaDB 11.x: privilegios administrativos del split de SUPER (no controlados).
_INACTIVE_MARIADB_EXTRA: list[tuple[str, str, str]] = [
    ("BINLOG ADMIN", "Servidor", "Administrar el binlog y purgarlo"),
    ("BINLOG MONITOR", "Servidor", "Monitorear el binlog (SHOW BINLOG, etc.)"),
    ("BINLOG REPLAY", "Servidor", "Reproducir eventos del binlog (BINLOG)"),
    ("CONNECTION ADMIN", "Servidor", "Saltar límites de conexión y matar conexiones"),
    ("FEDERATED ADMIN", "Servidor", "Administrar tablas FederatedX"),
    ("READ_ONLY ADMIN", "Servidor", "Ignorar read_only del servidor"),
    ("REPLICA MONITOR", "Servidor", "Monitorear réplicas (SHOW REPLICA STATUS)"),
    ("REPLICATION MASTER ADMIN", "Servidor", "Administrar la configuración de primario"),
    ("REPLICATION SLAVE ADMIN", "Servidor", "Administrar la réplica (START/STOP REPLICA)"),
    ("SET USER", "Servidor", "Definir un usuario distinto en DEFINER"),
    ("SLAVE MONITOR", "Servidor", "Monitorear el estado de la réplica (heredado)"),
]

_INACTIVE_PG: list[tuple[str, str, str]] = [
    ("SUPERUSER", "Atributo de rol", "Superusuario: omite todas las comprobaciones"),
    ("CREATEROLE", "Atributo de rol", "Crear, alterar y eliminar roles"),
    ("CREATEDB", "Atributo de rol", "Crear bases de datos"),
    ("REPLICATION", "Atributo de rol", "Iniciar conexiones de replicación"),
    ("BYPASSRLS", "Atributo de rol", "Omitir las políticas de Row-Level Security"),
    ("SET", "Parámetros", "Cambiar parámetros de configuración (GRANT SET ON PARAMETER)"),
    ("ALTER SYSTEM", "Parámetros", "Modificar parámetros con ALTER SYSTEM"),
]


def _active_rows(engine: str, desc: dict[str, tuple[str, str]]) -> list[dict]:
    rows = []
    for name in sorted(controlled_tokens(engine)):
        context, description = desc.get(name, ("", name))
        rows.append(
            {
                "engine": engine,
                "name": name,
                "category": "object",
                "context": context,
                "description": description,
                "is_sensitive": token_is_sensitive(engine, name),
                "is_active": True,
            }
        )
    return rows


def _inactive_rows(engine: str, entries: list[tuple[str, str, str]]) -> list[dict]:
    return [
        {
            "engine": engine,
            "name": name,
            "category": "admin",
            "context": context,
            "description": description,
            "is_sensitive": False,
            "is_active": False,
        }
        for name, context, description in entries
    ]


def privilege_seed_rows() -> list[dict]:
    """Todas las filas de catálogo a sembrar (los 3 motores)."""
    rows: list[dict] = []
    rows += _active_rows("mysql", _DESC_MYSQL)
    rows += _inactive_rows("mysql", _INACTIVE_MYSQL)
    rows += _active_rows("mariadb", _DESC_MYSQL)
    rows += _inactive_rows("mariadb", _INACTIVE_MYSQL + _INACTIVE_MARIADB_EXTRA)
    rows += _active_rows("postgresql", _DESC_PG)
    rows += _inactive_rows("postgresql", _INACTIVE_PG)
    return rows
