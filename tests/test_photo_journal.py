"""故障注入保护跨数据库/存储提交：已提交图片不能误删，未提交图片必须可追踪清理。"""

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, AsyncSessionTransaction
from test_render import FakeProvider, create, photo_context, render_context  # noqa: F401

from huaye_window_ai.db.photo_models import Photo
from huaye_window_ai.db.render_models import RenderResult, RenderTask
from huaye_window_ai.services.render_worker import RenderWorker


async def test_enqueue_commit_ack_loss_keeps_committed_input(render_context, monkeypatch):  # noqa: F811
    """服务端已提交但连接随后断开时，rollback 无法撤销事务，输入文件仍应保留。"""
    context = render_context
    commit = AsyncSession.commit
    fired = False

    async def lose_ack(session):
        nonlocal fired
        pending = any(isinstance(item, RenderTask) for item in session.new)
        await commit(session)
        if pending and not fired:
            fired = True
            raise ConnectionError("simulated lost commit acknowledgement")

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    with pytest.raises(ConnectionError):
        await create(context)
    async with context.session_factory() as session:
        task = (await session.scalars(select(RenderTask))).one()
        photo = await session.get(Photo, task.input_photo_id)
        assert task.status == "QUEUED"
        assert (context.directory / photo.storage_key).exists()
        assert (context.directory / photo.thumbnail_key).exists()


async def test_result_commit_ack_loss_keeps_successful_image(render_context, monkeypatch):  # noqa: F811
    """最终事务成功后抛连接错误，不能把已计费成功结果变成永久坏图。"""
    context = render_context
    await create(context)
    original = AsyncSessionTransaction.__aexit__
    fired = False

    async def lose_ack(transaction, type_, value, traceback):
        nonlocal fired
        pending = any(
            isinstance(item, Photo) and item.purpose == "RENDER_RESULT"
            for item in transaction.session.identity_map.values()
        )
        outcome = await original(transaction, type_, value, traceback)
        if pending and not fired:
            fired = True
            raise ConnectionError("simulated lost result acknowledgement")
        return outcome

    monkeypatch.setattr(AsyncSessionTransaction, "__aexit__", lose_ack)
    provider = FakeProvider()
    await RenderWorker(context.settings, context.session_factory, context.storage, provider).run_once()
    assert fired and provider.calls == 1
    async with context.session_factory() as session:
        task = (await session.scalars(select(RenderTask))).one()
        result = (await session.scalars(select(RenderResult))).one()
        photo = await session.get(Photo, result.after_photo_id)
        assert task.status == "SUCCEEDED"
        assert (context.directory / photo.storage_key).exists()
        assert (context.directory / photo.thumbnail_key).exists()


async def test_failed_compensation_has_durable_retry_record(render_context, monkeypatch):  # noqa: F811
    """数据库失败叠加对象存储失败时，住宅快照不能成为业务系统无法定位的孤儿。"""
    from huaye_window_ai.db.photo_models import PhotoWriteIntent
    from huaye_window_ai.services.photo_journal import reconcile_photo_writes

    context = render_context
    commit = AsyncSession.commit
    delete = context.storage.delete

    async def fail_task_commit(session):
        if any(isinstance(item, RenderTask) for item in session.new):
            raise ConnectionError("simulated database unavailable")
        await commit(session)

    async def fail_delete(key):
        raise ConnectionError("simulated object storage unavailable")

    monkeypatch.setattr(AsyncSession, "commit", fail_task_commit)
    monkeypatch.setattr(context.storage, "delete", fail_delete)
    with pytest.raises(ConnectionError):
        await create(context)
    async with context.session_factory() as session:
        assert list(await session.scalars(select(Photo))) == []
        intent = (await session.scalars(select(PhotoWriteIntent))).one()
        intent.created_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()
    assert len(list(context.directory.rglob("*.png"))) == 2
    # 存储恢复后无需用户保留提交索引，维护任务也能找到并回收两份对象。
    monkeypatch.setattr(context.storage, "delete", delete)
    await reconcile_photo_writes(context.session_factory, context.storage)
    async with context.session_factory() as session:
        assert list(await session.scalars(select(PhotoWriteIntent))) == []
    assert not list(context.directory.rglob("*.png"))


async def test_upload_commit_ack_loss_keeps_private_preview(photo_context, monkeypatch):  # noqa: F811
    """原图上传与生成快照必须遵守同一提交语义，网络失败后不能误删已持久图片。"""
    context = photo_context
    commit = AsyncSession.commit
    fired = False

    async def lose_ack(session):
        nonlocal fired
        pending = any(isinstance(item, Photo) for item in session.identity_map.values())
        await commit(session)
        if pending and not fired:
            fired = True
            raise ConnectionError("simulated lost upload acknowledgement")

    monkeypatch.setattr(AsyncSession, "commit", lose_ack)
    with pytest.raises(ConnectionError):
        await context.upload()
    async with context.session_factory() as session:
        photo = (await session.scalars(select(Photo))).one()
        assert (context.directory / photo.storage_key).exists()
        assert (context.directory / photo.thumbnail_key).exists()


