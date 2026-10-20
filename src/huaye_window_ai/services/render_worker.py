"""独立 worker 从数据库领取任务；租约与条件写入保护多进程和迟到结果。"""

import asyncio
import contextlib
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from loguru import logger
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError
from huaye_window_ai.db.photo_models import Photo
from huaye_window_ai.db.render_models import RenderResult, RenderTask
from huaye_window_ai.integrations.photo_storage import PhotoStorage
from huaye_window_ai.integrations.seedream import ImageModelProvider
from huaye_window_ai.services.render_images import (
    composite_selection,
    marked_input,
    open_image,
    output_dimensions,
    png,
    provider_image,
)
from huaye_window_ai.services.renders import RenderService


class LeaseLostError(Exception):
    """任务已被回收时停止写入，旧 worker 不能复活失败或被删除的任务。"""


class RenderWorker:
    """数据库持久队列不依赖 Web 请求生命周期；供应商调用前持久化 GENERATING。"""

    def __init__(
        self,
        settings: Settings,
        factory: async_sessionmaker[AsyncSession],
        storage: PhotoStorage,
        provider: ImageModelProvider,
    ) -> None:
        self.settings, self.factory, self.storage, self.provider = settings, factory, storage, provider
        self.service = RenderService(settings, storage)

    async def recover(self) -> None:
        """准备阶段可安全重新领取；调用已开始的任务失联后失败，绝不自动重复计费。"""
        now = datetime.now(UTC)
        async with self.factory() as session, session.begin():
            stale = (RenderTask.status == "PROCESSING", RenderTask.lease_expires_at < now)
            await session.execute(
                update(RenderTask)
                .where(*stale, RenderTask.stage == "PREPARING")
                .values(status="QUEUED", stage="QUEUED", worker_token=None, lease_expires_at=None, started_at=None)
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
            )

    async def claim(self) -> RenderTask | None:
        """原子条件更新领取一个任务；失败竞争者不调用供应商。"""
        async with self.factory() as session:
            candidate = await session.scalar(
                select(RenderTask.id)
                .where(RenderTask.status == "QUEUED")
                .order_by(RenderTask.created_at, RenderTask.id)
                .limit(1)
            )
            if candidate is None:
                return None
            token = str(uuid.uuid4())
            now = datetime.now(UTC)
            result = await session.execute(
                update(RenderTask)
                .where(RenderTask.id == candidate, RenderTask.status == "QUEUED")
                .values(
                    status="PROCESSING",
                    stage="PREPARING",
                    worker_token=token,
                    started_at=now,
                    lease_expires_at=now + timedelta(seconds=30),
                )
            )
            await session.commit()
            if result.rowcount != 1:  # type: ignore[attr-defined]
                return None
            return await session.get(RenderTask, candidate)

    async def change(self, task: RenderTask, **values: Any) -> None:
        """阶段只有真实工作完成或即将开始时改变；过期租约禁止提交。"""
        async with self.factory() as session, session.begin():
            result = await session.execute(
                update(RenderTask)
                .where(
                    RenderTask.id == task.id,
                    RenderTask.worker_token == task.worker_token,
                    RenderTask.status == "PROCESSING",
                    RenderTask.lease_expires_at > datetime.now(UTC),
                )
                .values(**values)
            )
            if result.rowcount != 1:  # type: ignore[attr-defined]
                raise LeaseLostError

    async def heartbeat(self, task: RenderTask) -> None:
        """续租覆盖长时间模型等待；续租丢失由执行端的阶段写入及最终事务再次检查。"""
        while True:
            await asyncio.sleep(10)
            await self.change(task, lease_expires_at=datetime.now(UTC) + timedelta(seconds=30))

    async def execute(self, task: RenderTask) -> None:
        """图片准备、模型生成、区域融合与结果保存分别对应真实阶段。"""
        after: Photo | None = None
        try:
            async with self.factory() as session:
                before = await session.get(Photo, task.input_photo_id)
            if before is None or before.deleted_at is not None:
                raise AppError("RENDER_SOURCE_INVALID", "照片已删除，请重新上传", status_code=422)
            original = await self.storage.read(before.storage_key)
            image = await asyncio.to_thread(open_image, original)
            original = await asyncio.to_thread(png, image)
            selection = task.snapshot["request"]["selection"]
            marked = await asyncio.to_thread(marked_input, original, selection)
            reference = await self.service.catalog_image(task.snapshot["referencePath"])
            reference = await asyncio.to_thread(lambda: png(open_image(reference)))
            inputs = await asyncio.gather(
                *(asyncio.to_thread(provider_image, data) for data in [original, marked, reference])
            )
            solution = task.snapshot["solution"]
            prompt = (
                f"图1是待改造的房间，图2红框仅标记编辑区域，图3仅提供门窗款式参考。"
                f"只将图1框内门窗替换为{solution['name']}，参考图3的窗扇分格和结构。"
                f"边框使用{solution['frameColorLabel']}（{solution['colorHex']}），玻璃使用{solution['glassLabel']}。"
                f"特点：{'、'.join(solution['features'])}。保留窗洞尺寸、墙面、家具、窗帘、窗外景色和拍摄视角。"
                "保持原图比例，匹配透视和光照，不添加标记、文字或红框。"
            )
            await self.change(task, stage="GENERATING")
            # httpx 分段超时不是总超时；外层额外限制供应商整个调用的墙钟时间。
            async with asyncio.timeout(self.settings.render_provider_timeout_seconds):
                generated = await self.provider.generate(
                    list(inputs),
                    prompt,
                    output_dimensions(image.width, image.height),
                    model=task.model,
                )
            await self.change(task, stage="SAVING")
            composite = await asyncio.to_thread(composite_selection, original, generated, selection)
            after = await self.service.write_photo(composite, task.user_id, "RENDER_RESULT")
            result_id = f"result_{uuid.uuid4().hex}"
            async with self.factory() as session, session.begin():
                updated = await session.execute(
                    update(RenderTask)
                    .where(
                        RenderTask.id == task.id,
                        RenderTask.worker_token == task.worker_token,
                        RenderTask.status == "PROCESSING",
                        RenderTask.lease_expires_at > datetime.now(UTC),
                    )
                    .values(
                        status="SUCCEEDED",
                        stage="COMPLETED",
                        result_id=result_id,
                        worker_token=None,
                        lease_expires_at=None,
                    )
                )
                if updated.rowcount != 1:  # type: ignore[attr-defined]
                    raise LeaseLostError
                session.add(after)
                await session.flush()
                session.add(
                    RenderResult(
                        id=result_id,
                        task_id=task.id,
                        user_id=task.user_id,
                        before_photo_id=before.id,
                        after_photo_id=after.id,
                        width=before.width,
                        height=before.height,
                        snapshot=task.snapshot,
                        result_type="USER_GENERATED" if task.source_type == "UPLOADED_PHOTO" else "SAMPLE_DEMO",
                    )
                )
            logger.bind(fields={"taskId": task.id}).info("生成结果已保存")
        except BaseException:
            if after is not None:
                await self.service.cleanup_photo(after)
            raise

    async def run_once(self) -> bool:
        """处理至多一个任务；错误只保存可公开分类，不记录供应商响应或住宅图像。"""
        await self.recover()
        task = await self.claim()
        if task is None:
            return False
        heartbeat = asyncio.create_task(self.heartbeat(task))
        try:
            async with asyncio.timeout(self.settings.render_task_timeout_seconds):
                await self.execute(task)
        except LeaseLostError:
            pass
        except asyncio.CancelledError:
            # 程序关闭只留下租约供下次回收，不自动重新发送可能已计费的请求。
            raise
        except Exception as exc:
            if isinstance(exc, (httpx.TimeoutException, TimeoutError)):
                code, message, retryable = "RENDER_PROVIDER_TIMEOUT", "生成超时，重试可能产生新的模型调用费用", True
            elif isinstance(exc, AppError):
                code, message = exc.code, exc.message
                retryable = code not in {
                    "RENDER_SOURCE_INVALID",
                    "RENDER_PROVIDER_CONFIGURATION",
                    "RENDER_REFERENCE_INVALID",
                    "RENDER_IMAGE_UNSUPPORTED",
                }
            else:
                code, message, retryable = "RENDER_FAILED", "生成未完成，请重试", True
            with contextlib.suppress(LeaseLostError):
                await self.change(
                    task,
                    status="FAILED",
                    failure={"code": code, "message": message, "retryable": retryable},
                    worker_token=None,
                    lease_expires_at=None,
                )
            logger.bind(fields={"taskId": task.id, "code": code}).warning("生成任务失败")
        finally:
            heartbeat.cancel()
            with contextlib.suppress(asyncio.CancelledError, LeaseLostError):
                await heartbeat
        return True
