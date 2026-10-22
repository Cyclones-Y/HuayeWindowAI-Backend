"""HTTP 响应、参数错误、链路标识和本地启动入口测试。"""

from typing import Any

from fastapi.testclient import TestClient
from loguru import logger

from huaye_window_ai import main as main_module
from huaye_window_ai.core.errors import WechatCodeInvalidError
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


def test_access_log_contains_status_and_excludes_query(settings: object) -> None:
    app = create_app(settings)  # type: ignore[arg-type]

    @app.get("/test-success")
    async def test_success() -> dict[str, bool]:
        return {"ok": True}

    records: list[dict[str, Any]] = []
    with TestClient(app) as client:
        # sink 必须在 lifespan 完成日志初始化后注册，否则 configure_logging 会按设计清理旧 sink。
        sink_id = logger.add(lambda message: records.append(message.record), enqueue=False)
        try:
            response = client.get("/test-success?temporary=value")
        finally:
            logger.remove(sink_id)

    assert response.status_code == 200
    # 周期维护会并发产生日志；只选择访问记录，不能假定每条日志都带结构化 fields。
    completed = next(record for record in records if (record["extra"].get("fields") or {}).get("statusCode") == 200)
    assert completed["extra"]["fields"]["path"] == "/test-success"
    assert completed["extra"]["fields"]["statusCode"] == 200
    assert "temporary" not in str(completed["extra"]["fields"])


def test_failed_access_log_contains_business_reason(settings: object) -> None:
    """非 2xx 访问日志必须给出安全的业务错误码和原因，不能只显示 HTTP 状态码。"""

    app = create_app(settings)  # type: ignore[arg-type]

    @app.post("/test-wechat-login-error")
    async def test_wechat_login_error() -> None:
        raise WechatCodeInvalidError()

    records: list[dict[str, Any]] = []
    with TestClient(app) as client:
        sink_id = logger.add(lambda message: records.append(message.record), enqueue=False)
        try:
            response = client.post("/test-wechat-login-error")
        finally:
            logger.remove(sink_id)

    assert response.status_code == 401
    failed = next(record for record in records if (record["extra"].get("fields") or {}).get("statusCode") == 401)
    assert failed["extra"]["fields"]["errorCode"] == "WECHAT_CODE_INVALID"
    assert failed["extra"]["fields"]["errorMessage"] == "微信登录凭证已失效，请重试"
    assert "WECHAT_CODE_INVALID" in failed["message"]
    assert "微信登录凭证已失效，请重试" in failed["message"]


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
        "access_log": False,
        "log_config": None,
    }
