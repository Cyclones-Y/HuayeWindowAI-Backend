"""微信手机号规范化。"""

from __future__ import annotations

import re

COUNTRY_CODE_DIGITS = re.compile(r"^\d{1,4}$")
PHONE_DIGITS = re.compile(r"^\d{6,20}$")


def normalize_phone(country_code: str, pure_phone_number: str) -> str:
    """将微信返回的国家码和纯号码规范成稳定的 E.164 风格数据库值。"""

    country = country_code.strip().removeprefix("+")
    number = pure_phone_number.strip().replace(" ", "").replace("-", "")
    if not COUNTRY_CODE_DIGITS.fullmatch(country) or not PHONE_DIGITS.fullmatch(number):
        raise ValueError("手机号格式无效")
    if number.startswith(country):
        return f"+{number}"
    return f"+{country}{number}"
