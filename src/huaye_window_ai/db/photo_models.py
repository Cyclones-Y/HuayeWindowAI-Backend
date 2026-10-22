"""私有现场照片的归属、规范化尺寸和删除状态；文件内容保存在存储适配器。"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from huaye_window_ai.db.base import Base
from huaye_window_ai.db.models import utc_now


class Photo(Base):
    """保留删除标记以支持幂等删除和清理重试，绝不持久化会过期的访问 URL。"""

    __tablename__ = "photos"

    id: Mapped[str] = mapped_column(String(41), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(41), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    thumbnail_key: Mapped[str] = mapped_column(String(512), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PhotoWriteIntent(Base):
    """文件写入前独立提交的登记；无外键，业务事务回滚或进程退出后仍能定位残留对象。

    写入方必须在业务事务中锁住登记，并与 Photo 一起删除登记；维护任务只回收到期且未被
    写入方锁定的登记。这里不保存图片、账户信息或签名 URL。
    """

    __tablename__ = "photo_write_intents"

    photo_id: Mapped[str] = mapped_column(String(41), primary_key=True)
    storage_key: Mapped[str] = mapped_column(String(512), nullable=False)
    thumbnail_key: Mapped[str] = mapped_column(String(512), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, nullable=False, index=True)
    next_attempt_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utc_now, nullable=False, index=True
    )
