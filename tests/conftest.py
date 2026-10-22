"""测试配置与密钥夹具。"""

from __future__ import annotations

import os

import pytest

# 全局 ASGI 应用在导入时校验配置；测试进程固定 test，避免调用者的生产选择泄漏真实配置。
os.environ["APP_ENV"] = "test"
os.environ.setdefault("WECHAT_APP_ID", "wx_test_app")
os.environ.setdefault("WECHAT_APP_SECRET", "test-secret")

from huaye_window_ai.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        _env_file=None,
        app_env="test",
        app_startup_checks_enabled=False,
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://unused/0",
        log_console_enabled=False,
        log_file_enabled=False,
        wechat_app_id="wx_test_app",
        wechat_app_secret="test-secret",
    )
