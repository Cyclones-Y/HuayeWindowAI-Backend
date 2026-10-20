"""私有照片上传/删除，以及本地存储的短期签名预览入口。"""

from __future__ import annotations

from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, File, Form, Path, Query, Request, Response, UploadFile
from sqlalchemy.ext.asyncio import AsyncSession

from huaye_window_ai.api.dependencies import get_current_user_id
from huaye_window_ai.core.responses import ApiResponse
from huaye_window_ai.db.session import get_db_session
from huaye_window_ai.schemas.photos import UploadedPhotoData
from huaye_window_ai.services.photos import PhotoService

router = APIRouter(prefix="/photos", tags=["照片"])


def get_photo_service(request: Request) -> PhotoService:
    """使用应用级存储配置，测试可以替换服务而无需连接外部对象存储。"""

    return cast(PhotoService, request.app.state.photo_service)


@router.post("", response_model=ApiResponse[UploadedPhotoData])
async def upload_photo(
    request: Request,
    file: Annotated[UploadFile, File(description="JPG/PNG/HEIC 现场照片")],
    purpose: Annotated[Literal["WINDOW_PREVIEW"], Form()],
    session: AsyncSession = Depends(get_db_session),
    user_id: str = Depends(get_current_user_id),
    service: PhotoService = Depends(get_photo_service),
) -> ApiResponse[UploadedPhotoData]:
    """只在认证用户名下创建照片，并保证读取失败时也关闭 multipart 临时文件。"""

    try:
        data = await service.upload(file, user_id, session)
    finally:
        await file.close()
    return ApiResponse(data=data, request_id=request.state.request_id)


@router.delete("/{photoId}", response_model=ApiResponse[None])
async def delete_photo(
    request: Request,
    photo_id: str = Path(alias="photoId"),
    session: AsyncSession = Depends(get_db_session),
    user_id: str = Depends(get_current_user_id),
    service: PhotoService = Depends(get_photo_service),
) -> ApiResponse[None]:
    """所有者可以重复删除；其他用户按契约返回 403，且不返回照片属性。"""

    await service.delete(photo_id, user_id, session)
    return ApiResponse(data=None, request_id=request.state.request_id)


@router.get("/{photoId}/preview", response_class=Response)
async def preview_photo(
    photo_id: str = Path(alias="photoId"),
    expires: int = Query(gt=0),
    signature: str = Query(min_length=1, max_length=128),
    session: AsyncSession = Depends(get_db_session),
    service: PhotoService = Depends(get_photo_service),
) -> Response:
    """图片组件不携带 Bearer，因此用限时签名；禁用缓存以确保删除后的后续请求立即失效。"""

    data = await service.preview(photo_id, expires, signature, session)
    return Response(
        content=data,
        media_type="image/png" if data.startswith(b"\x89PNG") else "image/jpeg",
        headers={"Cache-Control": "private, no-store", "X-Content-Type-Options": "nosniff"},
    )
