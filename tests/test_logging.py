"""日志字段脱敏与滚动文件输出测试。"""

import json
import logging
from pathlib import Path

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.logging import configure_logging, redact, shutdown_logging


def test_redacts_nested_credentials_and_phone() -> None:
    value = {
        "code": "login-code",
        "nested": {"phoneCode": "phone-code", "phone": "13800138000"},
        "safe": "kept",
    }
    assert redact(value) == {
        "code": "[REDACTED]",
        "nested": {"phoneCode": "[REDACTED]", "phone": "[REDACTED]"},
        "safe": "kept",
    }


def test_writes_json_application_and_error_logs(tmp_path: Path) -> None:
    settings = Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://unused/0",
        log_console_enabled=False,
        log_file_enabled=True,
        log_directory=tmp_path,
        wechat_app_id="wx_test",
        wechat_app_secret="secret",
    )
    configure_logging(settings)
    try:
        logging.getLogger("test.runtime").warning(
            "runtime warning",
            extra={"request_id": "req_test123", "fields": {"token": "secret-token", "statusCode": 503}},
        )
    finally:
        shutdown_logging()

    application_payload = json.loads((tmp_path / "application.log").read_text(encoding="utf-8"))
    error_payload = json.loads((tmp_path / "error.log").read_text(encoding="utf-8"))
    assert application_payload["requestId"] == "req_test123"
    assert application_payload["fields"] == {"token": "[REDACTED]", "statusCode": 503}
    assert error_payload == application_payload
