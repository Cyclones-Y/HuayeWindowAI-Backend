"""第一阶段的用户与微信身份持久化模型。"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DateTime, ForeignKey, Index, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from huaye_window_ai.db.base import Base


def utc_now() -> datetime:
    return datetime.now(UTC)


class User(Base):
    """业务用户以规范化手机号归并，不暴露密码或注册状态。"""

    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(41), primary_key=True)
    phone_number: Mapped[str] = mapped_column(String(24), nullable=False, unique=True)
    phone_country_code: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ACTIVE")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utc_now, onupdate=utc_now
    )

    identities: Mapped[list[WechatIdentity]] = relationship(back_populates="user", cascade="all, delete-orphan")


class WechatIdentity(Base):
    """微信身份单独建表，允许同一手机号在不同微信身份或未来多个 AppID 下恢复账号。"""

    __tablename__ = "wechat_identities"
    __table_args__ = (
        UniqueConstraint("app_id", "open_id", name="uq_wechat_identities_app_open"),
        Index("ix_wechat_identities_union_id", "union_id"),
    )

    id: Mapped[str] = mapped_column(String(41), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        String(41), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    app_id: Mapped[str] = mapped_column(String(32), nullable=False)
    open_id: Mapped[str] = mapped_column(String(128), nullable=False)
    union_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)
    last_login_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utc_now)

    user: Mapped[User] = relationship(back_populates="identities")
