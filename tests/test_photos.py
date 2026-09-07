"""照片 HTTP 契约、真实解码、归属隔离以及跨存储失败后的清理回归。"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from fakeredis.aioredis import FakeRedis
from fastapi import FastAPI, UploadFile
from PIL import Image
from PIL.PngImagePlugin import PngInfo
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError
from huaye_window_ai.db.base import Base
from huaye_window_ai.db.models import User
from huaye_window_ai.db.photo_models import Photo
from huaye_window_ai.integrations.photo_storage import LocalPhotoStorage
from huaye_window_ai.main import create_app
from huaye_window_ai.services.photos import PhotoService, process_photo
from huaye_window_ai.services.session import SessionStore


def make_image(*, image_format: str = "JPEG", size: tuple[int, int] = (20, 30), exif: bytes = b"") -> bytes:
    """生成真实小图，格式不依赖扩展名；EXIF 参数用于保护方向纠正和隐私清理行为。"""

    output = io.BytesIO()
    Image.new("RGB", size, (60, 120, 180)).save(output, format=image_format, **({"exif": exif} if exif else {}))
    return output.getvalue()


@dataclass
class PhotoContext:
    """用真实 SQLite/本地私有存储和内存 Redis 测 HTTP，不跳过 Bearer 所有者校验。"""

    app: FastAPI
    client: httpx.AsyncClient
    session_factory: async_sessionmaker[AsyncSession]
    storage: LocalPhotoStorage
    service: PhotoService
    settings: Settings
    directory: Path
    owner_headers: dict[str, str]
    other_headers: dict[str, str]

    async def upload(self, data: bytes | None = None, **kwargs: Any) -> httpx.Response:
        """默认提交合规表单，单个测试可替换文件字节或请求头来覆盖异常路径。"""

        headers = kwargs.pop("headers", self.owner_headers)
        return await self.client.post(
            f"{self.settings.api_prefix}/photos",
            files={"file": ("camera.jpg", data if data is not None else make_image(), "image/jpeg")},
            data={"purpose": "WINDOW_PREVIEW"},
            headers=headers,
            **kwargs,
        )


@pytest.fixture
async def photo_context(settings: Settings, tmp_path: Path) -> AsyncIterator[PhotoContext]:
    config = settings.model_copy(update={"photo_local_directory": tmp_path, "api_public_base_url": "http://test"})
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with factory() as session:
        session.add_all(
            [
                User(id="user_owner", phone_number="+8613800138000", phone_country_code="86"),
                User(id="user_other", phone_number="+8613900139000", phone_country_code="86"),
            ]
        )
        await session.commit()
    redis = FakeRedis()
    session_store = SessionStore(redis, 7200)
    owner_token = (await session_store.issue("user_owner", "device_owner")).token
    other_token = (await session_store.issue("user_other", "device_other")).token
    storage = LocalPhotoStorage(tmp_path, api_prefix=config.api_prefix, public_base_url="http://test")
    service = PhotoService(config, storage)
    app = create_app(config)
    # ASGITransport 不运行 lifespan；显式注入测试资源，避免创建 MySQL/真实 Redis 连接。
    app.state.settings = config
    app.state.session_factory = factory
    app.state.session_store = session_store
    app.state.photo_storage = storage
    app.state.photo_service = service
    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
            yield PhotoContext(
                app,
                client,
                factory,
                storage,
                service,
                config,
                tmp_path,
                {"Authorization": f"Bearer {owner_token}"},
                {"Authorization": f"Bearer {other_token}"},
            )
    finally:
        await storage.aclose()
        await redis.aclose()
        await engine.dispose()


async def test_upload_corrects_orientation_removes_metadata_and_returns_signed_preview(
    photo_context: PhotoContext,
) -> None:
    exif = Image.Exif()
    exif[274] = 6  # 相机的竖屏旋转必须反映到返回尺寸，防止后续框选落到错误坐标。
    exif[270] = "private camera location"
    response = await photo_context.upload(make_image(exif=exif.tobytes()))

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == "OK"
    assert body["requestId"]
    data = body["data"]
    assert data["width"] == 30 and data["height"] == 20
    assert data["mimeType"] == "image/jpeg"
    assert "storageKey" not in data
    preview = await photo_context.client.get(data["previewUrl"])
    assert preview.status_code == 200
    assert preview.headers["content-type"] == "image/jpeg"
    assert preview.headers["cache-control"] == "private, no-store"
    assert len(preview.content) == data["sizeBytes"]
    with Image.open(io.BytesIO(preview.content)) as clean:
        assert clean.size == (30, 20)
        assert not clean.getexif()
        assert "exif" not in clean.info
        assert "icc_profile" not in clean.info
    assert b"private camera location" not in preview.content
    async with photo_context.session_factory() as session:
        photo = await session.get(Photo, data["photoId"])
        assert photo is not None and photo.user_id == "user_owner"
        assert photo.purpose == "WINDOW_PREVIEW" and photo.deleted_at is None
        thumbnail = await photo_context.storage.read(photo.thumbnail_key)
    with Image.open(io.BytesIO(thumbnail)) as image:
        assert max(image.size) <= photo_context.settings.photo_thumbnail_max_edge
        assert not image.getexif()


def test_png_transparency_and_text_are_normalized_and_thumbnail_is_bounded() -> None:
    buffer = io.BytesIO()
    info = PngInfo()
    info.add_text("GPS", "private coordinates")
    Image.new("RGBA", (1200, 800), (0, 0, 0, 0)).save(buffer, format="PNG", pnginfo=info)

    processed = process_photo(buffer.getvalue(), max_pixels=1_000_000, thumbnail_max_edge=512)

    assert (processed.width, processed.height) == (1200, 800)
    assert b"private coordinates" not in processed.original
    with Image.open(io.BytesIO(processed.original)) as original:
        assert original.getpixel((0, 0)) == (255, 255, 255)
        assert "GPS" not in original.info
    with Image.open(io.BytesIO(processed.thumbnail)) as thumbnail:
        assert thumbnail.width == 512 and thumbnail.height <= 512


@pytest.mark.parametrize("image_format", ["JPEG", "PNG", "HEIF"])
async def test_upload_uses_real_decoded_type_instead_of_filename_or_mime(
    photo_context: PhotoContext, image_format: str
) -> None:
    response = await photo_context.client.post(
        "/v1/photos",
        files={"file": ("../../../untrusted.exe", make_image(image_format=image_format), "application/octet-stream")},
        data={"purpose": "WINDOW_PREVIEW"},
        headers=photo_context.owner_headers,
    )

    assert response.status_code == 200
    assert response.json()["data"]["mimeType"] == "image/jpeg"
    assert len(list(photo_context.directory.rglob("*.jpg"))) == 2


@pytest.mark.parametrize(
    ("content", "expected_code", "expected_status"),
    [
        (b"", "PHOTO_DECODE_FAILED", 422),
        (b"pretend image", "PHOTO_UNSUPPORTED", 415),
        (b"\xff\xd8\xffbroken-jpeg", "PHOTO_DECODE_FAILED", 422),
        (b"\x89PNG\r\n\x1a\ncorrupt-png", "PHOTO_DECODE_FAILED", 422),
    ],
)
async def test_invalid_uploads_never_create_files_or_records(
    photo_context: PhotoContext, content: bytes, expected_code: str, expected_status: int
) -> None:
    response = await photo_context.upload(content)

    assert response.status_code == expected_status
    assert response.json()["code"] == expected_code
    assert not list(photo_context.directory.rglob("*.jpg"))
    async with photo_context.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Photo)) == 0


async def test_valid_but_unsupported_image_is_rejected(photo_context: PhotoContext) -> None:
    response = await photo_context.upload(make_image(image_format="GIF"))
    assert response.status_code == 415
    assert response.json()["code"] == "PHOTO_UNSUPPORTED"


async def test_pixel_limit_is_checked_before_persisting(photo_context: PhotoContext) -> None:
    photo_context.settings.photo_max_pixels = 100
    response = await photo_context.upload()
    assert response.status_code == 413
    assert response.json()["code"] == "PHOTO_TOO_LARGE"
    assert not list(photo_context.directory.rglob("*.jpg"))


@pytest.mark.parametrize("declared_size", [None, 1025])
async def test_byte_limit_does_not_depend_on_declared_upload_size(
    photo_context: PhotoContext, declared_size: int | None
) -> None:
    photo_context.settings.photo_max_bytes = 1024
    upload = UploadFile(io.BytesIO(b"x" * 1025), size=declared_size)
    try:
        async with photo_context.session_factory() as session:
            with pytest.raises(AppError) as caught:
                await photo_context.service.upload(upload, "user_owner", session)
        assert caught.value.code == "PHOTO_TOO_LARGE"
        assert caught.value.status_code == 413
    finally:
        await upload.close()


@pytest.mark.parametrize("headers", [{}, {"Authorization": "Bearer invalid-token"}])
async def test_photo_upload_requires_live_bearer_session(photo_context: PhotoContext, headers: dict[str, str]) -> None:
    response = await photo_context.upload(headers=headers)
    assert response.status_code == 401
    assert response.json()["code"] == "UNAUTHORIZED"


@pytest.mark.parametrize("purpose", [None, "PROFILE_AVATAR"])
async def test_purpose_is_required_and_restricted(photo_context: PhotoContext, purpose: str | None) -> None:
    response = await photo_context.client.post(
        "/v1/photos",
        files={"file": ("camera.jpg", make_image(), "image/jpeg")},
        data={"purpose": purpose} if purpose else {},
        headers=photo_context.owner_headers,
    )
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


async def test_delete_is_owner_only_idempotent_and_revokes_existing_preview(photo_context: PhotoContext) -> None:
    data = (await photo_context.upload()).json()["data"]
    path = f"/v1/photos/{data['photoId']}"

    unauthorized = await photo_context.client.delete(path)
    assert unauthorized.status_code == 401
    other = await photo_context.client.delete(path, headers=photo_context.other_headers)
    assert other.status_code == 403
    assert other.json()["code"] == "RESOURCE_FORBIDDEN"
    assert (await photo_context.client.get(data["previewUrl"])).status_code == 200
    for _ in range(2):
        deleted = await photo_context.client.delete(path, headers=photo_context.owner_headers)
        assert deleted.status_code == 200
        assert deleted.json()["data"] is None
    assert (await photo_context.client.get(data["previewUrl"])).status_code == 404
    assert not list(photo_context.directory.rglob("*.jpg"))
    async with photo_context.session_factory() as session:
        photo = await session.get(Photo, data["photoId"])
        assert photo is not None and photo.deleted_at is not None


async def test_preview_rejects_tampered_signature_expiry_and_photo_id(photo_context: PhotoContext) -> None:
    data = (await photo_context.upload()).json()["data"]
    original = httpx.URL(data["previewUrl"])
    bad_signature = original.copy_set_param("signature", "0" * 64)
    bad_expiry = original.copy_set_param("expires", "1")
    other_photo = original.copy_with(path="/v1/photos/photo_" + "0" * 32 + "/preview")

    for url in (bad_signature, bad_expiry, other_photo):
        response = await photo_context.client.get(url)
        assert response.status_code == 403
        assert response.json()["code"] == "PHOTO_PREVIEW_EXPIRED"


async def test_partial_storage_write_is_cleaned_up(photo_context: PhotoContext, monkeypatch: Any) -> None:
    original_put = photo_context.storage.put

    async def fail_after_thumbnail_write(key: str, content: bytes, content_type: str) -> None:
        # 模拟远端已经接收对象但客户端随后报错，失败对象也必须参与补偿删除。
        await original_put(key, content, content_type)
        if "thumbnail" in key:
            raise AppError("PHOTO_STORAGE_UNAVAILABLE", "照片存储暂时不可用", status_code=503)

    monkeypatch.setattr(photo_context.storage, "put", fail_after_thumbnail_write)
    response = await photo_context.upload()

    assert response.status_code == 503
    assert not list(photo_context.directory.rglob("*.jpg"))
    async with photo_context.session_factory() as session:
        assert await session.scalar(select(func.count()).select_from(Photo)) == 0


async def test_database_failure_is_cleaned_up(photo_context: PhotoContext, monkeypatch: Any) -> None:
    async def fail_flush(_session: AsyncSession, objects: object = None) -> None:
        raise RuntimeError("simulated database write failure")

    monkeypatch.setattr(AsyncSession, "flush", fail_flush)
    upload = UploadFile(io.BytesIO(make_image()))
    try:
        async with photo_context.session_factory() as session:
            with pytest.raises(RuntimeError, match="simulated database write failure"):
                await photo_context.service.upload(upload, "user_owner", session)
        assert not list(photo_context.directory.rglob("*.jpg"))
    finally:
        await upload.close()


async def test_failed_delete_stays_inactive_and_can_retry(photo_context: PhotoContext, monkeypatch: Any) -> None:
    data = (await photo_context.upload()).json()["data"]
    original_delete = photo_context.storage.delete
    failed_once = False

    async def fail_once(key: str) -> None:
        nonlocal failed_once
        if not failed_once:
            failed_once = True
            raise AppError("PHOTO_STORAGE_UNAVAILABLE", "照片存储暂时不可用", status_code=503)
        await original_delete(key)

    monkeypatch.setattr(photo_context.storage, "delete", fail_once)
    path = f"/v1/photos/{data['photoId']}"
    first = await photo_context.client.delete(path, headers=photo_context.owner_headers)

    assert first.status_code == 503
    assert (await photo_context.client.get(data["previewUrl"])).status_code == 404
    second = await photo_context.client.delete(path, headers=photo_context.owner_headers)
    assert second.status_code == 200
    assert not list(photo_context.directory.rglob("*.jpg"))
