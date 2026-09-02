"""用户与微信身份绑定的数据访问逻辑。"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from huaye_window_ai.core.errors import AccountBindingConflictError
from huaye_window_ai.core.security import EncryptedPhone
from huaye_window_ai.db.models import User, WechatIdentity


def new_id(prefix: str) -> str:
    """前缀化 UUID 保证客户端始终把 ID 当作字符串处理。"""

    return f"{prefix}_{uuid.uuid4().hex}"


@dataclass(frozen=True, slots=True)
class ResolvedUser:
    user: User
    is_new_user: bool


class AuthRepository:
    """在单个事务中解析手机号账号并绑定微信身份。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_identity(self, app_id: str, open_id: str) -> WechatIdentity | None:
        result = await self._session.execute(
            select(WechatIdentity)
            .options(selectinload(WechatIdentity.user))
            .where(WechatIdentity.app_id == app_id, WechatIdentity.open_id == open_id)
        )
        return result.scalar_one_or_none()

    async def get_user_by_phone_hash(self, phone_hash: bytes) -> User | None:
        result = await self._session.execute(select(User).where(User.phone_hash == phone_hash))
        return result.scalar_one_or_none()

    async def resolve_existing_identity(
        self,
        identity: WechatIdentity,
        *,
        union_id: str | None,
        phone_hash: bytes | None,
    ) -> ResolvedUser:
        """已绑定身份不得借一次新的手机号授权静默切换到其他业务账号。"""

        if phone_hash is not None:
            phone_user = await self.get_user_by_phone_hash(phone_hash)
            if phone_user is None or phone_user.id != identity.user_id:
                raise AccountBindingConflictError
        identity.union_id = union_id or identity.union_id
        identity.last_login_at = datetime.now(UTC)
        return ResolvedUser(identity.user, False)

    async def bind_new_identity(
        self,
        *,
        app_id: str,
        open_id: str,
        union_id: str | None,
        protected_phone: EncryptedPhone,
        country_code: str,
        key_version: int,
    ) -> ResolvedUser:
        user = await self.get_user_by_phone_hash(protected_phone.lookup_hash)
        is_new_user = user is None
        if user is None:
            user = User(
                id=new_id("user"),
                phone_ciphertext=protected_phone.ciphertext,
                phone_nonce=protected_phone.nonce,
                phone_hash=protected_phone.lookup_hash,
                phone_country_code=country_code,
                phone_key_version=key_version,
                status="ACTIVE",
            )
            self._session.add(user)

        identity = WechatIdentity(
            id=new_id("wxid"),
            user=user,
            app_id=app_id,
            open_id=open_id,
            union_id=union_id,
        )
        self._session.add(identity)
        await self._session.flush()
        return ResolvedUser(user, is_new_user)
