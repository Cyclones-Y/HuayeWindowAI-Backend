"""验证统一前缀与资源配置，防止路径变更后登录、上传或 Swagger 继续使用旧地址。"""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from huaye_window_ai.core.config import Settings
from huaye_window_ai.main import create_app


@pytest.mark.parametrize("prefix", ["/v1", "/api/v1", ""])
def test_configured_prefix_applies_to_all_business_routes(settings: Settings, prefix: str) -> None:
    app = create_app(settings.model_copy(update={"api_prefix": prefix}))
    paths = app.openapi()["paths"]
    expected = {
        "/auth/wechat/login": "post",
        "/solutions": "get",
        "/solutions/{solutionId}": "get",
        "/sample-scenes": "get",
        "/photos": "post",
        "/photos/{photoId}": "delete",
        "/photos/{photoId}/preview": "get",
    }
    for path, method in expected.items():
        assert method in paths[f"{prefix}{path}"]
    assert "/health/live" in paths
    assert "/health/ready" in paths
    if prefix != "/v1":
        assert "/v1/solutions" not in paths
    # HTTP 验证健康探针保持原地址，图片静态目录也不会跟业务前缀一起移动。
    with TestClient(app) as client:
        assert client.get("/health/live").status_code == 200
        assert client.get("/catalog/living-before.jpg").status_code == 200
        assert client.get("/docs").status_code == 200


def test_api_prefix_is_read_from_configuration_file(tmp_path: Path) -> None:
    configuration = tmp_path / "api.env"
    configuration.write_text(
        "API_PREFIX=/api/v1/\nAPI_PUBLIC_BASE_URL=https://api.example.test/\n"
        "WECHAT_APP_ID=wx_test\nWECHAT_APP_SECRET=test-secret\n",
        encoding="utf-8",
    )
    settings = Settings(_env_file=configuration)
    assert settings.api_prefix == "/api/v1"
    assert settings.api_public_base_url == "https://api.example.test"
    assert settings.catalog_public_base_url == "https://api.example.test/catalog"


@pytest.mark.parametrize("prefix", ["api/v1", "/api//v1", "/api/../v1", "/api?x=1", "/api/{version}"])
def test_invalid_prefix_fails_at_startup(prefix: str) -> None:
    with pytest.raises(ValidationError, match="API_PREFIX"):
        Settings(_env_file=None, api_prefix=prefix)


@pytest.mark.parametrize("url", ["file:///private", "https://user:secret@api.test", "https://api.test?a=1"])
def test_resource_url_rejects_credentials_and_non_http_schemes(url: str) -> None:
    with pytest.raises(ValidationError, match="HTTP"):
        Settings(_env_file=None, api_public_base_url=url)


def test_storage_configuration_guards_missing_bucket_and_partial_credentials() -> None:
    with pytest.raises(ValidationError, match="PHOTO_S3_BUCKET"):
        Settings(_env_file=None, photo_storage_backend="s3")
    with pytest.raises(ValidationError, match="成对"):
        Settings(_env_file=None, photo_storage_backend="s3", photo_s3_bucket="photos", photo_s3_access_key_id="key")


def test_empty_optional_s3_values_use_sdk_defaults() -> None:
    """空 .env 配置不能传 endpoint_url=''，真实 SDK 会在第一次上传时报 Invalid endpoint。"""

    settings = Settings(
        _env_file=None,
        photo_storage_backend="s3",
        photo_s3_bucket="photos",
        photo_s3_endpoint_url=" ",
        photo_s3_region="",
    )
    assert settings.photo_s3_endpoint_url is None
    assert settings.photo_s3_region is None


def test_production_local_storage_requires_stable_key_and_https() -> None:
    with pytest.raises(ValidationError, match="PHOTO_URL_SIGNING_KEY"):
        Settings(_env_file=None, app_env="production", app_docs_enabled=False)
    with pytest.raises(ValidationError, match="HTTPS"):
        Settings(_env_file=None, app_env="production", app_docs_enabled=False, photo_url_signing_key="k" * 32)
    settings = Settings(
        _env_file=None,
        app_env="production",
        app_docs_enabled=False,
        api_public_base_url="https://api.example.test",
        photo_url_signing_key="k" * 32,
    )
    assert settings.catalog_public_base_url == "https://api.example.test/catalog"
