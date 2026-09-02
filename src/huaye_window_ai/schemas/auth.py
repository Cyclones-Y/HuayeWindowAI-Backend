"""微信手机号登录的公开契约。"""

from __future__ import annotations

from pydantic import BaseModel, Field


class WechatLoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=256)
    phone_code: str | None = Field(default=None, alias="phoneCode", min_length=1, max_length=256)
    device_id: str = Field(alias="deviceId", min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._:-]+$")

    model_config = {"populate_by_name": True}


class LoginUser(BaseModel):
    id: str
    nickname: None = None
    avatar_url: None = Field(default=None, alias="avatarUrl")

    model_config = {"populate_by_name": True}


class WechatLoginData(BaseModel):
    access_token: str = Field(alias="accessToken")
    expires_in: int = Field(alias="expiresIn")
    is_new_user: bool = Field(alias="isNewUser")
    user: LoginUser

    model_config = {"populate_by_name": True}
