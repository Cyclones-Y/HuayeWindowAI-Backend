"""结构化日志和敏感字段脱敏。"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any

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


class JsonFormatter(logging.Formatter):
    """输出便于集中检索的单行 JSON，同时统一加入时间和日志级别。"""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        request_id = getattr(record, "request_id", None)
        if request_id:
            payload["requestId"] = request_id
        fields = getattr(record, "fields", None)
        if fields:
            payload["fields"] = redact(fields)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(level: str) -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())
    # httpx 的 INFO 日志会包含 code2Session 查询串中的 AppSecret，必须强制关闭。
    logging.getLogger("httpx").setLevel(logging.WARNING)
