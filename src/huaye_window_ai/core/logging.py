"""统一配置控制台、滚动文件日志和敏感字段脱敏。"""

from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from huaye_window_ai.core.config import Settings

SENSITIVE_KEYS = {
    "authorization",
    "access_token",
    "accesstoken",
    "app_secret",
    "appsecret",
    "code",
    "phone",
    "phonecode",
    "session_key",
    "token",
}


def redact(value: Any) -> Any:
    """递归清除日志对象里的凭证和个人信息，避免调用方遗漏单个字段。"""

    if isinstance(value, dict):
        return {
            key: "[REDACTED]" if key.lower().replace("-", "_") in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    return value


class _BaseFormatter(logging.Formatter):
    """为两种输出格式提供一致的服务、环境和请求上下文。"""

    def __init__(self, service: str, environment: str) -> None:
        super().__init__()
        self.service = service
        self.environment = environment

    def payload(self, record: logging.LogRecord) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "service": self.service,
            "environment": self.environment,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["requestId"] = request_id
        fields = getattr(record, "fields", None)
        if fields:
            payload["fields"] = redact(fields)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return payload


class JsonFormatter(_BaseFormatter):
    """输出单行 JSON，供文件检索、采集器和后续日志平台使用。"""

    def format(self, record: logging.LogRecord) -> str:
        return json.dumps(self.payload(record), ensure_ascii=False, separators=(",", ":"))


class PlainFormatter(_BaseFormatter):
    """输出适合本地开发阅读的单行文本，同时保留结构化扩展字段。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = self.payload(record)
        request_id = payload.get("requestId", "-")
        fields = payload.get("fields")
        suffix = f" | {json.dumps(fields, ensure_ascii=False, separators=(',', ':'))}" if fields else ""
        exception = f"\n{payload['exception']}" if payload.get("exception") else ""
        timestamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
        return (
            f"{timestamp} | {record.levelname:<8} | {self.service} | {request_id} | "
            f"{record.name} - {record.getMessage()}{suffix}{exception}"
        )


def _formatter(kind: Literal["plain", "json"], config: Settings) -> logging.Formatter:
    formatter_type = JsonFormatter if kind == "json" else PlainFormatter
    return formatter_type(config.app_name, config.app_env)


def _close_handlers(logger: logging.Logger) -> None:
    """重新配置时关闭旧文件句柄，避免 PyCharm 热重启后日志文件被 Windows 锁定。"""

    for handler in logger.handlers[:]:
        logger.removeHandler(handler)
        handler.flush()
        handler.close()


def configure_logging(config: Settings) -> None:
    """统一接管应用、FastAPI 与 Uvicorn 日志，并按配置启用滚动文件。"""

    root = logging.getLogger()
    _close_handlers(root)
    root.setLevel(config.app_log_level.upper())

    if config.log_console_enabled:
        console = logging.StreamHandler(sys.stdout)
        console.setFormatter(_formatter(config.log_console_format, config))
        root.addHandler(console)

    if config.log_file_enabled:
        # 日志目录在初始化阶段创建；业务代码无需关注目录是否存在。
        config.log_directory.mkdir(parents=True, exist_ok=True)
        application_file = RotatingFileHandler(
            config.log_directory / "application.log",
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backup_count,
            encoding="utf-8",
        )
        application_file.setFormatter(_formatter(config.log_file_format, config))
        root.addHandler(application_file)

        error_file = RotatingFileHandler(
            config.log_directory / "error.log",
            maxBytes=config.log_max_bytes,
            backupCount=config.log_backup_count,
            encoding="utf-8",
        )
        error_file.setLevel(logging.WARNING)
        error_file.setFormatter(_formatter(config.log_file_format, config))
        root.addHandler(error_file)

    if not root.handlers:
        root.addHandler(logging.NullHandler())

    # Uvicorn 默认持有自己的 handler；转交 root 后控制台和文件才能使用同一格式且不重复输出。
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        framework_logger = logging.getLogger(logger_name)
        _close_handlers(framework_logger)
        framework_logger.propagate = True
    # HTTP 请求由 RequestIdMiddleware 输出结构化日志，关闭 Uvicorn 的重复 access 行。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # httpx 的 INFO 日志可能包含 code2Session 查询串中的 AppSecret，必须强制关闭。
    logging.getLogger("httpx").setLevel(logging.WARNING)


def shutdown_logging() -> None:
    """刷新并关闭应用日志句柄，主要供测试和受控进程退出使用。"""

    _close_handlers(logging.getLogger())
