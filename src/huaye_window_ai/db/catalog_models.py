"""方案目录与公开样例的持久化模型；可用状态由服务端统一控制。"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, Boolean, DateTime, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from huaye_window_ai.db.base import Base


def _utc_now() -> datetime:
    return datetime.now(UTC)


class WindowSolution(Base):
    """保存完整方案及有序配置项，避免前端缓存成为可用组合的事实来源。

    图片仅记录相对资源路径，本地/CDN 拼域名，OSS 模式按 catalog 前缀签发限时链接；
    JSON 数组保留展示顺序，数据库不持久化会过期的签名 URL。
    更新 JSON 配置时应整体赋值，SQLAlchemy 不跟踪原地修改嵌套列表的变化。
    """

    __tablename__ = "window_solutions"
    __table_args__ = (Index("ix_window_solutions_listing", "available", "room", "sort_order"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    subtitle: Mapped[str] = mapped_column(String(240), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    thumbnail_path: Mapped[str] = mapped_column(String(256), nullable=False)
    cover_path: Mapped[str] = mapped_column(String(256), nullable=False)
    gallery_paths: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    room: Mapped[str] = mapped_column(String(32), nullable=False)
    needs: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    features: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    pricing_note: Mapped[str] = mapped_column(String(240), nullable=False)
    frame_colors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    glass_options: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    default_frame_color_code: Mapped[str] = mapped_column(String(64), nullable=False)
    default_glass_code: Mapped[str] = mapped_column(String(64), nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )


class SampleScene(Base):
    """保存公开体验场景与归一化初始选区；图片尺寸必须来自实际发布资产。"""

    __tablename__ = "sample_scenes"
    __table_args__ = (Index("ix_sample_scenes_listing", "available", "sort_order"),)

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    scene_type: Mapped[str] = mapped_column(String(32), nullable=False)
    thumbnail_path: Mapped[str] = mapped_column(String(256), nullable=False)
    width: Mapped[int] = mapped_column(Integer, nullable=False)
    height: Mapped[int] = mapped_column(Integer, nullable=False)
    default_selection: Mapped[dict[str, float]] = mapped_column(JSON, nullable=False)
    available: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=_utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utc_now, onupdate=_utc_now
    )
