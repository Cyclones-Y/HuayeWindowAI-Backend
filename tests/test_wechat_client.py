"""微信协议适配器测试，真实密钥和临时码不会参与自动化测试。"""

from __future__ import annotations

import httpx
import pytest
import respx
from fakeredis.aioredis import FakeRedis

from huaye_window_ai.core.errors import WechatCodeInvalidError
from huaye_window_ai.integrations.wechat import CODE2SESSION_URL, PHONE_NUMBER_URL, STABLE_TOKEN_URL, WechatClient


async def test_exchanges_identity_and_phone_without_returning_session_key() -> None:
    redis = FakeRedis()
    async with httpx.AsyncClient() as http:
        client = WechatClient(http, redis, app_id="wx_test", app_secret="secret")
        with respx.mock(assert_all_called=True) as router:
            router.get(CODE2SESSION_URL).mock(
                return_value=httpx.Response(
                    200, json={"openid": "open_1", "unionid": "union_1", "session_key": "hidden"}
                )
            )
            router.post(STABLE_TOKEN_URL).mock(
                return_value=httpx.Response(200, json={"access_token": "global-token", "expires_in": 7200})
            )
            router.post(PHONE_NUMBER_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errcode": 0,
                        "phone_info": {
                            "purePhoneNumber": "13800138000",
                            "countryCode": "86",
                            "watermark": {"appid": "wx_test"},
                        },
                    },
                )
            )

            identity = await client.code_to_identity("login-code")
            phone = await client.get_phone_number("phone-code")

    assert identity.open_id == "open_1"
    assert not hasattr(identity, "session_key")
    assert phone.pure_phone_number == "13800138000"
    assert await redis.get("wechat:access-token:wx_test") == b"global-token"


async def test_rejects_invalid_login_code() -> None:
    redis = FakeRedis()
    async with httpx.AsyncClient() as http:
        client = WechatClient(http, redis, app_id="wx_test", app_secret="secret")
        with respx.mock:
            respx.get(CODE2SESSION_URL).mock(return_value=httpx.Response(200, json={"errcode": 40029}))
            with pytest.raises(WechatCodeInvalidError):
                await client.code_to_identity("expired")


async def test_rejects_phone_watermark_for_another_app() -> None:
    redis = FakeRedis()
    await redis.set("wechat:access-token:wx_test", "global-token")
    async with httpx.AsyncClient() as http:
        client = WechatClient(http, redis, app_id="wx_test", app_secret="secret")
        with respx.mock:
            respx.post(PHONE_NUMBER_URL).mock(
                return_value=httpx.Response(
                    200,
                    json={
                        "errcode": 0,
                        "phone_info": {
                            "purePhoneNumber": "13800138000",
                            "countryCode": "86",
                            "watermark": {"appid": "wx_other"},
                        },
                    },
                )
            )
            with pytest.raises(WechatCodeInvalidError):
                await client.get_phone_number("phone-code")
