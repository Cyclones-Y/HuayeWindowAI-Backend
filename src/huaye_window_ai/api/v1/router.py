"""v1 路由聚合。"""

from fastapi import APIRouter

from huaye_window_ai.api.v1.routes.auth import router as auth_router

router = APIRouter(prefix="/v1")
router.include_router(auth_router)
