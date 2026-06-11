"""
Administración de servidores de base de datos DESTINO.

Expone un `ServerAdapter` por dialecto (MySQL/MariaDB, PostgreSQL) que encapsula
las diferencias de gestión de usuarios, bases de datos, permisos e introspección
de estructura. Punto de entrada único: `get_adapter(target)`.
"""

from app.services.db_admin.base_adapter import ServerAdapter
from app.services.db_admin.factory import get_adapter

__all__ = ["ServerAdapter", "get_adapter"]
