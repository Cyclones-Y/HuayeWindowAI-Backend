"""从环境变量加载配置，并在启动阶段阻止弱密钥进入运行环境。"""

from __future__ import annotations

import base64
import binascii
from functools import lru_cache
from typing import Literal, Self

from pydantic import Field, SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class DatabaseSettings(BaseSettings):
    """Alembic 和应用共享的数据库最小配置。"""

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "mysql+asyncmy://huaye:replace_me@127.0.0.1:3306/huaye_window_ai?charset=utf8mb4"


class Settings(DatabaseSettings):
    """运行时配置；所有敏感配置只允许通过环境或未提交的 `.env` 注入。"""

    app_env: Literal["development", "test", "production"] = "development"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_log_level: str = "INFO"
    app_docs_enabled: bool = True

    redis_url: str = "redis://127.0.0.1:6379/0"
    wechat_app_id: str
    wechat_app_secret: SecretStr
    phone_encryption_key_b64: SecretStr
    phone_lookup_hmac_key_b64: SecretStr
    phone_key_version: int = Field(default=1, ge=1)

    session_ttl_seconds: int = Field(default=7200, ge=300, le=604800)
    login_ip_limit_per_minute: int = Field(default=30, ge=1, le=10000)
    login_device_limit_per_minute: int = Field(default=10, ge=1, le=10000)
    wechat_http_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)

    @staticmethod
    def _decode_32_byte_key(value: SecretStr, field_name: str) -> bytes:
        try:
            decoded = base64.b64decode(value.get_secret_value(), validate=True)
        except (binascii.Error, ValueError) as exc:
            raise ValueError(f"{field_name} 必须是合法 Base64") from exc
        if len(decoded) != 32:
            raise ValueError(f"{field_name} 解码后必须为 32 字节")
        return decoded

    @model_validator(mode="after")
    def validate_security_settings(self) -> Self:
        """密钥长度和用途隔离必须在进程接受请求前完成校验。"""

        encryption_key = self._decode_32_byte_key(self.phone_encryption_key_b64, "PHONE_ENCRYPTION_KEY_B64")
        lookup_key = self._decode_32_byte_key(self.phone_lookup_hmac_key_b64, "PHONE_LOOKUP_HMAC_KEY_B64")
        if encryption_key == lookup_key:
            raise ValueError("手机号加密密钥与检索 HMAC 密钥不得复用")
        if not self.wechat_app_id.startswith("wx"):
            raise ValueError("WECHAT_APP_ID 格式无效")
        if self.app_env == "production" and self.app_docs_enabled:
            raise ValueError("生产环境默认必须关闭 OpenAPI 文档")
        return self

    @property
    def phone_encryption_key(self) -> bytes:
        return self._decode_32_byte_key(self.phone_encryption_key_b64, "PHONE_ENCRYPTION_KEY_B64")

    @property
    def phone_lookup_hmac_key(self) -> bytes:
        return self._decode_32_byte_key(self.phone_lookup_hmac_key_b64, "PHONE_LOOKUP_HMAC_KEY_B64")


@lru_cache
def get_settings() -> Settings:
    """进程内只解析一次环境配置。"""

    return Settings()
