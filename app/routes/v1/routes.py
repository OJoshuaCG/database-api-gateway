from fastapi import APIRouter

from app.routes.v1 import auth, servers, test

router = APIRouter()

router.include_router(auth.router)
router.include_router(servers.router)
router.include_router(test.router)
