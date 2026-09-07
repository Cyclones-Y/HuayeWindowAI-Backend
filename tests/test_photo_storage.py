"""照片私有存储测试，覆盖路径边界、限时签名以及无网络的 S3 协议。"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, BinaryIO
from urllib.parse import parse_qs, urlsplit

import pytest

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError
from huaye_window_ai.integrations import photo_storage
from huaye_window_ai.integrations.photo_storage import LocalPhotoStorage, S3PhotoStorage, create_photo_storage

KEY = "photos/0123456789abcdef0123456789abcdef/original.jpg"
PHOTO_ID = "photo_0123456789abcdef0123456789abcdef"


def _future() -> datetime:
    """所有测试使用带时区期限，避免本机时区影响签名行为。"""

    return datetime.now(UTC) + timedelta(minutes=15)


async def test_local_round_trip_and_repeated_delete(tmp_path: Path) -> None:
    storage = LocalPhotoStorage(tmp_path)
    await storage.put(KEY, b"private photo bytes", "image/jpeg")
    assert await storage.read(KEY) == b"private photo bytes"

    await storage.delete(KEY)
    await storage.delete(KEY)
    with pytest.raises(AppError) as caught:
        await storage.read(KEY)
    assert caught.value.code == "RESOURCE_NOT_FOUND"
    assert caught.value.status_code == 404


async def test_local_signed_url_uses_configured_prefix_and_verifies_identity_and_expiry(tmp_path: Path) -> None:
    storage = LocalPhotoStorage(
        tmp_path, api_prefix="/api/v2", public_base_url="https://api.example.test/", signing_key="test-signing-key"
    )
    expires_at = _future()
    url = await storage.signed_url(KEY, PHOTO_ID, expires_at)
    parsed = urlsplit(url)
    params = parse_qs(parsed.query)
    expires = int(params["expires"][0])
    signature = params["signature"][0]

    assert parsed.scheme == "https"
    assert parsed.netloc == "api.example.test"
    assert parsed.path == f"/api/v2/photos/{PHOTO_ID}/preview"
    assert expires == int(expires_at.timestamp())
    assert KEY not in url
    assert storage.verify_signature(PHOTO_ID, expires, signature)
    assert not storage.verify_signature("photo_" + "f" * 32, expires, signature)
    assert not storage.verify_signature(PHOTO_ID, expires + 1, signature)
    assert not storage.verify_signature(PHOTO_ID, 1, signature)
    assert not storage.verify_signature(PHOTO_ID, expires, "invalid-signature")
    assert not storage.verify_signature(PHOTO_ID, expires, "非 ASCII 签名")
    assert not storage.verify_signature("../../private", expires, signature)


async def test_local_different_signing_key_rejects_existing_link(tmp_path: Path) -> None:
    original = LocalPhotoStorage(tmp_path, signing_key="original-key")
    rotated = LocalPhotoStorage(tmp_path, signing_key="rotated-key")
    params = parse_qs(urlsplit(await original.signed_url(KEY, PHOTO_ID, _future())).query)
    assert not rotated.verify_signature(PHOTO_ID, int(params["expires"][0]), params["signature"][0])


@pytest.mark.parametrize(
    "key",
    [
        "../outside.jpg",
        "/photos/outside.jpg",
        "C:/photos/outside.jpg",
        "photos\\0123456789abcdef0123456789abcdef\\original.jpg",
        "photos/0123456789abcdef0123456789abcdef/../../outside.jpg",
        "photos/0123456789abcdef0123456789abcdef/original.jpg:private",
        "photos/0123456789abcdef0123456789abcdef/original.svg",
    ],
)
async def test_both_backends_reject_non_server_keys_before_accessing_storage(tmp_path: Path, key: str) -> None:
    # 相同对象键约束用于本地与 S3，避免切换存储后允许跨前缀访问其他对象。
    for storage in (LocalPhotoStorage(tmp_path), S3PhotoStorage(bucket="private-photos")):
        with pytest.raises(ValueError, match="存储键格式"):
            await storage.put(key, b"bytes", "image/jpeg")
        with pytest.raises(ValueError, match="存储键格式"):
            await storage.delete(key)
        with pytest.raises(ValueError, match="存储键格式"):
            await storage.signed_url(key, PHOTO_ID, _future())
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []


async def test_local_rejects_existing_symlink_escape_without_reading_target(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Windows 创建符号链接可能需要额外权限；模拟 resolve 结果即可覆盖相同的边界判断。
    original_resolve = Path.resolve
    outside = tmp_path.parent / "outside.jpg"

    def resolve(path: Path, *args: Any, **kwargs: Any) -> Path:
        if path.name == "original.jpg":
            return outside
        return original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="越出私有目录"):
        await LocalPhotoStorage(tmp_path).read(KEY)


async def test_local_disk_operations_run_off_event_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_path = LocalPhotoStorage._path
    thread_ids: list[int] = []

    def recorded_path(storage: LocalPhotoStorage, key: str) -> Path:
        thread_ids.append(threading.get_ident())
        return original_path(storage, key)

    monkeypatch.setattr(LocalPhotoStorage, "_path", recorded_path)
    loop_thread = threading.get_ident()
    storage = LocalPhotoStorage(tmp_path)
    await storage.put(KEY, b"bytes", "image/jpeg")
    await storage.read(KEY)
    await storage.delete(KEY)
    assert len(thread_ids) == 3
    assert all(thread_id != loop_thread for thread_id in thread_ids)


async def test_local_write_failure_hides_private_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_write(*args: Any, **kwargs: Any) -> None:
        raise PermissionError("sensitive disk path and credentials")

    monkeypatch.setattr(LocalPhotoStorage, "_put", fail_write)
    with pytest.raises(AppError) as caught:
        await LocalPhotoStorage(tmp_path).put(KEY, b"bytes", "image/jpeg")
    assert caught.value.code == "PHOTO_STORAGE_UNAVAILABLE"
    assert caught.value.status_code == 503
    assert "sensitive" not in caught.value.message


async def test_local_close_failure_removes_partial_photo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    original_open = Path.open

    @contextmanager
    def failing_open(path: Path, mode: str) -> Iterator[BinaryIO]:
        # 模拟数据已写出、关闭刷新失败的磁盘故障，避免残缺文件被后续预览误读。
        with original_open(path, mode) as destination:
            yield destination
        raise OSError("flush failed")

    monkeypatch.setattr(Path, "open", failing_open)
    with pytest.raises(AppError) as caught:
        await LocalPhotoStorage(tmp_path).put(KEY, b"partial bytes", "image/jpeg")
    assert caught.value.code == "PHOTO_STORAGE_UNAVAILABLE"
    assert not await asyncio.to_thread((tmp_path / KEY).exists)


async def test_local_duplicate_key_does_not_remove_or_replace_original(tmp_path: Path) -> None:
    storage = LocalPhotoStorage(tmp_path)
    await storage.put(KEY, b"original photo", "image/jpeg")
    with pytest.raises(AppError):
        await storage.put(KEY, b"replacement", "image/jpeg")
    assert await storage.read(KEY) == b"original photo"


@pytest.mark.parametrize("expired", [datetime.now(UTC) - timedelta(seconds=1), datetime(2030, 1, 1)])
async def test_both_backends_reject_expired_or_naive_expiry(tmp_path: Path, expired: datetime) -> None:
    for storage in (LocalPhotoStorage(tmp_path), S3PhotoStorage(bucket="private-photos")):
        with pytest.raises(ValueError):
            await storage.signed_url(KEY, PHOTO_ID, expired)


class FakeS3Client:
    """记录 SDK 调用及线程，模拟对象存储且不允许实际网络请求。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any], int]] = []

    def put_object(self, **kwargs: Any) -> None:
        self.calls.append(("put", kwargs, threading.get_ident()))

    def delete_object(self, **kwargs: Any) -> None:
        self.calls.append(("delete", kwargs, threading.get_ident()))

    def generate_presigned_url(self, method: str, **kwargs: Any) -> str:
        self.calls.append((method, kwargs, threading.get_ident()))
        return "https://private.example.test/signed-object"

    def close(self) -> None:
        self.calls.append(("close", {}, threading.get_ident()))


