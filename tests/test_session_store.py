"""Redis 随机会话的摘要存储与设备轮换测试。"""

from __future__ import annotations

import asyncio
from typing import Any

from fakeredis.aioredis import FakeRedis

from huaye_window_ai.services.session import SessionStore


async def test_session_stores_only_digest_and_rotates_same_device() -> None:
    redis = FakeRedis()
    store = SessionStore(redis, 7200)

    first = await store.issue("user_1", "device_123")
    second = await store.issue("user_1", "device_123")

    assert first.token != second.token
    assert await store.get_user_id(first.token) is None
    assert await store.get_user_id(second.token) == "user_1"
    all_keys = [item.decode() if isinstance(item, bytes) else item for item in await redis.keys("*")]
    assert all(first.token not in key for key in all_keys)


async def test_multiple_devices_and_bulk_revoke() -> None:
    redis = FakeRedis()
    store = SessionStore(redis, 7200)
    first = await store.issue("user_1", "device_1")
    second = await store.issue("user_1", "device_2")

    assert await store.get_user_id(first.token) == "user_1"
    assert await store.get_user_id(second.token) == "user_1"
    await store.revoke_user("user_1")
    assert await store.get_user_id(first.token) is None
    assert await store.get_user_id(second.token) is None


async def test_overlapping_same_device_logins_leave_only_one_valid_session(monkeypatch: Any) -> None:
    """让两个轮换都读到同一旧值，保护读取旧令牌与替换之间的并发窗口。"""
    redis = FakeRedis()
    store = SessionStore(redis, 7200)
    other = await store.issue("user_1", "other_device")
    old = await store.issue("user_1", "same_device")
    original_get, original_pipeline = redis.get, redis.pipeline
    reads = 0
    both_read = asyncio.Event()

    async def overlap(get: Any, key: str) -> Any:
        """只阻挡首次两个设备键读取；重试读必须能继续，否则测试会人为制造死锁。"""
        nonlocal reads
        value = await get(key)
        if key.startswith("auth:device:") and reads < 2:
            reads += 1
            if reads == 2:
                both_read.set()
            await asyncio.wait_for(both_read.wait(), timeout=5)
        return value

    async def get(key: str) -> Any:
        return await overlap(original_get, key)

    def pipeline(*args: Any, **kwargs: Any) -> Any:
        """同时覆盖旧实现的直读与修复后的 WATCH 连接读取，调度不依赖实现的选择。"""
        pipe = original_pipeline(*args, **kwargs)
        pipe_get = pipe.get

        async def watched_get(key: str) -> Any:
            return await overlap(pipe_get, key)

        pipe.get = watched_get
        return pipe

    monkeypatch.setattr(redis, "get", get)
    monkeypatch.setattr(redis, "pipeline", pipeline)
    try:
        first, second = await asyncio.gather(store.issue("user_1", "same_device"), store.issue("user_1", "same_device"))
        valid = [await store.get_user_id(item.token) for item in (first, second)]
        assert valid.count("user_1") == 1
        assert await store.get_user_id(old.token) is None
        assert await store.get_user_id(other.token) == "user_1"
        await store.revoke_user("user_1")
        assert await store.get_user_id(first.token) is None
        assert await store.get_user_id(second.token) is None
    finally:
        await redis.aclose()
