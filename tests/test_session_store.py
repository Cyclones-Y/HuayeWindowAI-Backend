"""Redis 随机会话的摘要存储与设备轮换测试。"""

from __future__ import annotations

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
