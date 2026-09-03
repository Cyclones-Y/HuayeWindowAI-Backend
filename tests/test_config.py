"""安全配置启动校验。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from huaye_window_ai.core.config import Settings


def test_rejects_invalid_wechat_app_id() -> None:
    with pytest.raises(ValidationError, match="WECHAT_APP_ID"):
        Settings(
            app_env="test",
            wechat_app_id="invalid",
            wechat_app_secret="secret",
        )


def test_production_disables_docs() -> None:
    with pytest.raises(ValidationError, match="关闭 OpenAPI"):
        Settings(
            app_env="production",
            app_docs_enabled=True,
            wechat_app_id="wx_test",
            wechat_app_secret="secret",
        )
