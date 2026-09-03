"""为每次请求生成可回传和检索的链路标识。"""

from __future__ import annotations

import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
QUIET_SUCCESS_PATHS = frozenset({"/docs", "/openapi.json", "/health/live"})


class RequestIdMiddleware(BaseHTTPMiddleware):
    """建立请求链路标识，并输出不包含查询串与请求体的访问日志。"""

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        supplied = request.headers.get("X-Request-Id", "")
        request_id = supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else f"req_{uuid.uuid4().hex}"
        request.state.request_id = request_id
        started_at = time.perf_counter()
        # 当前是单体服务，只保留能贯穿响应和日志的 requestId；trace/span 等到拆分服务后再启用。
        with logger.contextualize(
            request_id=request_id,
            method=request.method,
            path=request.url.path,
        ):
            try:
                response = await call_next(request)
            except Exception:
                duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                logger.bind(
                    fields={"method": request.method, "path": request.url.path, "durationMs": duration_ms}
                ).exception("HTTP 请求失败")
                raise
            response.headers["X-Request-Id"] = request_id
            settings = getattr(request.app.state, "settings", None)
            if settings is None or settings.log_access_enabled:
                duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
                # 文档与存活探针的成功访问没有排障价值；失败时仍会完整记录。
                if response.status_code < 400 and request.url.path in QUIET_SUCCESS_PATHS:
                    return response
                status_color = (
                    "green" if response.status_code < 400 else "yellow" if response.status_code < 500 else "red"
                )
                error_code = getattr(request.state, "access_error_code", None)
                error_message = getattr(request.state, "access_error_message", None)
                reason_text = (
                    f" | <yellow>{error_code}</yellow> | {error_message}" if error_code and error_message else ""
                )
                logger.bind(
                    fields={
                        "method": request.method,
                        "path": request.url.path,
                        "statusCode": response.status_code,
                        "durationMs": duration_ms,
                        "errorCode": error_code,
                        "errorMessage": error_message,
                    }
                ).opt(colors=True).info(
                    f"📨 <cyan>{request.method}</cyan> <blue>{request.url.path}</blue> | "
                    f"<{status_color}>{response.status_code}</{status_color}>{reason_text} | {duration_ms:.2f} ms"
                )
            return response
