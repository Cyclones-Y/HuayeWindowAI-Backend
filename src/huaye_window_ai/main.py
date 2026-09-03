"""FastAPI 应用工厂与资源生命周期。"""

from __future__ import annotations

import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
import uvicorn
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from loguru import logger
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

_ALL_INTERFACES_HOST = "0.0.0.0"  # noqa: S104 - 仅用于识别用户明确配置的监听范围。


def _request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "req_unknown")


def _error_payload(code: str, message: str, request_id: str, data: Any = None) -> dict[str, Any]:
    return {"code": code, "message": message, "data": data, "requestId": request_id}


def _access_urls(config: Settings, path: str = "") -> list[str]:
    """生成本机和局域网访问地址，方便开发者工具与真机联调。"""

    if config.app_host != _ALL_INTERFACES_HOST:
        return [f"http://{config.app_host}:{config.app_port}{path}"]
    hosts = {"127.0.0.1"}
    try:
        hosts.update(
            str(item[4][0])
            for item in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET)
            if item[4][0] != _ALL_INTERFACES_HOST
        )
    except OSError:
        # 主机名解析失败不应阻止服务启动，本机地址仍可用于健康检查。
        pass
    return [f"http://{host}:{config.app_port}{path}" for host in sorted(hosts)]


def _log_address_group(title: str, urls: list[str]) -> None:
    """采用若依的多行地址展示方式，避免 URL 被塞进难读的 JSON 数组。"""

    if not urls:
        return
    lines = [f"🏠 Local:    <cyan>{urls[0]}</cyan>"]
    lines.extend(f"📡 Network:  <cyan>{url}</cyan>" for url in urls[1:])
    logger.opt(colors=True).info(f"{title}:\n" + "\n".join(lines))


def _show_startup_addresses(config: Settings) -> None:
    """集中显示应用和 Swagger 地址，与 RuoYi-Vue3-FastAPI 的启动摘要一致。"""

    _log_address_group("💻 应用地址", _access_urls(config))
    if config.app_docs_enabled:
        _log_address_group("📄 Swagger文档", _access_urls(config, "/docs"))


async def _check_dependencies(app: FastAPI, request_id: str) -> dict[str, bool]:
    """独立检查 MySQL 和 Redis，并记录不包含连接密钥的失败摘要。"""

    checks = {"mysql": False, "redis": False}
    try:
        async with app.state.engine.connect() as connection:
            await connection.execute(text("SELECT 1"))
        checks["mysql"] = True
    except Exception as exc:
        logger.bind(
            request_id=request_id,
            fields={"dependency": "mysql", "exceptionType": type(exc).__name__},
        ).warning("MySQL 依赖检查失败")
    try:
        checks["redis"] = bool(await app.state.redis.ping())
    except Exception as exc:
        logger.bind(
            request_id=request_id,
            fields={"dependency": "redis", "exceptionType": type(exc).__name__},
        ).warning("Redis 依赖检查失败")
    return checks


def create_app(settings: Settings | None = None) -> FastAPI:
    """允许测试注入配置，生产入口仍只从环境变量读取。"""

    config = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        configure_logging(config)
        logger.bind(
            startup_phase="application_startup",
            fields={
                "appName": config.app_name,
                "version": config.app_version,
                "environment": config.app_env,
                "host": config.app_host,
                "port": config.app_port,
            },
        ).info(f"⏰️ {config.app_name}开始启动")
        engine = create_engine(config.resolved_database_url, echo=config.db_echo)
        redis = Redis.from_url(config.resolved_redis_url, decode_responses=False)
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
            checks: dict[str, bool] | str = "skipped"
            if config.app_startup_checks_enabled:
                checks = await _check_dependencies(app, "startup")
            dependencies_ready = isinstance(checks, str) or all(checks.values())
            startup_logger = logger.bind(startup_phase="application_startup", fields={"dependencies": checks})
            if dependencies_ready:
                startup_logger.info(f"🚀 {config.app_name}启动成功")
            else:
                startup_logger.warning(f"⚠️ {config.app_name}已启动，但依赖检查未通过")
            _show_startup_addresses(config)
            if config.log_file_enabled:
                logger.opt(colors=True).info(f"📁 日志目录: <cyan>{config.log_directory}</cyan>")
            # 所有 sink 都使用 enqueue=True，确保启动摘要在 Uvicorn 的完成提示前输出。
            await logger.complete()
            yield
        finally:
            logger.bind(shutdown_phase="application_shutdown").info(f"⏳ {config.app_name}开始关闭")
            await http.aclose()
            await redis.aclose()
            await engine.dispose()
            logger.bind(shutdown_phase="application_shutdown").info(f"👋 {config.app_name}已关闭")
            # 等待 Loguru 队列落盘，但保留 sink 供随后执行的 Uvicorn 退出日志使用。
            await logger.complete()

    app = FastAPI(
        title=config.app_name,
        version=config.app_version,
        docs_url="/docs" if config.app_docs_enabled else None,
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
        logger.bind(request_id=_request_id(request), fields={"exceptionType": type(exc).__name__}).opt(
            exception=exc
        ).error("未处理的请求异常")
        return JSONResponse(
            status_code=500,
            content=_error_payload("INTERNAL_ERROR", "服务暂时不可用", _request_id(request)),
        )

    @app.get("/health/live", tags=["健康检查"])
    async def live(request: Request) -> dict[str, Any]:
        return {"status": "UP", "requestId": _request_id(request)}

    @app.get("/health/ready", tags=["健康检查"])
    async def ready(request: Request) -> JSONResponse:
        checks = await _check_dependencies(request.app, _request_id(request))
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
    configure_logging(config)
    # 直接传入应用对象，避免 Uvicorn 再次导入模块；PyCharm 调试断点也能落在当前进程中。
    uvicorn.run(
        app,
        host=config.app_host,
        port=config.app_port,
        log_level=config.app_log_level.lower(),
        access_log=False,
        log_config=None,
    )


if __name__ == "__main__":
    main()
