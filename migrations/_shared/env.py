"""
env.py compartido para las migraciones de BLUEPRINTS sobre las BDs gestionadas.

A diferencia de ``alembic/env.py`` (que migra la BD de metadatos del gateway), este
env.py NO construye un engine ni lee variables de entorno: recibe una conexión YA
abierta inyectada por el ``MigrationRunner`` vía ``config.attributes['connection']``,
junto con el nombre de la tabla de versión por blueprint
(``config.attributes['version_table']`` → ``_gw_v_{slug}``).

Esto permite aplicar la MISMA secuencia de revisiones sobre N conexiones distintas
(multi-tenant): cada llamada del runner reutiliza este env.py apuntando a otra BD.

``target_metadata = None``: las migraciones son SQL crudo (``op.execute``); jamás se
usa autogenerate en runtime contra una BD gestionada.
"""

from alembic import context

config = context.config
target_metadata = None


def run_migrations_online() -> None:
    connection = config.attributes.get("connection")
    if connection is None:
        raise RuntimeError(
            "MigrationRunner debe inyectar 'connection' en config.attributes."
        )
    version_table = config.attributes.get("version_table", "alembic_version")

    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table=version_table,
        transaction_per_migration=True,
    )

    # El MigrationRunner inyecta la conexión en modo AUTOCOMMIT: cada sentencia (DDL y
    # la escritura de la tabla de versión) commitea al instante en MySQL y PostgreSQL.
    # Es necesario porque el advisory lock por BD abre una transacción de sesión que,
    # de otro modo, envolvería las migraciones sin commitearse. Consecuencia documentada
    # (ver docs/plans/02): una migración multi-sentencia que falle a mitad deja estado
    # parcial; por eso las migraciones deben ser idempotentes.
    with context.begin_transaction():
        context.run_migrations()


# El runner siempre opera en modo online (conexión inyectada). El modo offline
# (generar SQL sin conectarse, `--sql`) queda disponible para uso futuro.
if context.is_offline_mode():
    raise RuntimeError(
        "El runner de blueprints no soporta modo offline en esta versión."
    )
run_migrations_online()
