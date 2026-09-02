"""HTTP 响应、参数错误和链路标识测试。"""

from fastapi.testclient import TestClient

from huaye_window_ai.main import create_app


def test_liveness_has_request_id(settings: object) -> None:
    app = create_app(settings)  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.get("/health/live", headers={"X-Request-Id": "test_request_123"})
    assert response.status_code == 200
    assert response.json() == {"status": "UP", "requestId": "test_request_123"}
    assert response.headers["X-Request-Id"] == "test_request_123"


def test_login_validation_does_not_echo_codes(settings: object) -> None:
    app = create_app(settings)  # type: ignore[arg-type]
    with TestClient(app) as client:
        response = client.post(
            "/v1/auth/wechat/login",
            json={"code": "secret-login-code", "phoneCode": "secret-phone-code", "deviceId": "bad space"},
        )
    body = response.json()
    assert response.status_code == 400
    assert body["code"] == "INVALID_ARGUMENT"
    assert "secret-login-code" not in response.text
    assert "secret-phone-code" not in response.text
