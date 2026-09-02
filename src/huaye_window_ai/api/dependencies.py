"""请求依赖与未来私有接口复用的 Bearer 鉴权入口。"""

from __future__ import annotations

from typing import cast

from fastapi import Depends, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from huaye_window_ai.core.errors import AppError
from huaye_window_ai.db.session import get_db_session
from huaye_window_ai.services.auth import AuthService
from huaye_window_ai.services.session import SessionStore

bearer = HTTPBearer(auto_error=False)


def get_auth_service(request: Request) -> AuthService:
    return cast(AuthService, request.app.state.auth_service)


async def get_current_user_id(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    session: AsyncSession = Depends(get_db_session),
) -> str:
    """验证 Redis 会话后再确认数据库用户仍为可用状态。"""

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise AppError("UNAUTHORIZED", "请先登录", status_code=401)
    store: SessionStore = request.app.state.session_store
    user_id = await store.get_user_id(credentials.credentials)
    if user_id is None:
        raise AppError("UNAUTHORIZED", "登录状态已失效，请重新登录", status_code=401)

    # 第一阶段尚无用户资料接口，但鉴权仍检查账号状态，避免 Redis TTL 内封禁失效。
    from huaye_window_ai.db.models import User

    user = await session.get(User, user_id)
    if user is None or user.status != "ACTIVE":
        raise AppError("UNAUTHORIZED", "登录状态已失效，请重新登录", status_code=401)
    return user_id
