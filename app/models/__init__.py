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
from app.models.charset_collation_option import CharsetCollationOption
from app.models.clone_job import CloneJob, CloneJobItem
from app.models.collation_conversion_job import (
    CollationConversionJob,
    CollationConversionJobItem,
)
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
from app.models.migration_statement_progress import MigrationStatementProgress
from app.models.model_migration import ModelMigration
from app.models.model_migration_statement import ModelMigrationStatement
from app.models.permission_profile import PermissionProfile, PermissionProfileItem
from app.models.privilege import Privilege
from app.models.schema_comparison import SchemaComparison
from app.models.schema_comparison_item import SchemaComparisonItem
from app.models.query_execution import QueryExecution
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
    "MigrationStatementProgress",
    "ModelMigrationStatement",
    "AuditLog",
    "CryptoKey",
    "Privilege",
    "CharsetCollationOption",
    "PermissionProfile",
    "PermissionProfileItem",
    "SchemaComparison",
    "SchemaComparisonItem",
    "CloneJob",
    "CloneJobItem",
    "CollationConversionJob",
    "CollationConversionJobItem",
    "QueryExecution",
    "EngineType",
    "ServerStatus",
    "ProvisionStatus",
    "MigrationStatus",
]
