"""无需登录的门窗方案与样例读取接口；路由前缀由应用工厂配置。"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from urllib.parse import quote

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from huaye_window_ai.core.errors import AppError
from huaye_window_ai.core.responses import ApiResponse
from huaye_window_ai.db.session import get_db_session
from huaye_window_ai.integrations.oss_storage import catalog_object_key
from huaye_window_ai.integrations.photo_storage import PhotoStorage
from huaye_window_ai.repositories.catalog import CatalogRepository
from huaye_window_ai.schemas.catalog import (
    FrameColor,
    GlassOption,
    HomeDisplayAssets,
    NeedType,
    NormalizedSelection,
    RoomType,
    SampleSceneData,
    SampleSceneList,
    SolutionDetail,
    SolutionPage,
    SolutionSummary,
)

router = APIRouter(tags=["方案目录"])


async def _asset_url(request: Request, path: str, *, expires_at: datetime | None = None) -> str:
    """OSS 目录只签发运营配置的 catalog 对象；本地/CDN 模式保留原资源地址。"""

    settings = request.app.state.settings
    if settings.photo_storage_backend == "oss":
        try:
            key = catalog_object_key(settings.catalog_oss_prefix, path)
        except ValueError:
            raise AppError("CATALOG_ASSET_INVALID", "方案图片配置无效", status_code=503) from None
        storage = cast(PhotoStorage, request.app.state.photo_storage)
        # 仅目录素材免登录；用户照片不会通过本路由签名，也不能提交任意路径请求签名。
        return await storage.signed_url(
            key, "", expires_at or datetime.now(UTC) + timedelta(seconds=settings.photo_preview_ttl_seconds)
        )
    base_url: str = settings.catalog_public_base_url
    return f"{base_url.rstrip('/')}/{quote(path.lstrip('/'), safe='/')}"


@router.get("/display-assets/home", response_model=ApiResponse[HomeDisplayAssets])
async def get_home_display_assets(
    request: Request, session: AsyncSession = Depends(get_db_session)
) -> ApiResponse[HomeDisplayAssets]:
    """首页、登录和拍摄示意复用公开目录素材；仅签发数据库登记且上架的固定一对图片。"""
    paths = await CatalogRepository(session).home_display_assets()
    if set(paths) != {"home-before", "home-after"}:
        raise AppError("DISPLAY_ASSET_UNAVAILABLE", "首页图片暂不可用，请稍后重试", status_code=503)
    expires_at = datetime.now(UTC) + timedelta(seconds=request.app.state.settings.photo_preview_ttl_seconds)
    return ApiResponse(
        data=HomeDisplayAssets(
            before_url=await _asset_url(request, paths["home-before"], expires_at=expires_at),
            after_url=await _asset_url(request, paths["home-after"], expires_at=expires_at),
            expires_at=expires_at,
        ),
        request_id=request.state.request_id,
    )


@router.get("/solutions", response_model=ApiResponse[SolutionPage])
async def list_solutions(
    request: Request,
    room: RoomType | None = None,
    need: NeedType | None = None,
    keyword: str | None = Query(default=None, max_length=120),
    page: int = Query(default=1, ge=1),
    size: int = Query(default=20, ge=1, le=50),
    session: AsyncSession = Depends(get_db_session),
) -> ApiResponse[SolutionPage]:
    """返回按空间、需求、关键词交集过滤的可用方案，不接收任意排序字段。"""

    solutions, total = await CatalogRepository(session).list_solutions(
        room=room, need=need, keyword=keyword, page=page, size=size
    )
    items = [
        SolutionSummary(
            id=row.id,
            name=row.name,
            subtitle=row.subtitle,
            thumbnail_url=await _asset_url(request, row.thumbnail_path),
            room=RoomType(row.room),
            needs=[NeedType(value) for value in row.needs],
            features=row.features,
            pricing_note=row.pricing_note,
            available=row.available,
        )
        for row in solutions
    ]
    return ApiResponse(
        data=SolutionPage(items=items, page=page, size=size, total=total, has_next=page * size < total),
        request_id=request.state.request_id,
    )


@router.get("/solutions/{solutionId}", response_model=ApiResponse[SolutionDetail])
async def get_solution(
    solutionId: str,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ApiResponse[SolutionDetail]:
    """保留颜色、玻璃的可用状态和默认代码，供前端展示和后续生成参数校验。"""

    row = await CatalogRepository(session).get_solution(solutionId)
    if row is None:
        raise AppError("RESOURCE_NOT_FOUND", "方案不存在或已下架", status_code=404)
    data = SolutionDetail(
        id=row.id,
        name=row.name,
        subtitle=row.subtitle,
        description=row.description,
        cover_url=await _asset_url(request, row.cover_path),
        gallery_urls=[await _asset_url(request, path) for path in row.gallery_paths],
        room=RoomType(row.room),
        needs=[NeedType(value) for value in row.needs],
        features=row.features,
        pricing_note=row.pricing_note,
        frame_colors=[FrameColor.model_validate(value) for value in row.frame_colors],
        glass_options=[GlassOption.model_validate(value) for value in row.glass_options],
        default_frame_color_code=row.default_frame_color_code,
        default_glass_code=row.default_glass_code,
        available=row.available,
    )
    return ApiResponse(data=data, request_id=request.state.request_id)


@router.get("/sample-scenes", response_model=ApiResponse[SampleSceneList])
async def list_sample_scenes(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
) -> ApiResponse[SampleSceneList]:
    """公开样例可免登录读取；原图尺寸与默认选区作为体验流程的初始值。"""

    scenes = await CatalogRepository(session).list_sample_scenes()
    items = [
        SampleSceneData(
            id=row.id,
            name=row.name,
            scene_type=RoomType(row.scene_type),
            thumbnail_url=await _asset_url(request, row.thumbnail_path),
            width=row.width,
            height=row.height,
            default_selection=NormalizedSelection.model_validate(row.default_selection),
        )
        for row in scenes
    ]
    return ApiResponse(data=SampleSceneList(items=items), request_id=request.state.request_id)
