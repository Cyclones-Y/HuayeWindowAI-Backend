"""API 与 worker 共用的队列维护；无需模型进程在线也能结束过期等待。"""

import asyncio
from datetime import UTC, datetime, timedelta

from loguru import logger
from redis.asyncio import Redis
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huaye_window_ai.core.config import Settings
from huaye_window_ai.db.render_models import RenderTask
from huaye_window_ai.integrations.photo_storage import PhotoStorage

WORKER_HEARTBEAT_KEY = "huaye:render:worker:heartbeat"


async def expire_render_tasks(
    session: AsyncSession,
    settings: Settings,
    *,
    task_id: str | None = None,
    user_id: str | None = None,
    now: datetime | None = None,
) -> None:
    """以条件更新回收任务，事务由调用者提交；API 可按已验证归属的任务缩小范围。

    PREPARING 尚未调用供应商，可安全重领；已开始调用的旧租约只能失败，
    避免自动重复计费。所有更新重新比较数据库状态，防止覆盖并发领取或完成。
    """
    moment = now or datetime.now(UTC)
    scope = []
    if task_id is not None:
        scope.append(RenderTask.id == task_id)
    if user_id is not None:
        scope.append(RenderTask.user_id == user_id)
    stale = (*scope, RenderTask.status == "PROCESSING", RenderTask.lease_expires_at <= moment)
    await session.execute(
        update(RenderTask)
        .where(*stale, RenderTask.stage == "PREPARING")
        .values(status="QUEUED", stage="QUEUED", worker_token=None, lease_expires_at=None, started_at=None)
        .execution_options(synchronize_session=False)
    )
    await session.execute(
        update(RenderTask)
        .where(*stale, RenderTask.stage != "PREPARING")
        .values(
            status="FAILED",
            worker_token=None,
            lease_expires_at=None,
            failure={
                "code": "RENDER_WORKER_INTERRUPTED",
                "message": "生成连接中断，重试可能产生新的模型调用费用",
                "retryable": True,
            },
        )
        .execution_options(synchronize_session=False)
    )
    # 使用 created_at 而非 updated_at，重复轮询和准备阶段崩溃不能延长排队寿命。
    await session.execute(
        update(RenderTask)
        .where(
            *scope,
            RenderTask.status == "QUEUED",
            RenderTask.created_at <= moment - timedelta(seconds=settings.render_queue_max_wait_seconds),
        )
        .values(
            status="FAILED",
            worker_token=None,
            lease_expires_at=None,
            failure={"code": "RENDER_QUEUE_TIMEOUT", "message": "等待生成超时，请重试", "retryable": True},
        )
        .execution_options(synchronize_session=False)
    )


async def maintain_render_queue(factory: async_sessionmaker[AsyncSession], settings: Settings) -> None:
    """独立事务完成一轮维护，供 worker 轮询与 Web 后台共用。"""
    async with factory() as session, session.begin():
        await expire_render_tasks(session, settings)


async def worker_is_online(redis: Redis) -> bool:
    """只看有 TTL 的进程心跳；不以长时间模型任务是否完成判断在线。"""
    return bool(await redis.exists(WORKER_HEARTBEAT_KEY))


async def publish_worker_heartbeat(redis: Redis, settings: Settings) -> None:
    """独立协程持续报活；Redis 短暂故障可恢复，多 worker 共用在线标记。

    关闭时不删除共享键，避免一个进程退出误删其他进程心跳；最后一个进程
    停止后最多 TTL 秒标记离线。日志仅保留异常类型，不记录 Redis 地址。
    """
    while True:
        try:
            await redis.set(WORKER_HEARTBEAT_KEY, b"1", ex=settings.render_worker_heartbeat_ttl_seconds)
        except Exception as exc:
            logger.bind(fields={"exceptionType": type(exc).__name__}).warning("生成 worker 心跳写入失败")
        await asyncio.sleep(settings.render_worker_heartbeat_interval_seconds)


async def run_render_maintenance(
    factory: async_sessionmaker[AsyncSession], settings: Settings, storage: PhotoStorage
) -> None:
    """API 和 worker 均启动维护；队列回收与对象补偿互相隔离且不调用模型。

    两个循环分别捕获错误，慢存储或清理失败不能推迟排队终态。关闭时由拥有
    资源的 lifespan/worker 取消本协程，TaskGroup 会先结束子任务再关闭连接。
    """

    async def maintain_queue() -> None:
        """即使没有任务轮询和在线 worker，也定期结束排队等待。"""
        while True:
            try:
                await maintain_render_queue(factory, settings)
            except Exception as exc:
                logger.bind(fields={"exceptionType": type(exc).__name__}).warning("生成队列维护暂时失败")
            await asyncio.sleep(settings.render_maintenance_interval_seconds)

    async def maintain_objects() -> None:
        """独立低频扫描持久写入登记，不在每次领取任务时遍历对象。"""
        from huaye_window_ai.services.photo_journal import reconcile_photo_writes

        while True:
            try:
                await reconcile_photo_writes(factory, storage)
            except Exception as exc:
                logger.bind(fields={"exceptionType": type(exc).__name__}).warning("生成对象清理暂时失败")
            await asyncio.sleep(60)

    async with asyncio.TaskGroup() as group:
        group.create_task(maintain_queue())
        group.create_task(maintain_objects())
