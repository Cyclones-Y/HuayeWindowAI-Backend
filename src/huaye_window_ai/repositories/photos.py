"""照片数据访问；私有操作始终把当前用户归属写入查询条件。"""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from huaye_window_ai.db.photo_models import Photo


class PhotoRepository:
    """不提交事务，由服务协调存储写入与数据库状态的先后顺序。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, photo: Photo) -> None:
        """提前 flush 暴露持久化失败，让调用方清理已经写入的私有文件。"""

        self._session.add(photo)
        await self._session.flush()

    async def get_owned(self, photo_id: str, user_id: str) -> Photo | None:
        """包含墓碑记录，使所有者可以重试删除；所有业务字段只在归属条件满足时读取。"""

        result = await self._session.execute(select(Photo).where(Photo.id == photo_id, Photo.user_id == user_id))
        return result.scalar_one_or_none()

    async def exists(self, photo_id: str) -> bool:
        """仅分类文档约定的 403/404 错误，不读取其他用户的照片属性或存储位置。"""

        result = await self._session.execute(select(Photo.id).where(Photo.id == photo_id))
        return result.scalar_one_or_none() is not None

    async def get_active(self, photo_id: str) -> Photo | None:
        """仅供验证过签名的本地预览使用；删除后旧签名立即失效。"""

        result = await self._session.execute(select(Photo).where(Photo.id == photo_id, Photo.deleted_at.is_(None)))
        return result.scalar_one_or_none()
