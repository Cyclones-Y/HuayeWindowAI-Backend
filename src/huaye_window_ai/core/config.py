"""从环境变量加载配置，并在启动阶段校验关键运行参数。"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal, Self

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# IDE 直接运行源码时，当前工作目录可能落在 src/huaye_window_ai；配置文件应始终从项目根目录读取。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = _PROJECT_ROOT / ".env"


class DatabaseSettings(BaseSettings):
    """Alembic 和应用共享的数据库最小配置。"""

    model_config = SettingsConfigDict(env_file=_ENV_FILE, env_file_encoding="utf-8", extra="ignore")

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

    session_ttl_seconds: int = Field(default=7200, ge=300, le=604800)
    login_ip_limit_per_minute: int = Field(default=30, ge=1, le=10000)
    login_device_limit_per_minute: int = Field(default=10, ge=1, le=10000)
    wechat_http_timeout_seconds: float = Field(default=5.0, ge=1.0, le=30.0)

    @field_validator("wechat_app_id")
    @classmethod
    def validate_wechat_app_id(cls, value: str) -> str:
        if not value.startswith("wx"):
            raise ValueError("WECHAT_APP_ID 格式无效")
        return value

    @model_validator(mode="after")
    def validate_production_docs(self) -> Self:
        if self.app_env == "production" and self.app_docs_enabled:
            raise ValueError("生产环境默认必须关闭 OpenAPI 文档")
        return self


@lru_cache
def get_settings() -> Settings:
    """进程内只解析一次环境配置。"""

    return Settings()
