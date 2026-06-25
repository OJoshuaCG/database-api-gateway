"""
Modelos ORM del proyecto.

IMPORTANTE: Todos los modelos deben ser importados aquí para que Alembic
los detecte durante la generación automática de migraciones (autogenerate).

Al crear un nuevo modelo:
1. Crear el archivo en app/models/
2. Heredar de Base y opcionalmente TimestampMixin
3. Importar el modelo aquí
4. Agregarlo a __all__

Ejemplo:
    from app.models.new_model import NewModel
    __all__ = [..., "NewModel"]
"""

from app.models.audit_log import AuditLog
from app.models.base import Base, TimestampMixin
from app.models.crypto_key import CryptoKey
from app.models.database_migration_history import DatabaseMigrationHistory
from app.models.database_model import DatabaseModel
from app.models.enums import (
    EngineType,
    MigrationStatus,
    ProvisionStatus,
    ServerStatus,
)
from app.models.managed_database import ManagedDatabase
from app.models.model_migration import ModelMigration
from app.models.permission_profile import PermissionProfile, PermissionProfileItem
from app.models.privilege import Privilege
from app.models.server import Server
from app.models.server_user import ServerUser
from app.models.user import User

__all__ = [
    "Base",
    "TimestampMixin",
    "User",
    "Server",
    "ServerUser",
    "DatabaseModel",
    "ManagedDatabase",
    "ModelMigration",
    "DatabaseMigrationHistory",
    "AuditLog",
    "CryptoKey",
    "Privilege",
    "PermissionProfile",
    "PermissionProfileItem",
    "EngineType",
    "ServerStatus",
    "ProvisionStatus",
    "MigrationStatus",
]
