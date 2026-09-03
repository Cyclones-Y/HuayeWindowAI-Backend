"""安全配置启动校验。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from huaye_window_ai.core import config as config_module
from huaye_window_ai.core.config import Settings


def test_default_env_files_are_in_project_root() -> None:
    """IDE 改变工作目录时仍需定位到仓库根目录下的基础和环境配置。"""

    project_root = Path(config_module.__file__).resolve().parents[3]
    expected = (project_root / ".env", project_root / f".env.{config_module._environment_name()}")
    assert Settings.model_config["env_file"] == expected


def test_builds_database_and_redis_urls_from_readable_fields() -> None:
    settings = Settings(
        _env_file=None,
        database_url=None,
        db_username="app user",
        db_password="p@ss",
        db_database="huaye_window_ai",
        redis_url=None,
        redis_username="cache user",
        redis_password="redis@pass",
        wechat_app_id="wx_test",
        wechat_app_secret="secret",
    )

    assert settings.resolved_database_url == (
        "mysql+asyncmy://app%20user:p%40ss@127.0.0.1:3306/huaye_window_ai?charset=utf8mb4"
    )
    assert settings.resolved_redis_url == "redis://cache%20user:redis%40pass@127.0.0.1:6379/0"


def test_resolves_relative_log_directory_from_project_root() -> None:
    settings = Settings(
        _env_file=None,
        log_directory="runtime-logs",
        wechat_app_id="wx_test",
        wechat_app_secret="secret",
    )

    expected = Path(config_module.__file__).resolve().parents[3] / "runtime-logs"
    assert settings.log_directory == expected


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
