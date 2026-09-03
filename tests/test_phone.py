"""手机号规范化回归测试。"""

import pytest

from huaye_window_ai.core.phone import normalize_phone


def test_normalizes_chinese_phone() -> None:
    assert normalize_phone("86", "13800138000") == "+8613800138000"
    assert normalize_phone("86", "8613800138000") == "+8613800138000"


def test_rejects_invalid_phone() -> None:
    with pytest.raises(ValueError, match="手机号格式无效"):
        normalize_phone("86", "not-a-phone")
