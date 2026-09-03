"""从环境变量加载配置，并在启动阶段校验关键运行参数。"""

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Literal, Self
from urllib.parse import quote

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# IDE 直接运行源码时，当前工作目录可能落在 src/huaye_window_ai；配置文件应始终从项目根目录读取。
_PROJECT_ROOT = Path(__file__).resolve().parents[3]
_ENV_FILE = _PROJECT_ROOT / ".env"


def _environment_name() -> str:
    """在创建 Settings 前确定配置层，兼容系统环境变量与基础 `.env`。"""

    if configured := os.getenv("APP_ENV"):
        return configured.strip().lower()
    try:
        for line in _ENV_FILE.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if separator and key.strip() == "APP_ENV":
                return value.strip().strip("'\"").lower()
    except OSError:
        pass
    return "development"


# 基础配置先加载，按环境命名的文件后加载并覆盖；不存在环境文件时 Pydantic 会自然跳过。
_ENV_FILES = (_ENV_FILE, _PROJECT_ROOT / f".env.{_environment_name()}")


class DatabaseSettings(BaseSettings):
    """Alembic 和应用共享的数据库配置，支持分项配置和完整 DSN 覆盖。"""

    model_config = SettingsConfigDict(env_file=_ENV_FILES, env_file_encoding="utf-8", extra="ignore")

    database_url: str | None = None
    db_host: str = "127.0.0.1"
    db_port: int = Field(default=3306, ge=1, le=65535)
    db_username: str = "huaye"
    db_password: SecretStr = SecretStr("replace_me")
    db_database: str = "huaye_window_ai"
    db_echo: bool = False

    @property
    def resolved_database_url(self) -> str:
        """优先使用完整 DSN；否则由可读的 DB_* 配置安全拼接连接地址。"""

        if self.database_url:
            return self.database_url
        username = quote(self.db_username, safe="")
        password = quote(self.db_password.get_secret_value(), safe="")
        database = quote(self.db_database, safe="")
        return f"mysql+asyncmy://{username}:{password}@{self.db_host}:{self.db_port}/{database}?charset=utf8mb4"


class Settings(DatabaseSettings):
    """运行时配置；所有敏感配置只允许通过环境或未提交的 `.env` 注入。"""

    app_env: Literal["development", "test", "production"] = "development"
    app_name: str = "华业门窗 AI 小程序后端"
    app_version: str = "0.1.0"
    app_host: str = "127.0.0.1"
    app_port: int = Field(default=8000, ge=1, le=65535)
    app_log_level: str = "INFO"
    app_docs_enabled: bool = True
    app_startup_checks_enabled: bool = True

    redis_url: str | None = None
    redis_host: str = "127.0.0.1"
    redis_port: int = Field(default=6379, ge=1, le=65535)
    redis_username: str = ""
    redis_password: SecretStr = SecretStr("")
    redis_database: int = Field(default=0, ge=0)

    log_console_enabled: bool = True
    log_console_format: Literal["plain", "json"] = "plain"
    log_file_enabled: bool = True
    log_file_format: Literal["plain", "json"] = "json"
    log_directory: Path = _PROJECT_ROOT / "logs"
    log_rotation: str = "50 MB"
    log_retention: str = "30 days"
    log_compression: str = "zip"
    log_instance_id: str = "default"
    log_access_enabled: bool = True

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

    @field_validator("app_log_level")
    @classmethod
    def validate_log_level(cls, value: str) -> str:
        level = value.upper()
        if level not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("APP_LOG_LEVEL 必须是 DEBUG、INFO、WARNING、ERROR 或 CRITICAL")
        return level

    @field_validator("log_directory")
    @classmethod
    def resolve_log_directory(cls, value: Path) -> Path:
        """相对日志目录始终基于项目根目录，避免 IDE 工作目录改变落盘位置。"""

        return value if value.is_absolute() else _PROJECT_ROOT / value

    @model_validator(mode="after")
    def validate_production_docs(self) -> Self:
        if self.app_env == "production" and self.app_docs_enabled:
            raise ValueError("生产环境默认必须关闭 OpenAPI 文档")
        return self

    @property
    def resolved_redis_url(self) -> str:
        """优先使用完整 URL；否则从 REDIS_* 字段生成连接地址。"""

        if self.redis_url:
            return self.redis_url
        username = quote(self.redis_username, safe="")
        password = quote(self.redis_password.get_secret_value(), safe="")
        credentials = ""
        if username or password:
            credentials = f"{username}:{password}@"
        return f"redis://{credentials}{self.redis_host}:{self.redis_port}/{self.redis_database}"


@lru_cache
def get_settings() -> Settings:
    """进程内只解析一次环境配置。"""

    return Settings()
