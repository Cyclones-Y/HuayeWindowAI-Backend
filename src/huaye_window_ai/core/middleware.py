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
        # contextualize 使用 contextvars 隔离并发请求；请求内任意模块的日志都会自动携带 requestId。
        with logger.contextualize(request_id=request_id):
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
                logger.bind(
                    fields={
                        "method": request.method,
                        "path": request.url.path,
                        "statusCode": response.status_code,
                        "durationMs": round((time.perf_counter() - started_at) * 1000, 2),
                    }
                ).info("HTTP 请求完成")
            return response