@pytest.mark.parametrize("backend", ["local", "s3"])
async def test_cancelled_put_waits_for_write_before_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, backend: str
) -> None:
    # 阻塞工作线程，复现客户端取消后服务层准备补偿删除、实际写入却尚未完成的时序。
    started = asyncio.Event()
    release = threading.Event()
    completed = threading.Event()
    loop = asyncio.get_running_loop()
    objects: dict[str, bytes] = {}

    def wait_for_release() -> None:
        loop.call_soon_threadsafe(started.set)
        if not release.wait(timeout=3):
            raise RuntimeError("测试未释放阻塞写入")

    if backend == "local":
        storage = LocalPhotoStorage(tmp_path)
        original_put = storage._put

        def local_put(key: str, data: bytes) -> None:
            wait_for_release()
            original_put(key, data)
            completed.set()

        monkeypatch.setattr(storage, "_put", local_put)
    else:
        storage = S3PhotoStorage(bucket="private-photos")
        fake = FakeS3Client()

        def s3_put(**kwargs: Any) -> None:
            wait_for_release()
            objects[kwargs["Key"]] = kwargs["Body"]
            completed.set()

        def s3_delete(**kwargs: Any) -> None:
            objects.pop(kwargs["Key"], None)

        monkeypatch.setattr(fake, "put_object", s3_put)
        monkeypatch.setattr(fake, "delete_object", s3_delete)
        monkeypatch.setattr(storage, "_get_client", lambda: fake)

    uploading = asyncio.create_task(storage.put(KEY, b"photo", "image/jpeg"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        uploading.cancel()
        assert not (await asyncio.wait({uploading}, timeout=0.02))[0]
        # 重复取消也不能越过写入完成屏障，真实服务关闭或上层超时可能再次发出取消。
        uploading.cancel()
        assert not (await asyncio.wait({uploading}, timeout=0.02))[0]
        assert not completed.is_set()
    finally:
        release.set()

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(uploading, timeout=1)
    assert completed.is_set()
    # 取消传回服务层后再清理，此时不会再有后台写入把已删照片重新创建出来。
    await storage.delete(KEY)
    if isinstance(storage, LocalPhotoStorage):
        with pytest.raises(AppError) as caught:
            await storage.read(KEY)
        assert caught.value.code == "RESOURCE_NOT_FOUND"
    else:
        assert objects == {}


async def test_s3_uses_private_objects_ttl_and_threaded_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3Client()
    # 固定时钟使 TTL 断言精确，同时验证签名没有继承 SDK 的默认一小时期限。
    monkeypatch.setattr(photo_storage.time, "time", lambda: 1_800_000_000.0)
    storage = S3PhotoStorage(bucket="private-photos")
    monkeypatch.setattr(storage, "_get_client", lambda: fake)
    await storage.put(KEY, b"photo", "image/jpeg")
    url = await storage.signed_url(KEY, PHOTO_ID, datetime.fromtimestamp(1_800_000_900, UTC))
    await storage.delete(KEY)

    assert url == "https://private.example.test/signed-object"
    assert fake.calls[0][1] == {"Bucket": "private-photos", "Key": KEY, "Body": b"photo", "ContentType": "image/jpeg"}
    assert fake.calls[1][1] == {
        "Params": {"Bucket": "private-photos", "Key": KEY},
        "ExpiresIn": 900,
        "HttpMethod": "GET",
    }
    assert fake.calls[2][1] == {"Bucket": "private-photos", "Key": KEY}
    assert all(call[2] != threading.get_ident() for call in fake.calls)


async def test_s3_client_initialization_is_lazy_and_uses_custom_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeS3Client()
    configured: dict[str, Any] = {}
    thread_ids: list[int] = []

    def client(service: str, **kwargs: Any) -> FakeS3Client:
        assert service == "s3"
        configured.update(kwargs)
        thread_ids.append(threading.get_ident())
        return fake

    def fake_import(name: str) -> Any:
        if name == "boto3":
            return SimpleNamespace(session=SimpleNamespace(Session=lambda: SimpleNamespace(client=client)))
        assert name == "botocore.config"
        return SimpleNamespace(Config=lambda **kwargs: kwargs)

    monkeypatch.setattr(photo_storage, "import_module", fake_import)
    storage = S3PhotoStorage(
        bucket="private-photos", endpoint_url="https://objects.example.test", region="custom-region"
    )
    assert configured == {}
    await storage.put(KEY, b"photo", "image/jpeg")
    await storage.delete(KEY)
    await storage.aclose()

    assert configured["endpoint_url"] == "https://objects.example.test"
    assert configured["region_name"] == "custom-region"
    assert configured["aws_access_key_id"] is None
    assert configured["aws_secret_access_key"] is None
    assert configured["config"]["signature_version"] == "s3v4"
    assert len(thread_ids) == 1
    assert thread_ids[0] != threading.get_ident()
    assert fake.calls[-1][0] == "close"


async def test_s3_sdk_failure_hides_provider_details(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = S3PhotoStorage(bucket="private-photos")

    def fail_client() -> None:
        raise RuntimeError("private endpoint and sensitive credentials")

    monkeypatch.setattr(storage, "_get_client", fail_client)
    with pytest.raises(AppError) as caught:
        await storage.put(KEY, b"photo", "image/jpeg")
    assert caught.value.code == "PHOTO_STORAGE_UNAVAILABLE"
    assert caught.value.status_code == 503
    assert "credentials" not in caught.value.message


async def test_factory_uses_configured_absolute_local_preview_url(settings: Settings, tmp_path: Path) -> None:
    configured = settings.model_copy(
        update={"photo_local_directory": tmp_path, "api_prefix": "/api/v3", "api_public_base_url": "https://api.test"}
    )
    storage = create_photo_storage(configured)
    assert isinstance(storage, LocalPhotoStorage)
    assert (await storage.signed_url(KEY, PHOTO_ID, _future())).startswith(
        f"https://api.test/api/v3/photos/{PHOTO_ID}/"
    )
    assert await asyncio.to_thread(lambda: list(tmp_path.iterdir())) == []
    await storage.aclose()
