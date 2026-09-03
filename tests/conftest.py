"""测试配置与密钥夹具。"""

from __future__ import annotations

import pytest

from huaye_window_ai.core.config import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://unused/0",
        wechat_app_id="wx_test_app",
        wechat_app_secret="test-secret",
    )
