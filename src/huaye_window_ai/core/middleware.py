"""为每次请求生成可回传和检索的链路标识。"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
access_logger = logging.getLogger("huaye_window_ai.access")


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
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            access_logger.exception(
                "HTTP 请求失败",
                extra={
                    "request_id": request_id,
                    "fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "durationMs": duration_ms,
                    },
                },
            )
            raise
        response.headers["X-Request-Id"] = request_id
        settings = getattr(request.app.state, "settings", None)
        if settings is None or settings.log_access_enabled:
            access_logger.info(
                "HTTP 请求完成",
                extra={
                    "request_id": request_id,
                    "fields": {
                        "method": request.method,
                        "path": request.url.path,
                        "statusCode": response.status_code,
                        "durationMs": round((time.perf_counter() - started_at) * 1000, 2),
                    },
                },
            )
        return response
