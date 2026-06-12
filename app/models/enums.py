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
