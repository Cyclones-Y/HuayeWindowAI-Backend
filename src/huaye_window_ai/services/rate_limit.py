"""登录接口的 Redis 固定窗口限流。"""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime

from redis.asyncio import Redis

from huaye_window_ai.core.errors import RateLimitedError


class LoginRateLimiter:
    """IP 与设备双维度限制，设备标识和 IP 在 key 中仅保留不可逆摘要。"""

    def __init__(self, redis: Redis, ip_limit: int, device_limit: int) -> None:
        self._redis = redis
        self._ip_limit = ip_limit
        self._device_limit = device_limit

    async def check(self, ip_address: str, device_id: str) -> None:
        minute = datetime.now(UTC).strftime("%Y%m%d%H%M")
        keys = (
            (f"rate:login:ip:{self._hash(ip_address)}:{minute}", self._ip_limit),
            (f"rate:login:device:{self._hash(device_id)}:{minute}", self._device_limit),
        )
        for key, limit in keys:
            count = await self._redis.incr(key)
            if count == 1:
                await self._redis.expire(key, 70)
            if count > limit:
                raise RateLimitedError

    @staticmethod
    def _hash(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()[:24]
