"""现场照片的真实解码、隐私元数据清除、私有存储和幂等删除。"""

from __future__ import annotations

import io
import uuid
import warnings
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

# pillow-heif 的公开重导出缺少显式类型导出，保留官方入口以免依赖内部模块路径。
from pillow_heif import register_heif_opener  # type: ignore[attr-defined]
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.concurrency import run_in_threadpool

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError
from huaye_window_ai.db.photo_models import Photo
from huaye_window_ai.db.render_models import RenderTask
from huaye_window_ai.integrations.photo_storage import LocalPhotoStorage, PhotoStorage
from huaye_window_ai.repositories.photos import PhotoRepository
from huaye_window_ai.schemas.photos import UploadedPhotoData
from huaye_window_ai.services.photo_journal import compensate_photo_write, prepare_photo_write

# 只注册 HEIF 解码器，不接受 Pillow 额外支持的 GIF、SVG 或其他文件类型。
register_heif_opener()
_ALLOWED_FORMATS = {"JPEG", "PNG", "HEIF"}


@dataclass(frozen=True, slots=True)
class ProcessedPhoto:
    """内存中的干净 JPEG 和缩略图；离开处理线程后不保留解码器对象或原始元数据。"""

    original: bytes
    thumbnail: bytes
    width: int
    height: int


def _has_supported_signature(data: bytes) -> bool:
    """只用于区分损坏图片与不支持的文件；最终格式与尺寸仍以真实解码结果为准。"""

    return (
        data.startswith(b"\xff\xd8\xff")
        or data.startswith(b"\x89PNG\r\n\x1a\n")
        or (len(data) >= 12 and data[4:8] == b"ftyp" and data[8:12] in {b"heic", b"heix", b"hevc", b"hevx", b"mif1"})
    )


def process_photo(data: bytes, *, max_pixels: int, thumbnail_max_edge: int) -> ProcessedPhoto:
    """在工作线程中解码并重新编码，不信任文件名/MIME；像素上限在完整解码前检查。"""

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in _ALLOWED_FORMATS:
                    raise AppError("PHOTO_UNSUPPORTED", "仅支持 JPG、PNG 和 HEIC 照片", status_code=415)
                if source.width * source.height > max_pixels:
                    raise AppError("PHOTO_TOO_LARGE", "照片分辨率超过限制，请压缩后重试", status_code=413)
                source.load()
                # 框选坐标应基于用户看到的方向；重新创建 RGB 画布可同时剥离 EXIF/GPS/ICC/文本等元数据。
                oriented = ImageOps.exif_transpose(source)
                rgba = oriented.convert("RGBA")
                clean = Image.new("RGB", rgba.size, "white")
                clean.paste(rgba, mask=rgba.getchannel("A"))
                width, height = clean.size
                original_buffer = io.BytesIO()
                clean.save(original_buffer, format="JPEG", quality=90)
                clean.thumbnail((thumbnail_max_edge, thumbnail_max_edge), Image.Resampling.LANCZOS)
                thumbnail_buffer = io.BytesIO()
                clean.save(thumbnail_buffer, format="JPEG", quality=85)
                return ProcessedPhoto(original_buffer.getvalue(), thumbnail_buffer.getvalue(), width, height)
    except (Image.DecompressionBombError, Image.DecompressionBombWarning):
        raise AppError("PHOTO_TOO_LARGE", "照片分辨率超过限制，请压缩后重试", status_code=413) from None
    except UnidentifiedImageError:
        if _has_supported_signature(data):
            raise AppError("PHOTO_DECODE_FAILED", "照片已损坏或无法解码，请重新选择", status_code=422) from None
        raise AppError("PHOTO_UNSUPPORTED", "仅支持 JPG、PNG 和 HEIC 照片", status_code=415) from None
    except (OSError, ValueError, SyntaxError):
        raise AppError("PHOTO_DECODE_FAILED", "照片已损坏或无法解码，请重新选择", status_code=422) from None


async def _read_upload(file: UploadFile, max_bytes: int) -> bytes:
    """分块读取并硬性限长，既兼容缺失 size 的上传，也避免一次读取无限大文件。"""

    if file.size is not None and file.size > max_bytes:
        raise AppError("PHOTO_TOO_LARGE", "照片文件超过大小限制，请压缩后重试", status_code=413)
    chunks = bytearray()
    while chunk := await file.read(min(65536, max_bytes - len(chunks) + 1)):
        chunks.extend(chunk)
        if len(chunks) > max_bytes:
            raise AppError("PHOTO_TOO_LARGE", "照片文件超过大小限制，请压缩后重试", status_code=413)
    if not chunks:
        raise AppError("PHOTO_DECODE_FAILED", "照片为空，请重新选择", status_code=422)
    return bytes(chunks)


