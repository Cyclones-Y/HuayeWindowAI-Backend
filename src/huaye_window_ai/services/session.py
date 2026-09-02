"""Redis 随机会话的签发、轮换与撤销。"""

from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Awaitable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, cast

from redis.asyncio import Redis


@dataclass(frozen=True, slots=True)
class IssuedSession:
    token: str
    expires_in: int


class SessionStore:
    """只在 Redis 保存随机令牌摘要，数据库和日志均不接触原始令牌。"""

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    async def issue(self, user_id: str, device_id: str) -> IssuedSession:
        token = secrets.token_urlsafe(32)
        digest = self.digest(token)
        device_digest = hashlib.sha256(device_id.encode("utf-8")).hexdigest()
        device_key = f"auth:device:{user_id}:{device_digest}"
        old_digest = await self._redis.get(device_key)
        old_digest_text = self._decode(old_digest) if old_digest else None

        value = json.dumps(
            {
                "userId": user_id,
                "deviceIdHash": device_digest,
                "issuedAt": datetime.now(UTC).isoformat(),
            },
            separators=(",", ":"),
        )
        user_set_key = f"auth:user-sessions:{user_id}"
        pipeline = self._redis.pipeline(transaction=True)
        if old_digest_text:
            pipeline.delete(self._session_key(old_digest_text))
            pipeline.srem(user_set_key, old_digest_text)
        pipeline.set(self._session_key(digest), value, ex=self._ttl_seconds)
        pipeline.set(device_key, digest, ex=self._ttl_seconds)
        pipeline.sadd(user_set_key, digest)
        pipeline.expire(user_set_key, self._ttl_seconds)
        await pipeline.execute()
        return IssuedSession(token=token, expires_in=self._ttl_seconds)

    async def get_user_id(self, token: str) -> str | None:
        raw = await self._redis.get(self._session_key(self.digest(token)))
        if not raw:
            return None
        try:
            payload = json.loads(self._decode(raw))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None
        user_id = payload.get("userId")
        return user_id if isinstance(user_id, str) else None

    async def revoke_user(self, user_id: str) -> None:
        """为后续数据清除和封禁预留一次撤销该用户全部设备会话的能力。"""

        user_set_key = f"auth:user-sessions:{user_id}"
        digests = await cast(Awaitable[set[Any]], self._redis.smembers(user_set_key))
        pipeline = self._redis.pipeline(transaction=True)
        for digest in digests:
            pipeline.delete(self._session_key(self._decode(digest)))
        pipeline.delete(user_set_key)
        await pipeline.execute()

    @staticmethod
    def digest(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _session_key(digest: str) -> str:
        return f"auth:session:{digest}"

    @staticmethod
    def _decode(value: str | bytes) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else value
