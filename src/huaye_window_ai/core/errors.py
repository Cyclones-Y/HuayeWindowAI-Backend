"""面向前端的稳定错误类型，隔离第三方服务和内部异常细节。"""

from __future__ import annotations

from typing import Any


class AppError(Exception):
    """所有可安全返回给客户端的业务错误基类。"""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int,
        data: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.data = data


class PhoneAuthRequiredError(AppError):
    def __init__(self) -> None:
        super().__init__("PHONE_AUTH_REQUIRED", "请授权手机号后继续", status_code=428)


class AccountBindingConflictError(AppError):
    def __init__(self) -> None:
        super().__init__("ACCOUNT_BINDING_CONFLICT", "当前微信身份与手机号账号不一致", status_code=409)


class WechatCodeInvalidError(AppError):
    def __init__(self) -> None:
        super().__init__("WECHAT_CODE_INVALID", "微信登录凭证已失效，请重试", status_code=401)


class WechatServiceUnavailableError(AppError):
    def __init__(self) -> None:
        super().__init__("WECHAT_SERVICE_UNAVAILABLE", "微信服务暂时不可用，请稍后重试", status_code=503)


class RateLimitedError(AppError):
    def __init__(self) -> None:
        super().__init__("RATE_LIMITED", "操作过于频繁，请稍后重试", status_code=429)
