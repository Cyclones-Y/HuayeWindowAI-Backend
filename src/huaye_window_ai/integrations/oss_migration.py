"""显式迁移工具的计划与校验；不会改数据库路径，不读取无归属或已删除的照片。"""

from __future__ import annotations

import asyncio
import mimetypes
from pathlib import Path

from sqlalchemy import select

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError
from huaye_window_ai.db.photo_models import Photo
from huaye_window_ai.db.session import create_engine, create_session_factory
from huaye_window_ai.integrations.oss_storage import catalog_object_key
from huaye_window_ai.integrations.photo_storage import PhotoStorage, _validate_key


def _source(root: Path, relative: str) -> Path:
    """解析后的文件必须留在指定根目录内；禁止借符号链接读取其他目录。"""
    source = root / relative
    if not source.resolve().is_relative_to(root.resolve()) or not source.is_file():
        raise ValueError(f"迁移源文件不存在或超出目录：{relative}")
    return source


def catalog_plan(root: Path, prefix: str) -> list[tuple[str, Path]]:
    """按原相对路径生成方案素材上传计划；不调用 SDK，不改变运营数据库。"""
    if not root.is_dir():
        raise ValueError("目录素材文件夹不存在")
    plan = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            relative = path.relative_to(root).as_posix()
            plan.append((catalog_object_key(prefix, relative), _source(root, relative)))
    return plan


async def local_photo_plan(settings: Settings) -> list[tuple[str, Path]]:
    """只迁移数据库中未删除的原图、缩略图与生成快照，过滤未登记的残留文件。"""
    engine = create_engine(settings.resolved_database_url)
    try:
        async with create_session_factory(engine)() as session:
            rows = await session.scalars(select(Photo).where(Photo.deleted_at.is_(None)))
            keys = {key for row in rows for key in (row.storage_key, row.thumbnail_key)}
        plan = []
        for key in sorted(keys):
            _validate_key(key)
            plan.append((key, _source(settings.photo_local_directory, key)))
        return plan
    finally:
        await engine.dispose()


async def upload_verified(storage: PhotoStorage, key: str, source: Path) -> None:
    """禁止覆盖对象；重复运行时只接受相同内容，且上传后读回比对完整字节。"""
    data = await asyncio.to_thread(source.read_bytes)
    try:
        await storage.put(key, data, mimetypes.guess_type(source.name)[0] or "application/octet-stream")
    except AppError:
        # OSS 禁止覆盖或写响应丢失时，确认远端字节一致才视为已完成；其他错误继续失败。
        if await storage.read(key) != data:
            raise ValueError(f"OSS 同名对象内容不同，停止迁移：{key}") from None
    if await storage.read(key) != data:
        raise ValueError(f"OSS 上传后校验失败：{key}")
