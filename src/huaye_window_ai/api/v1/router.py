"""业务路由聚合；统一前缀由应用工厂依据配置挂载。"""

from fastapi import APIRouter

from huaye_window_ai.api.v1.routes.auth import router as auth_router
from huaye_window_ai.api.v1.routes.catalog import router as catalog_router
from huaye_window_ai.api.v1.routes.photos import router as photos_router
from huaye_window_ai.api.v1.routes.render import router as render_router

router = APIRouter()
router.include_router(auth_router)
router.include_router(catalog_router)
router.include_router(photos_router)
router.include_router(render_router)
