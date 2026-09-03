"""测试配置与密钥夹具。"""

from __future__ import annotations

import os

import pytest

# 全局 ASGI 应用在导入时校验必要配置；测试环境只注入不会覆盖开发者本地环境的占位值。
os.environ.setdefault("WECHAT_APP_ID", "wx_test_app")
os.environ.setdefault("WECHAT_APP_SECRET", "test-secret")

from huaye_window_ai.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        app_startup_checks_enabled=False,
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://unused/0",
        log_console_enabled=False,
        log_file_enabled=False,
        wechat_app_id="wx_test_app",
        wechat_app_secret="test-secret",
    )
