"""生成任务与结果持久化；队列、幂等记录和结果归属均以数据库为准。"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, ForeignKey, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from huaye_window_ai.db.base import Base
from huaye_window_ai.db.models import utc_now


class RenderTask(Base):
    """一个提交只对应一个任务；租约 token 隔离迟到 worker 的状态写入。"""

    __tablename__ = "render_tasks"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_render_tasks_user_key"),
        UniqueConstraint("user_id", "client_request_id", name="uq_render_tasks_user_client"),
        Index("ix_render_tasks_queue", "status", "created_at"),
    )
    id: Mapped[str] = mapped_column(String(41), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(41), ForeignKey("users.id"), index=True)
    idempotency_key: Mapped[str] = mapped_column(String(128))
    client_request_id: Mapped[str] = mapped_column(String(36))
    request_hash: Mapped[str] = mapped_column(String(64))
    source_type: Mapped[str] = mapped_column(String(24))
    photo_id: Mapped[str | None] = mapped_column(String(41), ForeignKey("photos.id"), nullable=True, index=True)
    input_photo_id: Mapped[str] = mapped_column(String(41), ForeignKey("photos.id"))
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    model: Mapped[str] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(16), default="QUEUED")
    stage: Mapped[str] = mapped_column(String(16), default="QUEUED")
    failure: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    result_id: Mapped[str | None] = mapped_column(String(41), nullable=True)
    retry_of: Mapped[str | None] = mapped_column(String(41), nullable=True)
    worker_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class RenderResult(Base):
    """独立的原图快照与生成图不依赖源照片，删除采用墓碑支持存储清理重试。"""

    __tablename__ = "render_results"
    id: Mapped[str] = mapped_column(String(41), primary_key=True)
    task_id: Mapped[str] = mapped_column(String(41), ForeignKey("render_tasks.id"), unique=True)
    user_id: Mapped[str] = mapped_column(String(41), ForeignKey("users.id"), index=True)
    before_photo_id: Mapped[str] = mapped_column(String(41), ForeignKey("photos.id"))
    after_photo_id: Mapped[str] = mapped_column(String(41), ForeignKey("photos.id"))
    width: Mapped[int] = mapped_column(Integer)
    height: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    result_type: Mapped[str] = mapped_column(String(24))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
