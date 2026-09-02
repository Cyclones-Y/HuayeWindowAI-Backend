"""安全配置启动校验。"""

from __future__ import annotations

import base64

import pytest
from pydantic import ValidationError

from huaye_window_ai.core.config import Settings


def test_rejects_reused_phone_keys() -> None:
    key = base64.b64encode(b"x" * 32).decode()
    with pytest.raises(ValidationError, match="不得复用"):
        Settings(
            app_env="test",
            wechat_app_id="wx_test",
            wechat_app_secret="secret",
            phone_encryption_key_b64=key,
            phone_lookup_hmac_key_b64=key,
        )


def test_production_disables_docs() -> None:
    with pytest.raises(ValidationError, match="关闭 OpenAPI"):
        Settings(
            app_env="production",
            app_docs_enabled=True,
            wechat_app_id="wx_test",
            wechat_app_secret="secret",
            phone_encryption_key_b64=base64.b64encode(b"e" * 32).decode(),
            phone_lookup_hmac_key_b64=base64.b64encode(b"h" * 32).decode(),
        )
