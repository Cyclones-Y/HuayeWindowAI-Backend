"""测试配置与密钥夹具。"""

from __future__ import annotations

import base64

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
        phone_encryption_key_b64=base64.b64encode(b"e" * 32).decode(),
        phone_lookup_hmac_key_b64=base64.b64encode(b"h" * 32).decode(),
    )
