"""首次建档、静默复登、手机号归并和冲突保护。"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from fakeredis.aioredis import FakeRedis
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from huaye_window_ai.core.errors import AccountBindingConflictError, PhoneAuthRequiredError
from huaye_window_ai.core.security import PhoneProtector
from huaye_window_ai.db.base import Base
from huaye_window_ai.integrations.wechat import WechatIdentityData, WechatPhoneData
from huaye_window_ai.schemas.auth import WechatLoginRequest
from huaye_window_ai.services.auth import AuthService
from huaye_window_ai.services.session import SessionStore


@dataclass
class FakeWechatClient:
    identities: dict[str, WechatIdentityData]
    phones: dict[str, WechatPhoneData]

    async def code_to_identity(self, code: str) -> WechatIdentityData:
        return self.identities[code]

    async def get_phone_number(self, code: str) -> WechatPhoneData:
        return self.phones[code]


async def make_service() -> tuple[AuthService, async_sessionmaker[AsyncSession], FakeRedis]:
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    redis = FakeRedis()
    wechat = FakeWechatClient(
        identities={
            "login-a": WechatIdentityData("open-a", None),
            "login-b": WechatIdentityData("open-b", None),
        },
        phones={
            "phone-a": WechatPhoneData("86", "13800138000"),
            "phone-b": WechatPhoneData("86", "13900139000"),
        },
    )
    service = AuthService(
        wechat,  # type: ignore[arg-type]
        SessionStore(redis, 7200),
        PhoneProtector(b"e" * 32, b"h" * 32, 1),
        app_id="wx_test",
    )
    return service, factory, redis


async def test_new_identity_requires_phone_authorization() -> None:
    service, factory, _ = await make_service()
    async with factory() as session:
        with pytest.raises(PhoneAuthRequiredError):
            await service.login(WechatLoginRequest(code="login-a", deviceId="device_123"), session)


async def test_first_login_then_silent_login_rotates_session() -> None:
    service, factory, _ = await make_service()
    async with factory() as session:
        first = await service.login(
            WechatLoginRequest(code="login-a", phoneCode="phone-a", deviceId="device_123"), session
        )
    async with factory() as session:
        second = await service.login(WechatLoginRequest(code="login-a", deviceId="device_123"), session)

    assert first.is_new_user is True
    assert second.is_new_user is False
    assert first.user.id == second.user.id
    assert first.access_token != second.access_token


async def test_same_phone_merges_another_wechat_identity() -> None:
    service, factory, _ = await make_service()
    async with factory() as session:
        first = await service.login(
            WechatLoginRequest(code="login-a", phoneCode="phone-a", deviceId="device_123"), session
        )
    async with factory() as session:
        second = await service.login(
            WechatLoginRequest(code="login-b", phoneCode="phone-a", deviceId="device_456"), session
        )

    assert second.is_new_user is False
    assert second.user.id == first.user.id


async def test_existing_identity_cannot_switch_to_another_phone_account() -> None:
    service, factory, _ = await make_service()
    async with factory() as session:
        await service.login(WechatLoginRequest(code="login-a", phoneCode="phone-a", deviceId="device_123"), session)
    async with factory() as session:
        await service.login(WechatLoginRequest(code="login-b", phoneCode="phone-b", deviceId="device_456"), session)
    async with factory() as session:
        with pytest.raises(AccountBindingConflictError):
            await service.login(WechatLoginRequest(code="login-a", phoneCode="phone-b", deviceId="device_123"), session)
