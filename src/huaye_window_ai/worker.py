"""独立生成执行进程；运行 python -m huaye_window_ai.worker，不依附 Web 进程。"""

import asyncio

import httpx
from loguru import logger
from redis.asyncio import Redis

from huaye_window_ai.core.config import get_settings
from huaye_window_ai.core.logging import configure_logging
from huaye_window_ai.db.session import create_engine, create_session_factory
from huaye_window_ai.integrations.photo_storage import create_photo_storage
from huaye_window_ai.integrations.seedream import SeedreamProvider
from huaye_window_ai.services.render_queue import publish_worker_heartbeat, run_render_maintenance
from huaye_window_ai.services.render_worker import RenderWorker


async def main() -> None:
    """多执行槽共用原子数据库领取；关闭时取消任务，租约回收决定安全终态。"""
    settings = get_settings()
    configure_logging(settings)
    if not settings.render_enabled or not settings.ark_api_key.get_secret_value():
        raise RuntimeError("请配置 RENDER_ENABLED=true 和 ARK_API_KEY 后启动生成 worker")
    engine = create_engine(settings.resolved_database_url)
    storage = create_photo_storage(settings)
    redis = Redis.from_url(settings.resolved_redis_url, decode_responses=False)
    factory = create_session_factory(engine)
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(settings.render_provider_timeout_seconds, connect=10)
        ) as http:
            worker = RenderWorker(settings, factory, storage, SeedreamProvider(settings, http))

            async def consume() -> None:
                """队列暂时不可用时退避，单个数据库错误不能永久结束整个 worker。"""
                while True:
                    try:
                        if not await worker.run_once():
                            await asyncio.sleep(1)
                    except Exception as exc:
                        logger.bind(fields={"exceptionType": type(exc).__name__}).error("生成 worker 暂时不可用")
                        await asyncio.sleep(3)

            async with asyncio.TaskGroup() as group:
                # 报活和维护使用独立任务，所有生成槽都在等待模型时仍可工作。
                group.create_task(publish_worker_heartbeat(redis, settings))
                group.create_task(run_render_maintenance(factory, settings, storage))
                for _ in range(settings.render_worker_concurrency):
                    group.create_task(consume())
    finally:
        await redis.aclose()
        await storage.aclose()
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
