"""生成提交、幂等恢复、结果读取和私有对象清理，服务层负责事务边界。"""

import asyncio
import hashlib
import json
import uuid
from datetime import UTC, datetime, timedelta
from importlib.resources import files
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError
from huaye_window_ai.db.catalog_models import SampleScene, WindowSolution
from huaye_window_ai.db.photo_models import Photo
from huaye_window_ai.db.render_models import RenderResult, RenderTask
from huaye_window_ai.integrations.photo_storage import PhotoStorage
from huaye_window_ai.schemas.render import CreateRenderTask
from huaye_window_ai.services.render_images import open_image, output_dimensions, png

STAGE_LABELS = {
    "QUEUED": "等待生成",
    "PREPARING": "准备图片",
    "GENERATING": "AI 正在生成",
    "SAVING": "保存结果",
    "COMPLETED": "生成完成",
}


def task_data(task: RenderTask) -> dict[str, Any]:
    """状态查询不包含供应商原始错误、内部提示词或任何推算百分比。"""
    return {
        "taskId": task.id,
        "status": task.status,
        "stage": task.stage,
        "stageLabel": STAGE_LABELS[task.stage],
        "progress": None,
        "pollAfterMs": 1500 if task.stage in {"PREPARING", "SAVING"} else 3000,
        "resultId": task.result_id,
        "failure": {**task.failure, "stage": task.stage} if task.failure else None,
        "createdAt": task.created_at.isoformat(),
        "updatedAt": task.updated_at.isoformat(),
    }


async def owned[OwnedRecord: (RenderTask, RenderResult)](
    session: AsyncSession, model: type[OwnedRecord], identifier: str, user_id: str
) -> OwnedRecord:
    """先验证归属，再读取业务资源；与现有照片接口保持 403/404 行为一致。"""
    record = await session.get(model, identifier)
    if record is None:
        raise AppError("RESOURCE_NOT_FOUND", "记录不存在", status_code=404)
    if record.user_id != user_id:
        raise AppError("RESOURCE_FORBIDDEN", "无权访问该记录", status_code=403)
    return record


