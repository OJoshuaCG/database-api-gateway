"""Enums compartidos por los modelos del inventario del gateway."""

import enum


class EngineType(str, enum.Enum):
    """Motor de base de datos de un servidor destino."""

    mysql = "mysql"
    mariadb = "mariadb"
    postgresql = "postgresql"


class ServerStatus(str, enum.Enum):
    """Estado operativo de un servidor destino en el inventario."""

    active = "active"
    inactive = "inactive"
    unreachable = "unreachable"


class ProvisionStatus(str, enum.Enum):
    """
    Consistencia entre el inventario del gateway y el motor real (BDs gestionadas).

    El flujo de aprovisionamiento inserta en estado ``pending``, ejecuta el DDL/DCL
    remoto y pasa a ``active`` (éxito) o ``error`` (falla, con detalle en notas; el
    registro se conserva para auditoría/reintento, sin rollback silencioso).
    """

    pending = "pending"    # registrada en el inventario, aún no creada en el motor
    active = "active"      # creada/aprovisionada correctamente en el motor
    error = "error"        # la operación remota falló (ver notas)
    archived = "archived"  # retirada del uso sin borrarse del inventario


class MigrationStatus(str, enum.Enum):
    """
    Resultado de aplicar/revertir una migración de blueprint sobre una BD gestionada.

    Se registra en ``database_migration_history`` (espejo de auditoría del gateway).
    El estado de versión REAL de la BD destino lo mantiene Alembic en su tabla
    ``_gw_v_{slug}`` dentro de la propia BD gestionada; este enum solo describe el
    desenlace de cada intento.
    """

    applied = "applied"  # la migración se aplicó/revirtió correctamente
    failed = "failed"    # la migración falló (ver ``error``); BD posiblemente sucia


class MigrationKind(str, enum.Enum):
    """
    Naturaleza del contenido de una migración de blueprint.

    - ``schema``: DDL (tablas, vistas, rutinas, triggers…). Es el caso por defecto y
      cubre toda migración escrita a mano o generada por snapshot estructural.
    - ``data``: DML de datos-semilla (catálogos/tipos) generado desde un snapshot con
      INSERT idempotente (upsert). Se distingue del esquema porque (a) la UI lo muestra
      aparte, (b) su rollback se genera por PK (DELETE), y (c) está ATADO al motor de
      origen (la sintaxis upsert difiere entre MySQL y PostgreSQL): no se traduce
      cross-engine.
    """

    schema = "schema"
    data = "data"
