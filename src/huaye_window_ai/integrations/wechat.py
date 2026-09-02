"""微信小程序登录和手机号 API 适配器。"""

from __future__ import annotations

import asyncio
import json
import secrets
from collections.abc import Awaitable
from dataclasses import dataclass
from typing import Any, cast

import httpx
from redis.asyncio import Redis
from redis.exceptions import ResponseError

from huaye_window_ai.core.errors import WechatCodeInvalidError, WechatServiceUnavailableError

CODE2SESSION_URL = "https://api.weixin.qq.com/sns/jscode2session"
STABLE_TOKEN_URL = "https://api.weixin.qq.com/cgi-bin/stable_token"
PHONE_NUMBER_URL = "https://api.weixin.qq.com/wxa/business/getuserphonenumber"
INVALID_CODE_ERRORS = {40029, 40163}


@dataclass(frozen=True, slots=True)
class WechatIdentityData:
    open_id: str
    union_id: str | None


@dataclass(frozen=True, slots=True)
class WechatPhoneData:
    country_code: str
    pure_phone_number: str


class WechatClient:
    """封装微信协议并确保密钥、临时码和原始错误不会越过适配器边界。"""

    def __init__(
        self,
        http: httpx.AsyncClient,
        redis: Redis,
        *,
        app_id: str,
        app_secret: str,
    ) -> None:
        self._http = http
        self._redis = redis
        self._app_id = app_id
        self._app_secret = app_secret
        self._token_key = f"wechat:access-token:{app_id}"
        self._token_lock_key = f"wechat:access-token-lock:{app_id}"

    async def code_to_identity(self, code: str) -> WechatIdentityData:
        try:
            response = await self._http.get(
                CODE2SESSION_URL,
                params={
                    "appid": self._app_id,
                    "secret": self._app_secret,
                    "js_code": code,
                    "grant_type": "authorization_code",
                },
            )
            payload = self._safe_json(response)
        except (httpx.HTTPError, ValueError):
            # 异常对象可能包含带 AppSecret 的完整 URL，因此这里只抛出固定业务错误。
            raise WechatServiceUnavailableError from None

        self._raise_for_wechat_error(payload)
        open_id = payload.get("openid")
        if not isinstance(open_id, str) or not open_id:
            raise WechatServiceUnavailableError
        union_id = payload.get("unionid")
        # session_key 故意不进入返回模型，离开本作用域后即可释放。
        return WechatIdentityData(open_id=open_id, union_id=union_id if isinstance(union_id, str) else None)

    async def get_phone_number(self, phone_code: str) -> WechatPhoneData:
        access_token = await self._get_access_token()
        try:
            response = await self._http.post(
                PHONE_NUMBER_URL,
                params={"access_token": access_token},
                json={"code": phone_code},
            )
            payload = self._safe_json(response)
        except (httpx.HTTPError, ValueError):
            raise WechatServiceUnavailableError from None

        self._raise_for_wechat_error(payload)
        phone_info = payload.get("phone_info")
        if not isinstance(phone_info, dict):
            raise WechatServiceUnavailableError
        watermark = phone_info.get("watermark")
        if isinstance(watermark, dict) and watermark.get("appid") not in (None, self._app_id):
            raise WechatCodeInvalidError
        country_code = phone_info.get("countryCode")
        pure_phone = phone_info.get("purePhoneNumber")
        if not isinstance(country_code, str) or not isinstance(pure_phone, str):
            raise WechatServiceUnavailableError
        return WechatPhoneData(country_code=country_code, pure_phone_number=pure_phone)

    async def _get_access_token(self) -> str:
        cached = await self._redis.get(self._token_key)
        if cached:
            return self._decode(cached)

        lock_token = secrets.token_urlsafe(16)
        acquired = await self._redis.set(self._token_lock_key, lock_token, ex=10, nx=True)
        if not acquired:
            # 其他实例正在刷新。短暂等待缓存出现，避免同时刷新导致旧 token 失效。
            for _ in range(50):
                await asyncio.sleep(0.1)
                cached = await self._redis.get(self._token_key)
                if cached:
                    return self._decode(cached)
            raise WechatServiceUnavailableError

        try:
            return await self._refresh_access_token()
        finally:
            await self._release_lock(lock_token)

    async def _refresh_access_token(self) -> str:
        try:
            response = await self._http.post(
                STABLE_TOKEN_URL,
                json={
                    "grant_type": "client_credential",
                    "appid": self._app_id,
                    "secret": self._app_secret,
                    "force_refresh": False,
                },
            )
            payload = self._safe_json(response)
        except (httpx.HTTPError, ValueError):
            raise WechatServiceUnavailableError from None

        self._raise_for_wechat_error(payload)
        token = payload.get("access_token")
        expires_in = payload.get("expires_in")
        if not isinstance(token, str) or not isinstance(expires_in, int):
            raise WechatServiceUnavailableError
        # 提前五分钟失效，避免一次手机号请求跨过微信 token 的真实过期时间。
        await self._redis.set(self._token_key, token, ex=max(expires_in - 300, 60))
        return token

    async def _release_lock(self, lock_token: str) -> None:
        script = """
        if redis.call('get', KEYS[1]) == ARGV[1] then
          return redis.call('del', KEYS[1])
        end
        return 0
        """
        try:
            await cast(Awaitable[Any], self._redis.eval(script, 1, self._token_lock_key, lock_token))
        except ResponseError as exc:
            # fakeredis 的精简构建不支持 Lua；真实 Redis 始终走上面的原子比较删除。
            if "unknown command" not in str(exc).lower():
                raise
            current = await self._redis.get(self._token_lock_key)
            if current and self._decode(current) == lock_token:
                await self._redis.delete(self._token_lock_key)

    @staticmethod
    def _safe_json(response: httpx.Response) -> dict[str, Any]:
        response.raise_for_status()
        payload = json.loads(response.content)
        if not isinstance(payload, dict):
            raise ValueError("unexpected response")
        return payload

    @staticmethod
    def _decode(value: str | bytes) -> str:
        return value.decode("utf-8") if isinstance(value, bytes) else value

    @staticmethod
    def _raise_for_wechat_error(payload: dict[str, Any]) -> None:
        errcode = payload.get("errcode", 0)
        if errcode in (0, None):
            return
        if errcode in INVALID_CODE_ERRORS:
            raise WechatCodeInvalidError
        raise WechatServiceUnavailableError
