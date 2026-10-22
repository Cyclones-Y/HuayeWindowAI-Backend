"""离线 worker 的排队退出、独立心跳与无付费维护回归。"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest
from fakeredis.aioredis import FakeRedis
from pydantic import SecretStr
from test_photos import photo_context  # noqa: F401 — 复用临时 SQLite 和登录身份。

from huaye_window_ai.db.photo_models import Photo
from huaye_window_ai.db.render_models import RenderTask
from huaye_window_ai.services.render_worker import RenderWorker
from huaye_window_ai.services.renders import RenderService


@pytest.fixture
async def queue_context(photo_context):  # noqa: F811
    """只写临时任务元数据，不生成图片或构造任何供应商请求。"""
    context = photo_context
    context.settings.render_enabled = True
    context.settings.ark_api_key = SecretStr("queue-test-only")
    context.app.state.render_service = RenderService(context.settings, context.storage)
    async with context.session_factory() as session, session.begin():
        session.add(
            Photo(
                id="queue_input",
                user_id="user_owner",
                purpose="RENDER_INPUT",
                storage_key=f"photos/{'a' * 32}/original.png",
                thumbnail_key=f"photos/{'a' * 32}/thumbnail.png",
                width=10,
                height=10,
                size_bytes=1,
                mime_type="image/png",
            )
        )
        await session.flush()
        session.add(
            RenderTask(
                id="queue_task",
                user_id="user_owner",
                idempotency_key="queue-key",
                client_request_id="queue-client",
                request_hash="a" * 64,
                source_type="SAMPLE_SCENE",
                input_photo_id="queue_input",
                snapshot={},
                model="unused",
                created_at=datetime.now(UTC) - timedelta(hours=1),
            )
        )
    return context


async def test_query_expires_queue_without_worker(queue_context):
    context = queue_context
    response = await context.client.get("/v1/render-tasks/queue_task", headers=context.owner_headers)
    task = response.json()["data"]
    assert task["status"] == "FAILED"
    assert task["failure"] == {
        "code": "RENDER_QUEUE_TIMEOUT",
        "message": "等待生成超时，请重试",
        "retryable": True,
        "stage": "QUEUED",
    }


async def test_delete_expires_queue_without_prior_poll(queue_context):
    response = await queue_context.client.delete("/v1/render-tasks/queue_task", headers=queue_context.owner_headers)
    assert response.status_code == 200
    async with queue_context.session_factory() as session:
        task = await session.get(RenderTask, "queue_task")
        assert task.status == "FAILED" and not task.failure["retryable"]


async def test_claim_never_executes_already_expired_queue(queue_context):
    context = queue_context
    worker = RenderWorker(context.settings, context.session_factory, context.storage, None)
    assert await worker.claim() is None
    async with context.session_factory() as session:
        assert (await session.get(RenderTask, "queue_task")).status == "FAILED"


async def test_periodic_maintenance_expires_without_requests(queue_context):
    from huaye_window_ai.services.render_queue import maintain_render_queue

    context = queue_context
    await maintain_render_queue(context.session_factory, context.settings)
    async with context.session_factory() as session:
        assert (await session.get(RenderTask, "queue_task")).status == "FAILED"


async def test_background_maintenance_survives_object_cleanup_failure(queue_context, monkeypatch):
    """存储补偿失败也不能停止无人轮询时的排队回收；此处不运行付费 worker。"""
    from huaye_window_ai.services.render_queue import run_render_maintenance

    context = queue_context
    attempted = asyncio.Event()

    async def unavailable_storage(*args, **kwargs):
        attempted.set()
        raise RuntimeError("fake-storage-unavailable")

    monkeypatch.setattr("huaye_window_ai.services.photo_journal.reconcile_photo_writes", unavailable_storage)
    maintenance = asyncio.create_task(
        run_render_maintenance(context.session_factory, context.settings, context.storage)
    )
    try:
        await asyncio.wait_for(attempted.wait(), timeout=1)
        # 等数据库事务实际完成，不能把后台任务已创建当成维护成功。
        async with asyncio.timeout(1):
            while True:
                async with context.session_factory() as session:
                    if (await session.get(RenderTask, "queue_task")).status == "FAILED":
                        break
                await asyncio.sleep(0.01)
        assert not maintenance.done()
    finally:
        maintenance.cancel()
        with pytest.raises(asyncio.CancelledError):
            await maintenance


@pytest.mark.parametrize(
    "status,stage", [("QUEUED", "QUEUED"), ("PROCESSING", "GENERATING"), ("SUCCEEDED", "COMPLETED")]
)
async def test_maintenance_keeps_fresh_live_or_terminal_task(queue_context, status, stage):
    """队列到期不能覆盖已领取的有效租约、已完成任务或未超过期限的新任务。"""
    from huaye_window_ai.services.render_queue import maintain_render_queue

    context = queue_context
    async with context.session_factory() as session, session.begin():
        task = await session.get(RenderTask, "queue_task")
        task.status, task.stage = status, stage
        if status == "QUEUED":
            task.created_at = datetime.now(UTC)
        task.lease_expires_at = datetime.now(UTC) + timedelta(minutes=1)
    await maintain_render_queue(context.session_factory, context.settings)
    async with context.session_factory() as session:
        task = await session.get(RenderTask, "queue_task")
        assert task.status == status and task.stage == stage and task.failure is None


async def test_unauthorized_poll_does_not_change_task(queue_context):
    context = queue_context
    response = await context.client.get("/v1/render-tasks/queue_task", headers=context.other_headers)
    assert response.status_code == 403
    async with context.session_factory() as session:
        assert (await session.get(RenderTask, "queue_task")).status == "QUEUED"


async def test_generating_lease_is_terminal_on_api_poll(queue_context):
    context = queue_context
    async with context.session_factory() as session, session.begin():
        task = await session.get(RenderTask, "queue_task")
        task.status, task.stage, task.worker_token = "PROCESSING", "GENERATING", "old-worker"
        task.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    response = await context.client.get("/v1/render-tasks/queue_task", headers=context.owner_headers)
    task = response.json()["data"]
    assert task["status"] == "FAILED" and task["failure"]["stage"] == "GENERATING"
    assert task["failure"]["code"] == "RENDER_WORKER_INTERRUPTED"


async def test_worker_heartbeat_exists_while_model_slot_is_busy(queue_context):
    from huaye_window_ai.services.render_queue import publish_worker_heartbeat, worker_is_online

    redis = FakeRedis()
    try:
        assert not await worker_is_online(redis)
        heartbeat = asyncio.create_task(publish_worker_heartbeat(redis, queue_context.settings))
        for _ in range(20):
            if await worker_is_online(redis):
                break
            await asyncio.sleep(0)
        assert await worker_is_online(redis)
        # 心跳拥有 TTL；最后一个进程退出后不会永久显示在线。
        from huaye_window_ai.services.render_queue import WORKER_HEARTBEAT_KEY

        assert 0 < await redis.ttl(WORKER_HEARTBEAT_KEY) <= queue_context.settings.render_worker_heartbeat_ttl_seconds
        heartbeat.cancel()
        with pytest.raises(asyncio.CancelledError):
            await heartbeat
    finally:
        await redis.aclose()


async def test_ready_requires_worker_only_when_render_configured(queue_context):
    context = queue_context
    redis = FakeRedis()
    context.app.state.engine = context.session_factory.kw["bind"]
    context.app.state.redis = redis
    try:
        response = await context.client.get("/health/ready")
        assert response.status_code == 503 and response.json()["checks"]["renderWorker"] is False
        from huaye_window_ai.services.render_queue import WORKER_HEARTBEAT_KEY

        await redis.set(WORKER_HEARTBEAT_KEY, b"1", ex=20)
        response = await context.client.get("/health/ready")
        assert response.status_code == 200 and response.json()["checks"]["renderWorker"] is True
        await redis.delete(WORKER_HEARTBEAT_KEY)
        context.settings.ark_api_key = SecretStr("")
        response = await context.client.get("/health/ready")
        assert response.status_code == 200 and "renderWorker" not in response.json()["checks"]
        context.settings.render_enabled = False
        response = await context.client.get("/health/ready")
        assert response.status_code == 200 and "renderWorker" not in response.json()["checks"]
    finally:
        await redis.aclose()
