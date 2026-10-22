"""OSS 原生签名、私有对象读写及目录路径边界；只使用合成凭据，不连接云服务。"""

from __future__ import annotations

import io
import threading
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from alibabacloud_oss_v2.io_utils import StreamBodyReader
from pydantic import ValidationError

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError
from huaye_window_ai.integrations.photo_storage import create_photo_storage


def oss_settings(**overrides: object) -> Settings:
    """显式隔离开发配置，禁止签名测试使用用户的 AccessKey。"""
    values = dict(
        _env_file=None,
        wechat_app_id="wx_test",
        wechat_app_secret="synthetic",
        photo_storage_backend="oss",
        oss_bucket="huayewindowai",
        oss_region="cn-beijing",
        oss_endpoint="https://oss.example.test",
        oss_access_key_id="synthetic-id",
        oss_access_key_secret="synthetic-secret",
    )
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize("legacy", [{}, {"api_public_base_url": "", "catalog_public_base_url": ""}])
async def test_production_oss_does_not_require_local_preview_domain(legacy: dict[str, str]) -> None:
    """纯 OSS 的生产启动与签名只依赖 OSS 域名，不能被本地预览地址或其默认 HTTP 值阻塞。"""
    storage = create_photo_storage(oss_settings(app_env="production", app_docs_enabled=False, **legacy))
    try:
        url = await storage.signed_url("catalog/living-after.jpg", "", datetime.now(UTC) + timedelta(minutes=5))
        assert urlsplit(url).netloc == "oss.example.test"
        assert "x-oss-signature=" in url
    finally:
        await storage.aclose()


async def test_native_oss_signs_cname_with_v4_and_exact_expiration() -> None:
    """若错误拼上 Bucket 前缀、保存普通 URL 或使用错误期限，此签名回归会失败。"""
    storage = create_photo_storage(oss_settings())
    try:
        expires = datetime.now(UTC) + timedelta(minutes=10)
        url = await storage.signed_url("photos/" + "a" * 32 + "/original.jpg", "photo_" + "a" * 32, expires)
        parsed, query = urlsplit(url), parse_qs(urlsplit(url).query)
        assert parsed.netloc == "oss.example.test"
        assert parsed.path == "/photos/" + "a" * 32 + "/original.jpg"
        assert query["x-oss-signature-version"] == ["OSS4-HMAC-SHA256"]
        assert "cn-beijing/oss/aliyun_v4_request" in query["x-oss-credential"][0]
        start = datetime.strptime(query["x-oss-date"][0], "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)
        assert int(start.timestamp()) + int(query["x-oss-expires"][0]) <= int(expires.timestamp())
        assert query["x-oss-signature"]
    finally:
        await storage.aclose()


async def test_oss_private_io_runs_in_threads_and_closes_read_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """替换网络客户端边界，验证真实适配器保留对象路径、禁止覆盖及关闭响应流。"""
    storage = create_photo_storage(oss_settings())
    key = "photos/" + "a" * 32 + "/original.jpg"
    calls: list[tuple[str, object, int]] = []
    stream = io.BytesIO(b"private-photo")
    # 使用 SDK 的真实响应流包装器；它的 read() 不接受 size，不能用 BytesIO 接口代替。
    response = SimpleNamespace(
        read=lambda: stream.read(),
        close=stream.close,
        iter_bytes=lambda **kwargs: iter(lambda: stream.read(kwargs.get("block_size", 65536)), b""),
    )
    body = StreamBodyReader(response)

    def record(name: str, request: object) -> object:
        calls.append((name, request, threading.get_ident()))
        return SimpleNamespace(body=body)

    client = SimpleNamespace(
        put_object=lambda request: record("put", request),
        get_object=lambda request: record("get", request),
        delete_object=lambda request: record("delete", request),
    )
    monkeypatch.setattr(storage, "_get_client", lambda: client)
    await storage.put(key, b"private-photo", "image/jpeg")
    assert await storage.read(key) == b"private-photo"
    await storage.delete(key)
    assert stream.closed
    assert all(item[2] != threading.get_ident() for item in calls)
    assert calls[0][1].bucket == "huayewindowai"
    assert calls[0][1].key == key
    assert calls[0][1].forbid_overwrite is True
    assert calls[0][1].acl is None
    assert calls[0][1].content_type == "image/jpeg"


@pytest.mark.parametrize("key", ["../secret", "catalog/../photos/a.jpg", "/catalog/a.jpg", "https://evil.test/a.jpg"])
async def test_oss_rejects_paths_outside_managed_prefixes(key: str) -> None:
    storage = create_photo_storage(oss_settings())
    with pytest.raises(ValueError):
        await storage.signed_url(key, "", datetime.now(UTC) + timedelta(minutes=5))
    await storage.aclose()


async def test_oss_provider_failure_is_sanitized(monkeypatch: pytest.MonkeyPatch) -> None:
    storage = create_photo_storage(oss_settings())

    def fail() -> None:
        raise RuntimeError("provider-secret-and-private-url")

    monkeypatch.setattr(storage, "_get_client", fail)
    with pytest.raises(AppError) as caught:
        await storage.read("photos/" + "a" * 32 + "/original.jpg")
    assert caught.value.code == "PHOTO_STORAGE_UNAVAILABLE"
    assert "provider-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "overrides",
    [
        {"oss_access_key_id": ""},
        {"oss_access_key_secret": ""},
        {"oss_bucket": ""},
        {"oss_region": ""},
        {"oss_endpoint": "http://oss.example.test"},
        {"oss_endpoint": "https://oss.example.test/catalog"},
        {"catalog_oss_prefix": "photos"},
        {"catalog_oss_prefix": "catalog/../photos"},
    ],
)
def test_oss_configuration_requires_credentials_https_and_catalog_isolation(overrides: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        oss_settings(**overrides)


async def test_render_reference_is_read_from_oss(monkeypatch: pytest.MonkeyPatch) -> None:
    """避免展示新 OSS 封面、生成却继续读取随包旧封面的不一致。"""
    from huaye_window_ai.services.renders import RenderService

    storage = create_photo_storage(oss_settings())
    seen: list[str] = []

    async def read(key: str) -> bytes:
        seen.append(key)
        return b"oss-reference"

    monkeypatch.setattr(storage, "read", read)
    assert await RenderService(oss_settings(), storage).catalog_image("new-window.jpg") == b"oss-reference"
    assert seen == ["catalog/new-window.jpg"]
    await storage.aclose()


async def test_oss_stream_limit_closes_response(monkeypatch: pytest.MonkeyPatch) -> None:
    """超大响应必须在流迭代时中止，仍关闭 SDK 响应，不退回无上限 read()。"""
    from huaye_window_ai.integrations import oss_storage

    monkeypatch.setattr(oss_storage, "MAX_OSS_READ_BYTES", 4)
    closed: list[bool] = []
    response = SimpleNamespace(
        iter_bytes=lambda **kwargs: iter([b"123", b"45"]),
        close=lambda: closed.append(True),
    )
    storage = create_photo_storage(oss_settings())
    monkeypatch.setattr(
        storage,
        "_get_client",
        lambda: SimpleNamespace(
            get_object=lambda request: SimpleNamespace(body=StreamBodyReader(response)),
        ),
    )
    with pytest.raises(AppError, match="照片存储"):
        await storage.read("photos/" + "a" * 32 + "/original.jpg")
    assert closed == [True]
    await storage.aclose()
