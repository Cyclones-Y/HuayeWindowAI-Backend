"""手机号规范化、加密与不可逆检索摘要。"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

COUNTRY_CODE_DIGITS = re.compile(r"^\d{1,4}$")
PHONE_DIGITS = re.compile(r"^\d{6,20}$")


def normalize_phone(country_code: str, pure_phone_number: str) -> str:
    """将微信返回的国家码和纯号码规范成稳定的 E.164 风格检索值。"""

    country = country_code.strip().removeprefix("+")
    number = pure_phone_number.strip().replace(" ", "").replace("-", "")
    if not COUNTRY_CODE_DIGITS.fullmatch(country) or not PHONE_DIGITS.fullmatch(number):
        raise ValueError("手机号格式无效")
    if number.startswith(country):
        return f"+{number}"
    return f"+{country}{number}"


@dataclass(frozen=True, slots=True)
class EncryptedPhone:
    ciphertext: bytes
    nonce: bytes
    lookup_hash: bytes


class PhoneProtector:
    """用随机 nonce 加密手机号，并用独立 HMAC 支持等值查询和唯一约束。"""

    _AAD_PREFIX = b"huaye-phone:v"

    def __init__(self, encryption_key: bytes, lookup_hmac_key: bytes, key_version: int) -> None:
        self._aes = AESGCM(encryption_key)
        self._lookup_hmac_key = lookup_hmac_key
        self.key_version = key_version

    def protect(self, normalized_phone: str) -> EncryptedPhone:
        nonce = os.urandom(12)
        plaintext = normalized_phone.encode("utf-8")
        ciphertext = self._aes.encrypt(nonce, plaintext, self._aad())
        return EncryptedPhone(ciphertext, nonce, self.lookup_hash(normalized_phone))

    def reveal(self, ciphertext: bytes, nonce: bytes) -> str:
        return self._aes.decrypt(nonce, ciphertext, self._aad()).decode("utf-8")

    def lookup_hash(self, normalized_phone: str) -> bytes:
        return hmac.new(self._lookup_hmac_key, normalized_phone.encode("utf-8"), hashlib.sha256).digest()

    def _aad(self) -> bytes:
        # 将版本写入认证附加数据，防止密文被错误地按另一套密钥版本解释。
        return self._AAD_PREFIX + str(self.key_version).encode("ascii")
