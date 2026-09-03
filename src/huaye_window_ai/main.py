"""FastAPI 应用工厂与资源生命周期。"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from redis.asyncio import Redis
from sqlalchemy import text

from huaye_window_ai.api.v1.router import router as v1_router
from huaye_window_ai.core.config import Settings, get_settings
from huaye_window_ai.core.errors import AppError
from huaye_window_ai.core.logging import configure_logging
from huaye_window_ai.core.middleware import RequestIdMiddleware
from huaye_window_ai.db.session import create_engine, create_session_factory
from huaye_window_ai.integrations.wechat import WechatClient
from huaye_window_ai.services.auth import AuthService
from huaye_window_ai.services.rate_limit import LoginRateLimiter
from huaye_window_ai.services.session import SessionStore

logger = logging.getLogger(__name__)


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "req_unknown")


def _error_payload(code: str, message: str, request_id: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data, "requestId": request_id}


def create_app(settings: Settings | None = None) -> FastAPI:
    """允许测试注入配置，生产入口仍只从环境变量读取。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        config = settings or get_settings()
        configure_logging(config.app_log_level)
        engine = create_engine(config.database_url)
        redis = Redis.from_url(config.redis_url, decode_responses=False)
        http = httpx.AsyncClient(timeout=config.wechat_http_timeout_seconds)
        session_store = SessionStore(redis, config.session_ttl_seconds)
        wechat = WechatClient(
            http,
            redis,
            app_id=config.wechat_app_id,
            app_secret=config.wechat_app_secret.get_secret_value(),
        )

        app.state.settings = config
        app.state.engine = engine
        app.state.session_factory = create_session_factory(engine)
        app.state.redis = redis
        app.state.http = http
        app.state.session_store = session_store
        app.state.login_rate_limiter = LoginRateLimiter(
            redis,
            config.login_ip_limit_per_minute,
            config.login_device_limit_per_minute,
        )
        app.state.auth_service = AuthService(
            wechat,
            session_store,
            app_id=config.wechat_app_id,
        )
        try:
            yield
        finally:
            await http.aclose()
            await redis.aclose()
            await engine.dispose()

    configured = settings
    docs_enabled = (
        configured.app_docs_enabled
        if configured
        else os.getenv("APP_DOCS_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
    )
    app = FastAPI(
        title="华业门窗 AI 小程序后端",
        version="0.1.0",
        docs_url="/docs" if docs_enabled else None,
        redoc_url=None,
        lifespan=lifespan,
    )
    app.add_middleware(RequestIdMiddleware)
    app.include_router(v1_router)

    @app.exception_handler(AppError)
    async def handle_app_error(request: Request, exc: AppError) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content=_error_payload(exc.code, exc.message, _request_id(request), exc.data),
        )

    @app.exception_handler(RequestValidationError)
    async def handle_validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        # 不回显 input，避免登录 code、手机号授权码等敏感请求值进入响应或日志。
        fields = [
            {"field": ".".join(str(part) for part in item["loc"] if part != "body"), "type": item["type"]}
            for item in exc.errors()
        ]
        return JSONResponse(
            status_code=400,
            content=_error_payload("INVALID_ARGUMENT", "请求参数无效", _request_id(request), {"fields": fields}),
        )

    @app.exception_handler(Exception)
    async def handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
        logger.error(
            "unhandled request error",
            extra={"request_id": _request_id(request), "fields": {"exceptionType": type(exc).__name__}},
        )
        return JSONResponse(
            status_code=500,
            content=_error_payload("INTERNAL_ERROR", "服务暂时不可用", _request_id(request)),
        )

    @app.get("/health/live", tags=["健康检查"])
    async def live(request: Request) -> dict[str, Any]:
        return {"status": "UP", "requestId": _request_id(request)}

    @app.get("/health/ready", tags=["健康检查"])
    async def ready(request: Request) -> JSONResponse:
        checks = {"mysql": False, "redis": False}
        try:
            async with request.app.state.engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
            checks["mysql"] = True
        except Exception:
            logger.warning(
                "readiness dependency check failed",
                extra={"request_id": _request_id(request), "fields": {"dependency": "mysql"}},
            )
        try:
            checks["redis"] = bool(await request.app.state.redis.ping())
        except Exception:
            # 各依赖必须独立检查，避免 MySQL 失败时错误地把健康 Redis 也标记为失败。
            logger.warning(
                "readiness dependency check failed",
                extra={"request_id": _request_id(request), "fields": {"dependency": "redis"}},
            )
        status_code = 200 if all(checks.values()) else 503
        return JSONResponse(
            status_code=status_code,
            content={
                "status": "UP" if status_code == 200 else "DOWN",
                "checks": checks,
                "requestId": _request_id(request),
            },
        )

    return app


app = create_app()


def main() -> None:
    """供 PyCharm 等 IDE 直接运行本文件时启动开发服务器。"""

    config = get_settings()
    # 直接传入应用对象，避免 Uvicorn 再次导入模块；PyCharm 调试断点也能落在当前进程中。
    uvicorn.run(
        app,
        host=config.app_host,
        port=config.app_port,
        log_level=config.app_log_level.lower(),
    )


if __name__ == "__main__":
    main()
