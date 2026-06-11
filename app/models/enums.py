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
