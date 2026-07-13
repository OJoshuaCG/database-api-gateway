from fastapi import APIRouter

from app.routes.v1 import (
    auth,
    crypto,
    database_models,
    managed_databases,
    model_migrations,
    permission_profiles,
    privileges,
    schema_comparisons,
    server_users,
    servers,
    test,
)

router = APIRouter()

router.include_router(auth.router)
router.include_router(servers.router)
router.include_router(server_users.router)
router.include_router(database_models.router)
router.include_router(model_migrations.router)
router.include_router(managed_databases.router)
router.include_router(schema_comparisons.router)
router.include_router(privileges.router)
router.include_router(permission_profiles.router)
router.include_router(crypto.router)
router.include_router(test.router)
