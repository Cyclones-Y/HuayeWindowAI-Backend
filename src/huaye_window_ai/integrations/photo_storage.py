"""照片私有存储适配器：开发环境落本地，部署环境可切换 S3 兼容服务。

调用者必须先验证照片与账户关系；此模块只接受服务端生成的对象键。磁盘及
同步 SDK 操作均在线程中执行，避免大图写入和凭证解析阻塞其他异步请求。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections.abc import Callable
from datetime import datetime
from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError

_KEY_PATTERN = re.compile(r"photos/[0-9a-f]{32}/(?:original|thumbnail)\.(?:jpg|jpeg|png|webp)\Z")
_PHOTO_ID_PATTERN = re.compile(r"photo_[0-9a-f]{32}\Z")
_SIGNATURE_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
# 开发环境无需提交固定密钥；重启后旧链接自然失效。多进程部署应显式配置同一密钥。
_DEVELOPMENT_SIGNING_KEY = secrets.token_bytes(32)


class PhotoStorage(Protocol):
    """上传服务依赖的最小存储协议；过期时间必须带时区，删除操作应可重复执行。"""

    async def put(self, key: str, data: bytes, content_type: str) -> None: ...

    async def delete(self, key: str) -> None: ...

    async def read(self, key: str) -> bytes: ...

    async def signed_url(self, key: str, photo_id: str, expires_at: datetime) -> str: ...

    async def aclose(self) -> None: ...


def _validate_key(key: str) -> None:
    """固定服务端对象键格式，排除绝对路径、Windows 盘符、分隔符和目录穿越。"""

    if not _KEY_PATTERN.fullmatch(key):
        raise ValueError("照片存储键格式无效")


def _expires_timestamp(expires_at: datetime) -> int:
    """拒绝无时区或已过期的地址，防止操作系统时区改变签名期限。"""

    if expires_at.tzinfo is None or expires_at.utcoffset() is None:
        raise ValueError("照片地址过期时间必须包含时区")
    expires = int(expires_at.timestamp())
    if expires <= time.time():
        raise ValueError("照片地址过期时间必须晚于当前时间")
    return expires


def _storage_unavailable() -> AppError:
    """底层异常可能含私有路径、服务地址或凭证，统一返回固定可公开错误。"""

    return AppError("PHOTO_STORAGE_UNAVAILABLE", "照片存储暂时不可用，请稍后重试", status_code=503)


async def _complete_write_before_cancelling(write: Callable[[], object]) -> None:
    """等待同步写入真正结束后再传播请求取消，确保上层回滚删除不会被迟到写入覆盖。"""

    worker = asyncio.create_task(asyncio.to_thread(write))
    try:
        await asyncio.shield(worker)
    except asyncio.CancelledError:
        # to_thread 无法中断已经执行的磁盘或 SDK 写入。即使请求再次取消，也须等待工作线程
        # 完成，否则服务层会先删除对象，后台写入随后又把对象创建出来，留下无数据库记录的照片。
        while not worker.done():
            try:
                await asyncio.shield(worker)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        if not worker.cancelled():
            # 取消已是调用者的主要结果；取走线程异常避免未处理任务警告，再由服务层统一补偿。
            worker.exception()
        raise


class LocalPhotoStorage:
    """不挂载静态目录，通过照片 ID 和到期时间的 HMAC 签名提供限时预览。

    目录应只允许应用和运维账户写入；API 路由验签后必须重新查询未删除照片，
    再将数据库中的对象键传给 read，不能使用请求提供的磁盘路径。
    """

    def __init__(
        self,
        directory: Path,
        *,
        api_prefix: str = "/v1",
        signing_key: str | None = None,
        public_base_url: str = "http://127.0.0.1:8000",
    ) -> None:
        self._directory = directory
        self._api_prefix = api_prefix
        self._public_base_url = public_base_url.rstrip("/")
        self._signing_key = signing_key.encode("utf-8") if signing_key else _DEVELOPMENT_SIGNING_KEY

    def _path(self, key: str) -> Path:
        """在执行 IO 的线程中验证真实路径，阻止目录内已有符号链接逃逸。"""

        _validate_key(key)
        root = self._directory.resolve()
        path = (root / key).resolve()
        if not path.is_relative_to(root):
            raise ValueError("照片存储路径越出私有目录")
        return path

    def _put(self, key: str, data: bytes) -> None:
        """只创建随机生成的新对象，防止重复对象键意外覆盖原有照片。"""

        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        created = False
        try:
            with path.open("xb") as destination:
                created = True
                destination.write(data)
        except OSError:
            if created:
                # 关闭时刷新缓冲区也可能失败；仅清理由本次调用创建的半成品，保留键冲突的旧对象。
                path.unlink(missing_ok=True)
            raise

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """保存原始字节；MIME 类型由照片数据库保存，预览时使用该权威记录。"""

        try:
            await _complete_write_before_cancelling(lambda: self._put(key, data))
        except OSError:
            raise _storage_unavailable() from None

    async def delete(self, key: str) -> None:
        """清理对象并容忍重复回滚；不递归删除目录或其他照片。"""

        def remove() -> None:
            self._path(key).unlink(missing_ok=True)

        try:
            await asyncio.to_thread(remove)
        except OSError:
            raise _storage_unavailable() from None

    async def read(self, key: str) -> bytes:
        """只供完成验签及数据库有效性检查的本地预览路由读取。"""

        try:
            return await asyncio.to_thread(lambda: self._path(key).read_bytes())
        except FileNotFoundError:
            raise AppError("RESOURCE_NOT_FOUND", "照片不存在或已删除", status_code=404) from None
        except OSError:
            raise _storage_unavailable() from None

    def _signature(self, photo_id: str, expires: int) -> str:
        """签名只绑定不可变照片 ID 和期限，URL 中不暴露可操纵的存储键。"""

        payload = f"{photo_id}:{expires}".encode("ascii")
        return hmac.new(self._signing_key, payload, hashlib.sha256).hexdigest()

    async def signed_url(self, key: str, photo_id: str, expires_at: datetime) -> str:
        """使用可信配置构造绝对地址，不接受请求 Host 参与链接签发。"""

        _validate_key(key)
        if not _PHOTO_ID_PATTERN.fullmatch(photo_id):
            raise ValueError("照片 ID 格式无效")
        expires = _expires_timestamp(expires_at)
        signature = self._signature(photo_id, expires)
        return (
            f"{self._public_base_url}{self._api_prefix}/photos/{photo_id}/preview"
            f"?expires={expires}&signature={signature}"
        )

    def verify_signature(self, photo_id: str, expires: int, signature: str) -> bool:
        """拒绝过期、变造和格式异常链接，比较签名时避免时序差异。"""

        if (
            expires <= time.time()
            or not _PHOTO_ID_PATTERN.fullmatch(photo_id)
            or not _SIGNATURE_PATTERN.fullmatch(signature)
        ):
            return False
        return hmac.compare_digest(self._signature(photo_id, expires), signature)

    async def aclose(self) -> None:
        """本地适配器不持有长生命周期文件句柄。"""


class _S3Client(Protocol):
    """SDK 使用面保持显式，避免给整个应用引入第三方动态类型。"""

    def put_object(self, *, Bucket: str, Key: str, Body: bytes, ContentType: str) -> object: ...

    def delete_object(self, *, Bucket: str, Key: str) -> object: ...

    def generate_presigned_url(
        self, ClientMethod: str, *, Params: dict[str, str], ExpiresIn: int, HttpMethod: str
    ) -> str: ...

    def close(self) -> None: ...


class S3PhotoStorage:
    """使用私有 Bucket 的 S3 兼容存储，不设置公共 ACL，只签发限时 GET。

    客户端在首次线程操作中创建；凭证留空时使用 SDK 官方凭证链，避免启动时
    在事件循环执行可能访问实例元数据服务的同步请求。
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str | None = None,
        region: str | None = None,
        access_key_id: str = "",
        secret_access_key: str = "",
    ) -> None:
        self._bucket = bucket
        self._endpoint_url = endpoint_url
        self._region = region
        self._access_key_id = access_key_id
        self._secret_access_key = secret_access_key
        self._client: _S3Client | None = None
        self._client_lock = threading.Lock()

    def _get_client(self) -> _S3Client:
        """每个适配器独占 Session；并发首次上传不会重复创建 SDK 客户端。"""

        with self._client_lock:
            if self._client is None:
                boto3 = import_module("boto3")
                config_module = import_module("botocore.config")
                config = config_module.Config(
                    signature_version="s3v4",
                    connect_timeout=5,
                    read_timeout=30,
                    retries={"max_attempts": 2, "mode": "standard"},
                )
                self._client = cast(
                    _S3Client,
                    boto3.session.Session().client(
                        "s3",
                        endpoint_url=self._endpoint_url,
                        region_name=self._region,
                        aws_access_key_id=self._access_key_id or None,
                        aws_secret_access_key=self._secret_access_key or None,
                        config=config,
                    ),
                )
            return self._client

    async def put(self, key: str, data: bytes, content_type: str) -> None:
        """写入私有对象，SDK 及远端返回的内部细节不会进入业务错误。"""

        _validate_key(key)
        try:
            await _complete_write_before_cancelling(
                lambda: self._get_client().put_object(Bucket=self._bucket, Key=key, Body=data, ContentType=content_type)
            )
        except Exception:
            raise _storage_unavailable() from None

    async def delete(self, key: str) -> None:
        """使用 S3 幂等删除支持数据库保存失败后的补偿清理。"""

        _validate_key(key)
        try:
            await asyncio.to_thread(lambda: self._get_client().delete_object(Bucket=self._bucket, Key=key))
        except Exception:
            raise _storage_unavailable() from None

    async def signed_url(self, key: str, photo_id: str, expires_at: datetime) -> str:
        """由 SDK 签发私有对象地址，并限制在 SigV4 允许的七天内。"""

        _validate_key(key)
        expires = _expires_timestamp(expires_at)
        # 向下取整，确保存储链接不会晚于数据库返回的 expiresAt 失效。
        ttl = min(int(expires - time.time()), 604800)
        if ttl < 1:
            raise ValueError("照片地址剩余有效时间不足一秒")
        try:
            return await asyncio.to_thread(
                lambda: self._get_client().generate_presigned_url(
                    "get_object", Params={"Bucket": self._bucket, "Key": key}, ExpiresIn=ttl, HttpMethod="GET"
                )
            )
        except Exception:
            raise _storage_unavailable() from None

    async def aclose(self) -> None:
        """释放已建立的 SDK 连接池；未使用过 S3 时不额外创建客户端。"""

        if self._client is not None:
            await asyncio.to_thread(self._client.close)

    async def read(self, key: str) -> bytes:
        """后端直接读取私有源图，无需让供应商访问短期签名或局域网 URL。"""
        from typing import Any

        _validate_key(key)

        def fetch() -> bytes:
            client = cast(Any, self._get_client())
            response = client.get_object(Bucket=self._bucket, Key=key)
            body = response["Body"]
            try:
                # 40MP RGB 的无损 PNG 可超过模型上传的 30MB；私有快照须保留原尺寸。
                # 128MiB 容纳原始像素和 PNG 开销，模型输入在 worker 中另行缩小。
                limit = 128 * 1024 * 1024
                data: bytes = body.read(limit + 1)
                if len(data) > limit:
                    raise ValueError("私有图像超过读取上限")
                return data
            finally:
                body.close()

        try:
            return await asyncio.to_thread(fetch)
        except Exception:
            raise _storage_unavailable() from None


def create_photo_storage(settings: Settings) -> PhotoStorage:
    """按已校验配置选择存储；构造阶段不读写磁盘，也不连接外部服务。"""

    if settings.photo_storage_backend == "s3":
        return S3PhotoStorage(
            bucket=settings.photo_s3_bucket,
            endpoint_url=settings.photo_s3_endpoint_url,
            region=settings.photo_s3_region,
            access_key_id=settings.photo_s3_access_key_id.get_secret_value(),
            secret_access_key=settings.photo_s3_secret_access_key.get_secret_value(),
        )
    return LocalPhotoStorage(
        settings.photo_local_directory,
        api_prefix=settings.api_prefix,
        public_base_url=settings.api_public_base_url,
        signing_key=settings.photo_url_signing_key.get_secret_value(),
    )
