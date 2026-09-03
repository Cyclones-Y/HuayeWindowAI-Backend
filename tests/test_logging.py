"""Loguru 日志接管、脱敏和按日期落盘测试。"""

import json
import logging
from io import StringIO
from pathlib import Path

from loguru import logger
from pytest import MonkeyPatch

from huaye_window_ai.core import logging as logging_module
from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.logging import configure_logging, redact, sanitize_text, shutdown_logging


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


def test_sanitizes_unstructured_credentials() -> None:
    value = "request authorization=Bearer abc.def and app_secret='wechat-secret'"
    sanitized = sanitize_text(value)
    assert "abc.def" not in sanitized
    assert "wechat-secret" not in sanitized
    assert sanitized.count("[REDACTED]") == 2


def _settings(tmp_path: Path, *, file_format: str = "json") -> Settings:
    """构造不依赖真实 MySQL、Redis 和微信服务的日志配置。"""

    return Settings(
        _env_file=None,
        app_env="test",
        database_url="sqlite+aiosqlite:///:memory:",
        redis_url="redis://unused/0",
        log_console_enabled=False,
        log_file_enabled=True,
        log_file_format=file_format,
        log_directory=tmp_path,
        log_compression="zip",
        wechat_app_id="wx_test",
        wechat_app_secret="secret",
    )


def test_intercepts_logging_and_writes_dated_json_files(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path))
    try:
        # 标准 logging 代表 Uvicorn/FastAPI 等第三方库，验证它也经过同一套 Loguru sink。
        logging.getLogger("test.runtime").warning(
            "runtime warning",
            extra={"request_id": "req_test123", "fields": {"token": "secret-token", "statusCode": 503}},
        )
    finally:
        shutdown_logging()

    info_files = list(tmp_path.rglob("info.log"))
    error_files = list(tmp_path.rglob("error.log"))
    # Loguru 在注册 sink 时创建目标文件；WARNING 不会向 info.log 写入内容。
    assert len(info_files) == 1
    assert info_files[0].read_text(encoding="utf-8") == ""
    assert len(error_files) == 1

    payload = json.loads(error_files[0].read_text(encoding="utf-8"))
    assert payload["request_id"] == "req_test123"
    assert payload["extra"]["fields"] == {"token": "[REDACTED]", "statusCode": 503}
    assert payload["message"] == "runtime warning"


def test_info_and_warning_are_split_into_different_files(tmp_path: Path) -> None:
    configure_logging(_settings(tmp_path, file_format="plain"))
    try:
        logger.bind(request_id="req_info", fields={"statusCode": 200}).info("request completed")
        logger.bind(request_id="req_error").error("request failed")
    finally:
        shutdown_logging()

    info_text = next(tmp_path.rglob("info.log")).read_text(encoding="utf-8")
    error_text = next(tmp_path.rglob("error.log")).read_text(encoding="utf-8")
    assert "request completed" in info_text
    assert "request failed" not in info_text
    assert "request failed" in error_text


def test_plain_console_matches_ruoyi_column_order(monkeypatch: MonkeyPatch, tmp_path: Path) -> None:
    """防止再次加入固定宽度环境列或在消息末尾拼接字段 JSON。"""

    output = StringIO()
    monkeypatch.setattr(logging_module.sys, "stdout", output)
    settings = _settings(tmp_path, file_format="plain").model_copy(
        update={"log_console_enabled": True, "log_file_enabled": False}
    )
    configure_logging(settings)
    try:
        with logger.contextualize(trace_id="trace-1", span_id="span-1", request_id="request-1"):
            logger.bind(fields={"statusCode": 200}).info("console message")
    finally:
        shutdown_logging()

    rendered = output.getvalue()
    assert " | trace-1 | span-1 | request-1 | " in rendered
    assert " | INFO     | " in rendered
    assert rendered.endswith(" - console message\n")
    assert '"statusCode"' not in rendered
