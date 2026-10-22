"""跨数据库与私有存储的写入登记、提交核对和崩溃后清理。

数据库提交失败不代表未提交。补偿始终使用新事务检查 Photo；检查不可用时保留文件和登记，
交由周期维护重试。生产 MySQL 的行锁让维护任务无法删除仍在写入或提交的对象。
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta

from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from huaye_window_ai.db.photo_models import Photo, PhotoWriteIntent
from huaye_window_ai.integrations.photo_storage import PhotoStorage


@asynccontextmanager
async def _journal_session(session: AsyncSession) -> AsyncIterator[AsyncSession]:
    """同一数据库的独立短连接，避免业务池满时所有事务都等待第二条池连接。

    业务事务可能持有源照片锁，登记必须独立提交而不能复用该连接。NullPool 不向业务池
    申请额外名额，用完立即关闭；部署仍需为在途写入预留数据库连接。内存 SQLite 仅用于
    测试，独立连接会指向另一份空库，因此复用其原绑定。
    """
    if session.bind is None:
        raise RuntimeError("照片写入需要已绑定数据库的会话")
    engine = session.bind if isinstance(session.bind, AsyncEngine) else session.bind.engine
    independent = None
    if engine.dialect.name != "sqlite" or engine.url.database not in {None, "", ":memory:"}:
        independent = create_async_engine(engine.url, poolclass=NullPool, hide_parameters=True)
    try:
        async with async_sessionmaker(independent or engine, expire_on_commit=False)() as journal:
            yield journal
    finally:
        if independent is not None:
            await independent.dispose()


async def prepare_photo_write(session: AsyncSession, photo: Photo) -> None:
    """写文件前持久化意图，再在业务事务锁定；调用方负责将 Photo 与登记移除一起提交。

    独立提交先于任何文件写入，所以进程在任意写入边界中断都不会遗失清理依据。登记一旦
    被业务事务锁住，即使写入超过清理宽限期，维护任务也必须跳过，直到提交或回滚释放锁。
    """
    async with _journal_session(session) as journal_session, journal_session.begin():
        journal_session.add(
            PhotoWriteIntent(photo_id=photo.id, storage_key=photo.storage_key, thumbnail_key=photo.thumbnail_key)
        )
    intent = await session.scalar(
        select(PhotoWriteIntent).where(PhotoWriteIntent.photo_id == photo.id).with_for_update()
    )
    if intent is None:
        raise RuntimeError("照片写入登记不存在")
    # 删除尚未提交；Photo 写入失败时整笔回滚，先前独立提交的登记会自然恢复。
    await session.delete(intent)


async def _reconcile_one(
    session: AsyncSession, storage: PhotoStorage, intent: PhotoWriteIntent, *, final: bool = True
) -> None:
    """在持有登记行锁时核对业务引用；有效 Photo 永远优先保留，删除可安全重复。"""
    photo = await session.scalar(select(Photo).where(Photo.id == intent.photo_id).with_for_update())
    if photo is None or photo.deleted_at is not None:
        await storage.delete(intent.storage_key)
        await storage.delete(intent.thumbnail_key)
        # 远端 PUT 报错不代表服务端已停止写入。即时补偿后保留登记，宽限期结束再次核对，
        # 回收可能在 DELETE 之后才到达的对象；维护任务确认完成后才移除登记。
        if final:
            await session.delete(intent)
    else:
        await session.delete(intent)


async def compensate_photo_write(session: AsyncSession, storage: PhotoStorage, photo: Photo) -> None:
    """释放失败业务事务后独立核对，保留已提交图片；补偿故障不能遮盖原始异常。

    COMMIT 应答丢失时 Photo 和登记删除可能已一起提交，此时绝不能按异常类型直接删图。
    DB 或存储仍不可用则只记录安全摘要，持久登记会在恢复后再次被维护任务处理。
    """
    try:
        await session.rollback()
    except Exception:
        logger.bind(fields={"photoId": photo.id}).warning("照片事务回滚未确认，保留写入登记供核对")
    try:
        async with _journal_session(session) as check, check.begin():
            intent = await check.scalar(
                select(PhotoWriteIntent).where(PhotoWriteIntent.photo_id == photo.id).with_for_update()
            )
            if intent is not None:
                await _reconcile_one(check, storage, intent, final=False)
            # 没有登记表示业务事务已提交或此前已经清理；两种情况均不能再次盲删文件。
    except Exception:
        logger.bind(fields={"photoId": photo.id}).warning("照片写入补偿未完成，将由持久登记重试")


async def reconcile_photo_writes(
    factory: async_sessionmaker[AsyncSession], storage: PhotoStorage, *, grace_seconds: int = 3600, limit: int = 50
) -> None:
    """每轮最多回收一批到期登记；无引用时删对象，有有效引用时只移除登记。

    宽限期保护刚准备写入、尚未取得业务事务行锁的短暂窗口。每项用独立事务并跳过已锁行，
    避免多维护进程重复清理，或单个存储故障挡住整批登记。不会扫描或删除未登记的历史对象。
    """
    now = datetime.now(UTC)
    cutoff = now - timedelta(seconds=grace_seconds)
    attempted: list[str] = []
    for _ in range(limit):
        identifier = None
        try:
            async with factory() as session, session.begin():
                intent = await session.scalar(
                    select(PhotoWriteIntent)
                    .where(
                        PhotoWriteIntent.created_at <= cutoff,
                        PhotoWriteIntent.next_attempt_at <= now,
                        PhotoWriteIntent.photo_id.not_in(attempted),
                    )
                    .order_by(PhotoWriteIntent.next_attempt_at, PhotoWriteIntent.created_at, PhotoWriteIntent.photo_id)
                    .limit(1)
                    .with_for_update(skip_locked=True)
                )
                if intent is None:
                    break
                identifier = intent.photo_id
                attempted.append(identifier)
                await _reconcile_one(session, storage, intent)
        except Exception:
            if identifier is None:
                # 无法访问数据库时结束本轮，避免无意义重试同一连接错误。
                raise
            # 对象长期失败不能永久占满每批名额；退避期间继续处理其他到期登记。
            try:
                async with factory() as retry, retry.begin():
                    await retry.execute(
                        update(PhotoWriteIntent)
                        .where(PhotoWriteIntent.photo_id == identifier)
                        .values(next_attempt_at=now + timedelta(minutes=5))
                    )
            except Exception:
                logger.bind(fields={"photoId": identifier}).warning("照片登记退避未保存，保留原登记")
            logger.bind(fields={"photoId": identifier}).warning("照片登记清理暂未完成，下轮重试")
