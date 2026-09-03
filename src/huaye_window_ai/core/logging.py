"""基于 Loguru 的统一日志入口，并接管标准库、FastAPI 与 Uvicorn 日志。"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import traceback
from collections.abc import Mapping
from functools import partial
from types import FrameType
from typing import TYPE_CHECKING, Any

from loguru import logger

if TYPE_CHECKING:
    from loguru import Record

    from huaye_window_ai.core.config import Settings

# 这套格式沿用 RuoYi-Vue3-FastAPI 的“时间 + 链路 + 进程 + 级别 + 调用位置”布局，
# 同时把当前项目尚未使用的 trace/span 字段替换为环境，避免输出无意义占位列。
CONSOLE_FORMAT = (
    "<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | "
    "<cyan>{extra[environment]: <11}</cyan> | "
    "<yellow>{extra[request_id]: <20}</yellow> | "
    "<blue>{extra[worker_id]: <6}</blue> | "
    "<level>{level: <8}</level> | "
    "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - "
    "<level>{message}</level>{extra[fields_text]}{extra[sanitized_exception]}\n"
)
FILE_FORMAT = (
    "{time:YYYY-MM-DD HH:mm:ss.SSS} | {extra[environment]} | {extra[request_id]} | "
    "{extra[worker_id]} | {level: <8} | {name}:{function}:{line} - "
    "{message}{extra[fields_text]}{extra[sanitized_exception]}\n"
)

SENSITIVE_KEYS = {
    "authorization",
    "access_token",
    "accesstoken",
    "app_secret",
    "appsecret",
    "code",
    "cookie",
    "password",
    "phone",
    "phone_code",
    "phonecode",
    "session_key",
    "token",
}
_STANDARD_LOG_RECORD_KEYS = frozenset(logging.makeLogRecord({}).__dict__)
_BEARER_PATTERN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(authorization|access[_-]?token|app[_-]?secret|code|cookie|password|phone(?:[_-]?code)?|"
    r"session[_-]?key|token)(\s*[\"']?\s*[:=]\s*[\"']?)(Bearer\s+[A-Za-z0-9._~+/=-]+|[^\s,;&}\"']+)"
)


def _normalized_key(key: object) -> str:
    """统一不同命名风格，确保 phoneCode、phone-code 等字段都能命中规则。"""

    return str(key).strip().lower().replace("-", "_")


def redact(value: Any) -> Any:
    """递归清除结构化日志中的凭证和手机号，且不修改调用方传入的原对象。"""

    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _normalized_key(key) in SENSITIVE_KEYS else redact(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact(item) for item in value)
    if isinstance(value, set):
        # JSON sink 无法序列化 set，转换为列表后仍保留全部可检索内容。
        return [redact(item) for item in value]
    return value


def sanitize_text(value: object) -> str:
    """兜底清理非结构化消息中的常见认证信息，防止第三方异常把密钥写入文件。"""

    text = str(value)
    text = _SENSITIVE_VALUE_PATTERN.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", text)
    return _BEARER_PATTERN.sub("Bearer [REDACTED]", text)


def _patch_record(record: Record, *, service: str, environment: str, instance_id: str) -> None:
    """补齐所有 sink 依赖的上下文字段，并在格式化前统一脱敏。"""

    extra = record["extra"]
    extra.setdefault("service", service)
    extra.setdefault("environment", environment)
    extra.setdefault("instance_id", instance_id)
    extra.setdefault("worker_id", str(os.getpid()))
    extra.setdefault("request_id", "-")

    fields = extra.get("fields")
    sanitized_fields = redact(fields) if fields else None
    extra["fields"] = sanitized_fields
    extra["fields_text"] = (
        f" | {json.dumps(sanitized_fields, ensure_ascii=False, separators=(',', ':'), default=str)}"
        if sanitized_fields
        else ""
    )
    record["message"] = sanitize_text(record["message"])

    # Loguru 默认会在格式化后追加原始异常。这里先生成脱敏后的 traceback，再清空原异常，
    # 否则微信响应或数据库驱动异常中的连接信息可能绕开 message/fields 脱敏。
    exception = record["exception"]
    if exception is None:
        extra["sanitized_exception"] = ""
        return
    rendered = "".join(traceback.format_exception(exception.type, exception.value, exception.traceback))
    extra["sanitized_exception"] = f"\n{sanitize_text(rendered).rstrip()}"
    record["exception"] = None


class InterceptHandler(logging.Handler):
    """把标准 logging 记录转发给 Loguru，让依赖库与业务代码只有一套输出格式。"""

    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame: FrameType | None = logging.currentframe()
        depth = 0
        # 先越过当前 InterceptHandler，再越过 logging 包内部调用栈，使 name/function/line
        # 指向真正产生日志的 Uvicorn、SQLAlchemy 或业务代码，而不是 logging.callHandlers。
        while frame is not None and (depth == 0 or frame.f_code.co_filename == logging.__file__):
            frame = frame.f_back
            depth += 1

        # logging 的 extra 字段不会自动进入 Loguru；只转发调用方自定义字段，避免污染输出。
        custom = {
            key: value
            for key, value in record.__dict__.items()
            if key not in _STANDARD_LOG_RECORD_KEYS and not key.startswith("_")
        }
        logger.bind(**custom).opt(depth=depth, exception=record.exc_info).log(level, record.getMessage())


def _configure_standard_logging(level: str, *, sql_echo: bool) -> None:
    """接管 Uvicorn/FastAPI/SQLAlchemy 等仍使用标准 logging 的组件。"""

    handler = InterceptHandler()
    logging.basicConfig(handlers=[handler], level=0, force=True)
    for logger_name in ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi"):
        framework_logger = logging.getLogger(logger_name)
        framework_logger.handlers = [handler]
        framework_logger.propagate = False
        framework_logger.setLevel(level)

    # 请求访问日志由中间件统一记录，禁用 Uvicorn 自带的重复访问行。
    logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
    # SQLAlchemy 的 INFO 会输出连接池细节；仅在显式打开 DB_ECHO 时启用，日常运行保持安静。
    logging.getLogger("sqlalchemy").setLevel(logging.INFO if sql_echo else logging.WARNING)
    # httpx INFO 记录可能带有微信 code2Session 查询串，其中包含 AppSecret。
    logging.getLogger("httpx").setLevel(logging.WARNING)


def configure_logging(config: Settings) -> None:
    """按照若依风格配置彩色控制台及按日期分目录的 info/error 文件。"""

    # PyCharm 热重启或测试重复创建应用时会再次配置；先冲刷队列并移除旧 sink，避免重复日志。
    logger.complete()
    logger.remove()
    logger.configure(
        patcher=partial(
            _patch_record,
            service=config.app_name,
            environment=config.app_env,
            instance_id=config.log_instance_id,
        )
    )

    common_options: dict[str, Any] = {
        "level": config.app_log_level,
        "enqueue": True,
        "backtrace": True,
        "diagnose": False,
    }
    if config.log_console_enabled:
        logger.add(
            sys.stdout,
            format=CONSOLE_FORMAT,
            colorize=config.log_console_format == "plain",
            serialize=config.log_console_format == "json",
            **common_options,
        )

    if config.log_file_enabled:
        config.log_directory.mkdir(parents=True, exist_ok=True)
        dated_directory = config.log_directory / "{time:YYYY}" / "{time:MM}" / "{time:DD}"
        file_options: dict[str, Any] = {
            **common_options,
            "format": FILE_FORMAT,
            "encoding": "utf-8",
            "serialize": config.log_file_format == "json",
            "rotation": config.log_rotation,
            "retention": config.log_retention,
            "compression": config.log_compression,
        }
        logger.add(
            str(dated_directory / "info.log"),
            filter=lambda record: record["level"].name == "INFO",
            **file_options,
        )
        logger.add(
            str(dated_directory / "error.log"),
            filter=lambda record: record["level"].no >= logging.WARNING,
            **file_options,
        )

    _configure_standard_logging(config.app_log_level, sql_echo=config.db_echo)


def shutdown_logging() -> None:
    """等待异步日志队列写完并释放文件句柄，供测试和受控进程退出使用。"""

    logger.complete()
    logger.remove()