class RenderService:
    """HTTP 服务与 worker 共用私有存储，但供应商调用不占用请求事务。"""

    def __init__(self, settings: Settings, storage: PhotoStorage) -> None:
        self.settings, self.storage = settings, storage

    async def catalog_image(self, name: str) -> bytes:
        """只读取随包发布的目录素材；数据库字段也不能越出素材目录。"""
        from pathlib import Path

        def read() -> bytes:
            """解析路径与磁盘读写一起在线程中执行，避免阻塞 API/worker 事件循环。"""
            root = Path(str(files("huaye_window_ai").joinpath("data/public/catalog"))).resolve()
            path = (root / name).resolve()
            if not path.is_relative_to(root):
                raise AppError("RENDER_REFERENCE_INVALID", "方案参考图无效", status_code=422)
            return path.read_bytes()

        try:
            return await asyncio.to_thread(read)
        except OSError:
            raise AppError("RENDER_REFERENCE_INVALID", "方案参考图无法读取", status_code=422) from None

    async def write_photo(self, data: bytes, user_id: str, purpose: str) -> Photo:
        """调用者提交 Photo；任何半成品写入失败均尽力回收自己的随机对象键。"""
        identifier = uuid.uuid4().hex
        image = await asyncio.to_thread(open_image, data)
        normalized = await asyncio.to_thread(png, image)
        thumbnail = image.copy()
        thumbnail.thumbnail((512, 512))
        photo = Photo(
            id=f"photo_{identifier}",
            user_id=user_id,
            purpose=purpose,
            storage_key=f"photos/{identifier}/original.png",
            thumbnail_key=f"photos/{identifier}/thumbnail.png",
            width=image.width,
            height=image.height,
            size_bytes=len(normalized),
            mime_type="image/png",
        )
        try:
            await self.storage.put(photo.storage_key, normalized, "image/png")
            await self.storage.put(photo.thumbnail_key, await asyncio.to_thread(png, thumbnail), "image/png")
        except BaseException:
            await self.cleanup_photo(photo)
            raise
        return photo

    async def cleanup_photo(self, photo: Photo) -> None:
        """数据库未持久化时补偿文件写入；不让清理异常遮盖原业务错误。"""
        from loguru import logger

        for key in (photo.storage_key, photo.thumbnail_key):
            try:
                await self.storage.delete(key)
            except Exception:
                logger.bind(fields={"photoId": photo.id}).warning("生成临时对象清理失败")

    async def _existing(
        self, session: AsyncSession, user_id: str, key: str, client_id: str, digest: str
    ) -> RenderTask | None:
        """幂等键和客户端提交 ID 都可恢复；任何一个标识绑定其他输入则拒绝。"""
        records = (
            await session.scalars(
                select(RenderTask).where(
                    RenderTask.user_id == user_id,
                    or_(RenderTask.idempotency_key == key, RenderTask.client_request_id == client_id),
                )
            )
        ).all()
        if any(item.request_hash != digest for item in records) or len({item.id for item in records}) > 1:
            raise AppError("IDEMPOTENCY_CONFLICT", "提交标识已用于其他生成请求", status_code=409)
        return records[0] if records else None

    async def create(self, request: CreateRenderTask, user_id: str, key: str, session: AsyncSession) -> RenderTask:
        """完成输入快照后入队，唯一约束解决并发提交；网络重试不会再次调用模型。"""
        body = request.model_dump(mode="json", by_alias=True)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = await self._existing(session, user_id, key, str(request.client_request_id), digest)
        if existing is not None:
            return existing
        if not self.settings.render_enabled or not self.settings.ark_api_key.get_secret_value():
            raise AppError("RENDER_NOT_ENABLED", "真实生成服务尚未启用，请联系管理员", status_code=503)
        solution = await session.get(WindowSolution, request.solution_id)
        if solution is None or not solution.available:
            raise AppError("SOLUTION_UNAVAILABLE", "方案已下架，请重新选择", status_code=422)
        color = next(
            (item for item in solution.frame_colors if item["code"] == request.frame_color_code and item["available"]),
            None,
        )
        glass = next(
            (item for item in solution.glass_options if item["code"] == request.glass_code and item["available"]), None
        )
        if color is None or glass is None:
            raise AppError("SOLUTION_CONFIGURATION_INVALID", "颜色或玻璃配置不可用", status_code=422)
        if request.source_type == "UPLOADED_PHOTO":
            # 与删除服务锁定同一 Photo 行，避免检查通过后照片被并发物理删除。
            source = await session.scalar(select(Photo).where(Photo.id == request.photo_id).with_for_update())
            if source is None or source.deleted_at is not None:
                raise AppError("RESOURCE_NOT_FOUND", "照片不存在或已删除", status_code=404)
            if source.user_id != user_id:
                raise AppError("RESOURCE_FORBIDDEN", "无权使用该照片", status_code=403)
            if source.purpose != "WINDOW_PREVIEW":
                raise AppError("RENDER_SOURCE_INVALID", "请使用上传的现场照片", status_code=422)
            original = await self.storage.read(source.storage_key)
        else:
            sample = await session.get(SampleScene, request.sample_scene_id)
            if sample is None or not sample.available:
                raise AppError("RESOURCE_NOT_FOUND", "样例不可用", status_code=404)
            original = await self.catalog_image(sample.thumbnail_path)
        image = await asyncio.to_thread(open_image, original)
        output_dimensions(image.width, image.height)
        reference = await self.catalog_image(solution.cover_path)
        await asyncio.to_thread(open_image, reference)
        snapshot = {
            "request": body,
            "solution": {
                "id": solution.id,
                "name": solution.name,
                "subtitle": solution.subtitle,
                "features": solution.features,
                "pricingNote": solution.pricing_note,
                "frameColorCode": color["code"],
                "frameColorLabel": color["label"],
                "colorHex": color["colorHex"],
                "glassCode": glass["code"],
                "glassLabel": glass["label"],
            },
            "referencePath": solution.cover_path,
        }
        return await self._enqueue(request, user_id, key, session, original, snapshot, self.settings.seedream_model)

    async def _enqueue(
        self,
        request: CreateRenderTask,
        user_id: str,
        key: str,
        session: AsyncSession,
        original: bytes,
        snapshot: dict[str, Any],
        model: str,
        retry_of: str | None = None,
    ) -> RenderTask:
        """创建与重试共用事务与唯一键补偿；输入、配置和模型均由调用者明确冻结。"""
        body = request.model_dump(mode="json", by_alias=True)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        input_photo = await self.write_photo(original, user_id, "RENDER_INPUT")
        task = RenderTask(
            id=f"task_{uuid.uuid4().hex}",
            user_id=user_id,
            idempotency_key=key,
            client_request_id=str(request.client_request_id),
            request_hash=digest,
            source_type=request.source_type,
            photo_id=request.photo_id,
            input_photo_id=input_photo.id,
            snapshot=snapshot,
            model=model,
            retry_of=retry_of,
        )
        try:
            session.add(input_photo)
            await session.flush()
            session.add(task)
            await session.commit()
        except IntegrityError:
            await session.rollback()
            await self.cleanup_photo(input_photo)
            existing = await self._existing(session, user_id, key, str(request.client_request_id), digest)
            if existing is None:
                raise
            return existing
        except BaseException:
            await session.rollback()
            await self.cleanup_photo(input_photo)
            raise
        return task

    async def retry(self, task_id: str, user_id: str, key: str, session: AsyncSession) -> RenderTask:
        """重试生成新任务，重复点击相同重试标识仍返回同一个新任务。"""
        old = await session.scalar(select(RenderTask).where(RenderTask.id == task_id).with_for_update())
        if old is None or old.user_id != user_id:
            await owned(session, RenderTask, task_id, user_id)
            raise AssertionError("unreachable")
        body = dict(old.snapshot["request"])
        body["clientRequestId"] = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{user_id}:{task_id}:{key}"))
        request = CreateRenderTask.model_validate(body)
        body = request.model_dump(mode="json", by_alias=True)
        digest = hashlib.sha256(json.dumps(body, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        existing = await self._existing(session, user_id, key, str(request.client_request_id), digest)
        if existing is not None:
            return existing
        if old.status != "FAILED" or not old.failure or not old.failure["retryable"]:
            raise AppError("RENDER_NOT_RETRYABLE", "该任务无法重试，请重新选择照片", status_code=409)
        if not self.settings.render_enabled or not self.settings.ark_api_key.get_secret_value():
            raise AppError("RENDER_NOT_ENABLED", "真实生成服务尚未启用，请联系管理员", status_code=503)
        source = await session.scalar(select(Photo).where(Photo.id == old.input_photo_id).with_for_update())
        if source is None or source.deleted_at is not None:
            raise AppError("RENDER_SOURCE_INVALID", "输入已清理，请重新选择照片", status_code=422)
        original = await self.storage.read(source.storage_key)
        snapshot = {**old.snapshot, "request": body}
        # 重试延续原输入与选材，不受源照片删除、目录下架或模型默认值变化影响。
        return await self._enqueue(request, user_id, key, session, original, snapshot, old.model, retry_of=old.id)

    async def result(self, result: RenderResult, session: AsyncSession) -> dict[str, Any]:
        """详情每次签发新 URL；源照片删除和目录下架不影响已生成快照。"""
        if result.deleted_at is not None:
            raise AppError("RESOURCE_NOT_FOUND", "效果记录已删除", status_code=404)
        before, after = (
            await session.get(Photo, result.before_photo_id),
            await session.get(Photo, result.after_photo_id),
        )
        if before is None or after is None:
            raise AppError("RESOURCE_NOT_FOUND", "效果图片不存在", status_code=404)
        expires = datetime.now(UTC) + timedelta(seconds=self.settings.photo_preview_ttl_seconds)
        return {
            "id": result.id,
            "taskId": result.task_id,
            "resultType": result.result_type,
            "width": result.width,
            "height": result.height,
            "solution": result.snapshot["solution"],
            "beforeImageUrl": await self.storage.signed_url(before.storage_key, before.id, expires),
            "afterImageUrl": await self.storage.signed_url(after.storage_key, after.id, expires),
            "imageUrlExpiresAt": expires.isoformat(),
            "createdAt": result.created_at.isoformat(),
            "disclaimer": "AI 生成效果仅供方案预览，实际效果以现场测量和施工为准",
        }

    async def list_results(self, user_id: str, page: int, size: int, session: AsyncSession) -> dict[str, Any]:
        """归属与墓碑过滤同时用于计数、分页和详情，排序包含 ID 以稳定翻页。"""
        conditions = (RenderResult.user_id == user_id, RenderResult.deleted_at.is_(None))
        total = await session.scalar(select(func.count()).select_from(RenderResult).where(*conditions)) or 0
        rows = (
            await session.scalars(
                select(RenderResult)
                .where(*conditions)
                .order_by(RenderResult.created_at.desc(), RenderResult.id.desc())
                .offset((page - 1) * size)
                .limit(size)
            )
        ).all()
        return {
            "items": [await self.result(item, session) for item in rows],
            "page": page,
            "size": size,
            "total": total,
            "hasNext": page * size < total,
        }

    async def delete_result(self, result_id: str, user_id: str, session: AsyncSession) -> None:
        """先禁用结果及其图片，再物理清理；失败保留墓碑供同一请求重试。"""
        result = await owned(session, RenderResult, result_id, user_id)
        photos = (
            await session.scalars(select(Photo).where(Photo.id.in_([result.before_photo_id, result.after_photo_id])))
        ).all()
        now = datetime.now(UTC)
        result.deleted_at = now
        for photo in photos:
            photo.deleted_at = now
        await session.commit()
        for photo in photos:
            await self.storage.delete(photo.storage_key)
            await self.storage.delete(photo.thumbnail_key)

    async def delete_task_input(self, task_id: str, user_id: str, session: AsyncSession) -> None:
        """失败任务也能清理私有输入；保留任务防止旧幂等请求再次生成。"""
        await session.scalar(select(RenderTask).where(RenderTask.id == task_id).with_for_update())
        task = await owned(session, RenderTask, task_id, user_id)
        if task.status in {"QUEUED", "PROCESSING"}:
            raise AppError("PHOTO_IN_USE", "任务尚未结束，请完成后清理", status_code=409)
        if task.result_id:
            await self.delete_result(task.result_id, user_id, session)
            return
        photo = await session.get(Photo, task.input_photo_id)
        if photo is None:
            return
        photo.deleted_at = datetime.now(UTC)
        if task.failure:
            task.failure = {**task.failure, "retryable": False}
        await session.commit()
        await self.storage.delete(photo.storage_key)
        await self.storage.delete(photo.thumbnail_key)
