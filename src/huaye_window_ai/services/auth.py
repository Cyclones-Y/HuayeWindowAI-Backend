"""微信身份解析、手机号账号归并和会话签发的业务编排。"""

from __future__ import annotations

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from huaye_window_ai.core.errors import AppError, PhoneAuthRequiredError, WechatCodeInvalidError
from huaye_window_ai.core.security import PhoneProtector, normalize_phone
from huaye_window_ai.integrations.wechat import WechatClient, WechatIdentityData, WechatPhoneData
from huaye_window_ai.repositories.auth import AuthRepository, ResolvedUser
from huaye_window_ai.schemas.auth import LoginUser, WechatLoginData, WechatLoginRequest
from huaye_window_ai.services.session import SessionStore


class AuthService:
    """实现无注册页的一键登录，并用数据库唯一约束处理并发归并。"""

    def __init__(
        self,
        wechat: WechatClient,
        sessions: SessionStore,
        phone_protector: PhoneProtector,
        *,
        app_id: str,
    ) -> None:
        self._wechat = wechat
        self._sessions = sessions
        self._phone_protector = phone_protector
        self._app_id = app_id

    async def login(self, request: WechatLoginRequest, session: AsyncSession) -> WechatLoginData:
        identity_data = await self._wechat.code_to_identity(request.code)
        phone_data = await self._wechat.get_phone_number(request.phone_code) if request.phone_code else None

        resolved = await self._resolve_user(session, identity_data, phone_data)
        if resolved.user.status != "ACTIVE":
            raise AppError("ACCOUNT_DISABLED", "账号当前不可用", status_code=403)

        issued = await self._sessions.issue(resolved.user.id, request.device_id)
        return WechatLoginData(
            access_token=issued.token,
            expires_in=issued.expires_in,
            is_new_user=resolved.is_new_user,
            user=LoginUser(id=resolved.user.id),
        )

    async def _resolve_user(
        self,
        session: AsyncSession,
        identity_data: WechatIdentityData,
        phone_data: WechatPhoneData | None,
    ) -> ResolvedUser:
        normalized_phone: str | None = None
        protected_phone = None
        if phone_data:
            try:
                normalized_phone = normalize_phone(phone_data.country_code, phone_data.pure_phone_number)
            except ValueError:
                raise WechatCodeInvalidError from None
            protected_phone = self._phone_protector.protect(normalized_phone)

        # 同一手机号或 OpenID 的并发首登可能命中唯一索引；回滚后重读一次即可收敛到已创建账号。
        for attempt in range(2):
            repository = AuthRepository(session)
            try:
                identity = await repository.get_by_identity(self._app_id, identity_data.open_id)
                if identity:
                    resolved = await repository.resolve_existing_identity(
                        identity,
                        union_id=identity_data.union_id,
                        phone_hash=protected_phone.lookup_hash if protected_phone else None,
                    )
                else:
                    if protected_phone is None or phone_data is None:
                        raise PhoneAuthRequiredError
                    resolved = await repository.bind_new_identity(
                        app_id=self._app_id,
                        open_id=identity_data.open_id,
                        union_id=identity_data.union_id,
                        protected_phone=protected_phone,
                        country_code=phone_data.country_code,
                        key_version=self._phone_protector.key_version,
                    )
                await session.commit()
                return resolved
            except IntegrityError:
                await session.rollback()
                if attempt == 1:
                    raise AppError("LOGIN_CONFLICT", "登录状态冲突，请重试", status_code=409) from None
            except Exception:
                await session.rollback()
                raise

        raise RuntimeError("unreachable")
