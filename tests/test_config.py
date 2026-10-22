"""安全配置启动校验。"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from huaye_window_ai.core import config as config_module
from huaye_window_ai.core.config import DatabaseSettings, Settings


def isolate_environment_files(monkeypatch: pytest.MonkeyPatch, root: Path, environment: str) -> None:
    """配置根目录与选择变量均隔离，测试不得读取开发者的真实凭据。"""
    monkeypatch.setattr(config_module, "_PROJECT_ROOT", root)
    for key in ("APP_ENV", "DATABASE_URL", "DB_HOST", "DB_PASSWORD", "RENDER_QUEUE_MAX_WAIT_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("APP_ENV", environment)


def write_environment_file(root: Path, environment: str, host: str) -> None:
    """合成配置仅用于验证选择规则，生产约束使用测试地址和密钥，不连接任何服务。"""
    (root / f".env.{environment}").write_text(
        f"APP_ENV={environment}\nDB_HOST={host}\nWECHAT_APP_ID=wx_test\nWECHAT_APP_SECRET=test-secret\n"
        "APP_DOCS_ENABLED=false\nAPI_PUBLIC_BASE_URL=https://config.example.test\n"
        "PHOTO_URL_SIGNING_KEY=configuration-test-signing-key-at-least-32\n",
        encoding="utf-8",
    )


def test_environment_name_does_not_read_base_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """基础 .env 中的 APP_ENV 不得偷偷把默认本地启动切到另一个环境。"""
    isolate_environment_files(monkeypatch, tmp_path, "development")
    monkeypatch.delenv("APP_ENV")
    (tmp_path / ".env").write_text("APP_ENV=production\n", encoding="utf-8")
    assert config_module._environment_name() == "development"


@pytest.mark.parametrize("environment", ["development", "production"])
def test_only_selected_environment_file_is_loaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, environment: str
) -> None:
    """两个环境均独立，不允许旧基础文件或另一环境补齐缺少的密码与业务配置。"""
    isolate_environment_files(monkeypatch, tmp_path, environment)
    (tmp_path / ".env").write_text("DB_PASSWORD=forbidden-base-secret\nRENDER_QUEUE_MAX_WAIT_SECONDS=1234\n")
    write_environment_file(tmp_path, "development", "development-db")
    write_environment_file(tmp_path, "production", "production-db")
    settings = Settings()
    assert settings.app_env == environment
    assert settings.db_host == f"{environment}-db"
    assert settings.db_password.get_secret_value() == "replace_me"
    assert settings.render_queue_max_wait_seconds == 900
    assert DatabaseSettings().db_host == f"{environment}-db"


def test_environment_selection_is_resolved_for_each_new_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """API、迁移和种子脚本使用相同规则，选择不应在模块首次导入时就被永久固定。"""
    isolate_environment_files(monkeypatch, tmp_path, "development")
    write_environment_file(tmp_path, "development", "development-db")
    write_environment_file(tmp_path, "production", "production-db")
    assert DatabaseSettings().db_host == "development-db"
    monkeypatch.setenv("APP_ENV", "production")
    assert DatabaseSettings().db_host == "production-db"
    assert Settings().app_env == "production"


def test_missing_selected_file_never_falls_back_to_development(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """生产文件缺失时明确报错，防止迁移脚本静默连接默认开发库。"""
    isolate_environment_files(monkeypatch, tmp_path, "production")
    write_environment_file(tmp_path, "development", "development-db")
    with pytest.raises(ValueError, match="未找到.*env.production"):
        DatabaseSettings()


@pytest.mark.parametrize("environment", ["prod", "../production", ""])
def test_invalid_environment_is_rejected(monkeypatch: pytest.MonkeyPatch, environment: str) -> None:
    """拼错环境名称必须失败，不能生成任意路径或回落到默认开发配置。"""
    monkeypatch.setenv("APP_ENV", environment)
    with pytest.raises(ValueError, match="APP_ENV"):
        config_module._environment_name()


def test_runtime_environment_variables_work_without_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """容器通过 env_file 注入进程变量，镜像中无需再保存配置或密钥文件。"""
    isolate_environment_files(monkeypatch, tmp_path, "production")
    monkeypatch.setenv("DATABASE_URL", "mysql+asyncmy://test:fake@injected-db/application")
    monkeypatch.setenv("APP_DOCS_ENABLED", "false")
    monkeypatch.setenv("API_PUBLIC_BASE_URL", "https://injected.example.test")
    monkeypatch.setenv("PHOTO_URL_SIGNING_KEY", "configuration-test-signing-key-at-least-32")
    settings = Settings()
    assert settings.app_env == "production"
    assert "injected-db" in settings.resolved_database_url


def test_process_variables_override_selected_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """部署平台可覆盖文件内的连接地址，应用与迁移必须使用相同的最终值。"""
    isolate_environment_files(monkeypatch, tmp_path, "development")
    write_environment_file(tmp_path, "development", "file-db")
    monkeypatch.setenv("DB_HOST", "injected-db")
    assert Settings().db_host == "injected-db"
    assert DatabaseSettings().db_host == "injected-db"


def test_default_development_file_is_independent_of_working_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """IDE 从源码子目录启动时仍需定位到项目根目录的开发配置。"""
    isolate_environment_files(monkeypatch, tmp_path, "development")
    write_environment_file(tmp_path, "development", "project-root-db")
    monkeypatch.delenv("APP_ENV")
    nested = tmp_path / "src"
    nested.mkdir()
    monkeypatch.chdir(nested)
    assert Settings().db_host == "project-root-db"


def test_test_environment_does_not_load_any_environment_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """自动化测试即使遗留 .env.test，也只能使用显式参数和进程变量。"""
    isolate_environment_files(monkeypatch, tmp_path, "test")
    write_environment_file(tmp_path, "test", "forbidden-file-db")
    write_environment_file(tmp_path, "development", "forbidden-development-db")
    assert DatabaseSettings().db_host == "127.0.0.1"


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
