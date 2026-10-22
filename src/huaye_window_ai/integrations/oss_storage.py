"""原生 OSS 私有存储：数据库只保存对象路径，通过 CNAME 签发 V4 限时 GET。

照片账户权限由业务层检查；公开目录只允许签发 catalog 前缀内的数据库路径。
SDK 网络和签名操作在线程中执行，错误响应不暴露凭据或完整的签名 URL。
"""

from __future__ import annotations

import asyncio
import threading
from datetime import datetime
from importlib import import_module
from typing import Any, cast

from huaye_window_ai.core.config import Settings
from huaye_window_ai.integrations.photo_storage import (
    _complete_write_before_cancelling,
    _expires_timestamp,
    _storage_unavailable,
    _validate_key,
)

# 与本地/S3 生成快照读取预算一致；不能一次无上限读取 OSS 响应。
MAX_OSS_READ_BYTES = 128 * 1024 * 1024


def catalog_object_key(prefix: str, path: str) -> str:
    """目录记录为相对图片路径，拒绝 URL、目录穿越及私有照片前缀混入。"""
    parts = path.split("/")
    if (
        not path
        or any(part in {"", ".", ".."} for part in parts)
        or any(char in path for char in ("\\", ":", "?", "#", "\x00"))
        or any(ord(char) < 32 for char in path)
        or not path.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
    ):
        raise ValueError("目录图片必须使用相对图片路径")
    return f"{prefix}/{path}"


class OssPhotoStorage:
    """复用照片存储协议，所有对象保持私有，不修改 Bucket 或对象 ACL。

    目录对象限定为配置的 catalog 前缀；照片仍采用服务端随机路径。SDK 客户端
    懒创建并共享连接池，关闭应用时主动释放池；配置凭据缺失由启动校验拒绝。
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._sdk = import_module("alibabacloud_oss_v2")
        self._client: Any = None
        self._http_client: Any = None
        self._lock = threading.Lock()

    def _get_client(self) -> Any:
        """在线程首次使用时建立客户端，CNAME 模式避免 URL 被错误添加 Bucket 前缀。"""
        with self._lock:
            if self._client is None:
                sdk, settings = self._sdk, self._settings
                provider = sdk.credentials.StaticCredentialsProvider(
                    settings.oss_access_key_id.get_secret_value(), settings.oss_access_key_secret.get_secret_value()
                )
                self._http_client = import_module("alibabacloud_oss_v2.transport").RequestsHttpClient(
                    connect_timeout=5, readwrite_timeout=30
                )
                config = sdk.config.Config(
                    region=settings.oss_region,
                    endpoint=settings.oss_endpoint,
                    use_cname=True,
                    signature_version="v4",
                    credentials_provider=provider,
                    http_client=self._http_client,
                    connect_timeout=5,
                    readwrite_timeout=30,
                    retry_max_attempts=3,
                )
                self._client = sdk.Client(config)
            return self._client

    def _validate(self, key: str) -> None:
        """目录和私有照片使用不相交的命名空间，不能借目录路径读取任意对象。"""
        prefix = self._settings.catalog_oss_prefix + "/"
        if key.startswith(prefix):
            catalog_object_key(self._settings.catalog_oss_prefix, key[len(prefix) :])
        else:
            _validate_key(key)

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """写私有对象并禁止覆盖；取消请求须等写入结束再交由业务层补偿。"""
        self._validate(key)
        try:
            await _complete_write_before_cancelling(
                lambda: self._get_client().put_object(
                    self._sdk.PutObjectRequest(
                        bucket=self._settings.oss_bucket,
                        key=key,
                        body=data,
                        content_type=content_type,
                        forbid_overwrite=True,
                    )
                )
            )
        except Exception:
            raise _storage_unavailable() from None

    async def read(self, key: str) -> bytes:
        """后台读取私有源图供生成使用；限制大小并始终关闭响应流，防止连接泄漏。"""
        self._validate(key)

        def fetch() -> bytes:
            response = self._get_client().get_object(
                self._sdk.GetObjectRequest(bucket=self._settings.oss_bucket, key=key)
            )
            body = response.body
            try:
                # SDK StreamBodyReader.read() 只能全量读取，使用流迭代及时中止超大响应。
                data = bytearray()
                for chunk in body.iter_bytes(block_size=64 * 1024):
                    if len(data) + len(chunk) > MAX_OSS_READ_BYTES:
                        raise ValueError("私有图像超过读取上限")
                    data.extend(chunk)
                return bytes(data)
            finally:
                body.close()

        try:
            return await asyncio.to_thread(fetch)
        except Exception:
            raise _storage_unavailable() from None

    async def delete(self, key: str) -> None:
        """删除指定对象，保持 SDK 的不存在即成功语义供业务回滚和清理重试。"""
        self._validate(key)
        try:
            await asyncio.to_thread(
                lambda: self._get_client().delete_object(
                    self._sdk.DeleteObjectRequest(bucket=self._settings.oss_bucket, key=key)
                )
            )
        except Exception:
            raise _storage_unavailable() from None

    async def signed_url(self, key: str, photo_id: str, expires_at: datetime) -> str:
        """按路径及绝对到期时间签名；OSS 不使用照片 ID，URL 仅在响应时生成。"""
        self._validate(key)
        _expires_timestamp(expires_at)
        try:
            result = await asyncio.to_thread(
                lambda: self._get_client().presign(
                    self._sdk.GetObjectRequest(bucket=self._settings.oss_bucket, key=key),
                    expiration=expires_at,
                )
            )
            if not result.url:
                raise ValueError("OSS 未返回图片签名地址")
            return cast(str, result.url)
        except Exception:
            raise _storage_unavailable() from None

    async def aclose(self) -> None:
        """应用及 worker 退出后释放 SDK HTTP 池；未使用时不创建连接。"""
        if self._http_client is not None:
            await asyncio.to_thread(self._http_client.close)
