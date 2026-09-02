"""微信手机号登录接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from huaye_window_ai.api.dependencies import get_auth_service
from huaye_window_ai.core.responses import ApiResponse
from huaye_window_ai.db.session import get_db_session
from huaye_window_ai.schemas.auth import WechatLoginData, WechatLoginRequest
from huaye_window_ai.services.auth import AuthService
from huaye_window_ai.services.rate_limit import LoginRateLimiter

router = APIRouter(prefix="/auth/wechat", tags=["认证"])


@router.post("/login", response_model=ApiResponse[WechatLoginData])
async def wechat_login(
    payload: WechatLoginRequest,
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    service: AuthService = Depends(get_auth_service),
) -> ApiResponse[WechatLoginData]:
    """首次用手机号自动建档，已绑定微信身份后可仅凭新的 wx.login code 登录。"""

    limiter: LoginRateLimiter = request.app.state.login_rate_limiter
    client_ip = request.client.host if request.client else "unknown"
    await limiter.check(client_ip, payload.device_id)
    data = await service.login(payload, session)
    return ApiResponse(data=data, request_id=request.state.request_id)