class PhotoService:
    """协调照片持久化；返回成功前必须完成两份对象写入、签名和数据库提交。"""

    def __init__(self, settings: Settings, storage: PhotoStorage) -> None:
        self._settings = settings
        self._storage = storage

    async def upload(self, file: UploadFile, user_id: str, session: AsyncSession) -> UploadedPhotoData:
        """上传仅支持 WINDOW_PREVIEW；用途已由 HTTP 表单枚举约束，文件名不进入对象路径。"""

        data = await _read_upload(file, self._settings.photo_max_bytes)
        processed = await run_in_threadpool(
            process_photo,
            data,
            max_pixels=self._settings.photo_max_pixels,
            thumbnail_max_edge=self._settings.photo_thumbnail_max_edge,
        )
        object_id = uuid.uuid4().hex
        now = datetime.now(UTC)
        expires_at = now + timedelta(seconds=self._settings.photo_preview_ttl_seconds)
        photo = Photo(
            id=f"photo_{object_id}",
            user_id=user_id,
            purpose="WINDOW_PREVIEW",
            storage_key=f"photos/{object_id}/original.jpg",
            thumbnail_key=f"photos/{object_id}/thumbnail.jpg",
            width=processed.width,
            height=processed.height,
            size_bytes=len(processed.original),
            mime_type="image/jpeg",
            created_at=now,
        )
        keys = (photo.storage_key, photo.thumbnail_key)
        try:
            await prepare_photo_write(session, photo)
            await self._storage.put(keys[0], processed.original, "image/jpeg")
            await self._storage.put(keys[1], processed.thumbnail, "image/jpeg")
            preview_url = await self._storage.signed_url(keys[0], photo.id, expires_at)
            await PhotoRepository(session).add(photo)
            await session.commit()
        except BaseException:
            # 先核对提交结果，避免 COMMIT 成功但响应丢失时误删；清理失败仍有持久登记。
            await compensate_photo_write(session, self._storage, photo)
            raise
        return UploadedPhotoData(
            photo_id=photo.id,
            width=photo.width,
            height=photo.height,
            mime_type=photo.mime_type,
            size_bytes=photo.size_bytes,
            preview_url=preview_url,
            preview_url_expires_at=expires_at,
            created_at=now,
        )

    async def delete(self, photo_id: str, user_id: str, session: AsyncSession) -> None:
        """先禁用照片再清理对象，存储暂时失败时保留墓碑，允许同一请求安全重试。"""

        repository = PhotoRepository(session)
        # 创建任务与删除在同一源照片行上串行化，防止模型准备过程中物理删除输入。
        await session.scalar(select(Photo).where(Photo.id == photo_id).with_for_update())
        photo = await repository.get_owned(photo_id, user_id)
        if photo is None:
            if await repository.exists(photo_id):
                raise AppError("RESOURCE_FORBIDDEN", "无权操作该照片", status_code=403)
            raise AppError("RESOURCE_NOT_FOUND", "照片不存在", status_code=404)
        # 鉴权已建立 MySQL 一致性快照；这里必须 current read，才能看到等待行锁期间入队的任务。
        busy = await session.scalar(
            select(RenderTask.id)
            .where(RenderTask.photo_id == photo_id, RenderTask.status.in_(["QUEUED", "PROCESSING"]))
            .limit(1)
            .with_for_update()
        )
        if busy is not None or photo.purpose in {"RENDER_INPUT", "RENDER_RESULT"}:
            raise AppError("PHOTO_IN_USE", "照片关联生成任务，请等待任务结束或通过效果记录清理", status_code=409)
        if photo.deleted_at is None:
            photo.deleted_at = datetime.now(UTC)
            await session.commit()
        # 已完成结果持有独立原图快照，因此删除源照片不会破坏历史对比。
        await self._storage.delete(photo.storage_key)
        await self._storage.delete(photo.thumbnail_key)

    async def preview(self, photo_id: str, expires: int, signature: str, session: AsyncSession) -> bytes:
        """本地开发图片用短期签名访问；S3 模式直接使用对象存储的签名地址。"""

        if not isinstance(self._storage, LocalPhotoStorage):
            raise AppError("RESOURCE_NOT_FOUND", "照片不存在", status_code=404)
        if not self._storage.verify_signature(photo_id, expires, signature):
            raise AppError("PHOTO_PREVIEW_EXPIRED", "照片预览链接无效或已过期", status_code=403)
        photo = await PhotoRepository(session).get_active(photo_id)
        if photo is None:
            raise AppError("RESOURCE_NOT_FOUND", "照片不存在", status_code=404)
        return await self._storage.read(photo.storage_key)
