"""HTTP 响应、参数错误、链路标识和本地启动入口测试。"""

from typing import Any

from fastapi.testclient import TestClient

from huaye_window_ai import main as main_module
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


def test_main_starts_uvicorn_with_app_settings(monkeypatch: Any, settings: Any) -> None:
    """IDE 直接运行入口必须沿用环境配置，并避免启动真实监听端口。"""

    captured: dict[str, Any] = {}

    def fake_run(app: object, **kwargs: object) -> None:
        captured.update({"app": app, **kwargs})

    monkeypatch.setattr(main_module, "get_settings", lambda: settings)
    monkeypatch.setattr(main_module.uvicorn, "run", fake_run)

    main_module.main()

    assert captured == {
        "app": main_module.app,
        "host": settings.app_host,
        "port": settings.app_port,
        "log_level": settings.app_log_level.lower(),
    }
