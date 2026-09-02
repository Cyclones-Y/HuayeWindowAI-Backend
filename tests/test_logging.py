"""日志字段脱敏测试。"""

from huaye_window_ai.core.logging import redact


def test_redacts_nested_credentials_and_phone() -> None:
    value = {
        "code": "login-code",
        "nested": {"phoneCode": "phone-code", "phone": "13800138000"},
        "safe": "kept",
    }
    assert redact(value) == {
        "code": "[REDACTED]",
        "nested": {"phoneCode": "[REDACTED]", "phone": "[REDACTED]"},
        "safe": "kept",
    }
