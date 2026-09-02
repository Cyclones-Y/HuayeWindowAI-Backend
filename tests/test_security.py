"""手机号加密和规范化回归测试。"""

from huaye_window_ai.core.security import PhoneProtector, normalize_phone


def test_normalizes_chinese_phone() -> None:
    assert normalize_phone("86", "13800138000") == "+8613800138000"
    assert normalize_phone("86", "8613800138000") == "+8613800138000"


def test_phone_encryption_is_random_and_reversible() -> None:
    protector = PhoneProtector(b"e" * 32, b"h" * 32, 1)
    first = protector.protect("+8613800138000")
    second = protector.protect("+8613800138000")

    assert first.ciphertext != second.ciphertext
    assert first.lookup_hash == second.lookup_hash
    assert protector.reveal(first.ciphertext, first.nonce) == "+8613800138000"