async def test_interrupted_writer_is_tracked_and_recovered_after_grace(render_context):  # noqa: F811
    """模拟文件已写但进程未提交：事务退出释放锁，独立登记保留至宽限期后清理。"""
    from test_photos import make_image

    from huaye_window_ai.db.photo_models import PhotoWriteIntent
    from huaye_window_ai.services.photo_journal import reconcile_photo_writes

    context = render_context
    async with context.session_factory() as session:
        await context.app.state.render_service.write_photo(make_image(), "user_owner", "RENDER_INPUT", session)
        # 没有提交 Photo，等同写入后进程退出、数据库回滚未完成的业务事务。
    assert len(list(context.directory.rglob("*.png"))) == 2
    await reconcile_photo_writes(context.session_factory, context.storage)
    assert len(list(context.directory.rglob("*.png"))) == 2  # 新登记保护写入方尚未持锁的短窗口。
    async with context.session_factory() as session:
        intent = (await session.scalars(select(PhotoWriteIntent))).one()
        intent.created_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()
    await reconcile_photo_writes(context.session_factory, context.storage)
    assert not list(context.directory.rglob("*.png"))


async def test_reconciliation_preserves_referenced_private_photo(render_context):  # noqa: F811
    """即使残留登记很旧，已提交的业务引用也必须优先保留，不能按年龄盲删。"""
    from huaye_window_ai.db.photo_models import PhotoWriteIntent
    from huaye_window_ai.services.photo_journal import reconcile_photo_writes

    context = render_context
    await create(context)
    async with context.session_factory() as session:
        photo = (await session.scalars(select(Photo))).one()
        keys = [photo.storage_key, photo.thumbnail_key]
        session.add(
            PhotoWriteIntent(
                photo_id=photo.id,
                storage_key=keys[0],
                thumbnail_key=keys[1],
                created_at=datetime.now(UTC) - timedelta(hours=2),
            )
        )
        await session.commit()
    await reconcile_photo_writes(context.session_factory, context.storage)
    assert all((context.directory / key).exists() for key in keys)
    async with context.session_factory() as session:
        assert list(await session.scalars(select(PhotoWriteIntent))) == []


async def test_cancelled_object_write_leaves_no_untracked_files(render_context, monkeypatch):  # noqa: F811
    """存储已完成但调用被取消时仍须有补偿依据，不能让迟到写入覆盖先前清理。"""
    import asyncio

    context = render_context
    put = context.storage.put

    async def cancel_after_put(key, data, content_type):
        await put(key, data, content_type)
        raise asyncio.CancelledError

    monkeypatch.setattr(context.storage, "put", cancel_after_put)
    from test_photos import make_image

    # 直接取消服务调用，避免 ASGI 中间件把 CancelledError 转换为“No response returned”。
    with pytest.raises(asyncio.CancelledError):
        async with context.session_factory() as session:
            await context.app.state.render_service.write_photo(make_image(), "user_owner", "RENDER_INPUT", session)
    assert not list(context.directory.rglob("*.png"))


async def test_persistent_storage_failures_do_not_starve_later_objects(render_context, monkeypatch):  # noqa: F811
    """超过一批的旧故障对象不能永久占据维护名额，让后续健康对象永远无法清理。"""
    import uuid

    from huaye_window_ai.db.photo_models import PhotoWriteIntent
    from huaye_window_ai.services.photo_journal import reconcile_photo_writes

    context = render_context
    healthy = uuid.uuid4().hex
    async with context.session_factory() as session, session.begin():
        for index in range(51):
            identifier = healthy if index == 50 else uuid.uuid4().hex
            session.add(
                PhotoWriteIntent(
                    photo_id=f"photo_{identifier}",
                    storage_key=f"photos/{identifier}/original.png",
                    thumbnail_key=f"photos/{identifier}/thumbnail.png",
                    created_at=datetime.now(UTC) - timedelta(hours=3) + timedelta(seconds=index),
                )
            )
    deleted = []

    async def selective_failure(key):
        if healthy not in key:
            raise ConnectionError("simulated persistent failure for an older object")
        deleted.append(key)

    monkeypatch.setattr(context.storage, "delete", selective_failure)
    await reconcile_photo_writes(context.session_factory, context.storage)
    await reconcile_photo_writes(context.session_factory, context.storage)
    assert len(deleted) == 2
    async with context.session_factory() as session:
        assert await session.get(PhotoWriteIntent, f"photo_{healthy}") is None


async def test_small_business_pool_does_not_deadlock_write_registration(render_context):  # noqa: F811
    """业务事务持有唯一池连接时，写入登记仍须有独立连接，不能自我等待至池超时。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    context = render_context
    engine = create_async_engine(
        context.session_factory.kw["bind"].url, pool_size=1, max_overflow=0, pool_timeout=0.2, hide_parameters=True
    )
    context.app.state.session_factory = async_sessionmaker(engine, expire_on_commit=False)
    try:
        assert (await create(context)).status_code == 202
    finally:
        await engine.dispose()


async def test_late_remote_put_is_rechecked_after_immediate_compensation(render_context, monkeypatch):  # noqa: F811
    """远端 PUT 应答超时后可能迟到完成，首次 DELETE 成功仍需保留登记做延迟复核。"""
    from huaye_window_ai.db.photo_models import PhotoWriteIntent
    from huaye_window_ai.services.photo_journal import reconcile_photo_writes

    context = render_context
    put = context.storage.put

    async def uncertain_put(key, data, content_type):
        raise ConnectionError("simulated remote write acknowledgement lost")

    monkeypatch.setattr(context.storage, "put", uncertain_put)
    with pytest.raises(ConnectionError):
        await create(context)
    async with context.session_factory() as session:
        intent = (await session.scalars(select(PhotoWriteIntent))).one()
        key = intent.storage_key
        # 模拟对象服务端在第一次补偿 DELETE 之后才完成先前的 PUT。
        await put(key, b"synthetic late object", "image/png")
        intent.created_at = datetime.now(UTC) - timedelta(hours=2)
        await session.commit()
    await reconcile_photo_writes(context.session_factory, context.storage)
    assert not (context.directory / key).exists()
