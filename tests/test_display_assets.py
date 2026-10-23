"""首页素材免登录签发；只允许数据库登记的公开目录，不能签发用户照片。"""

from collections.abc import AsyncIterator
from datetime import datetime

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from huaye_window_ai.core.config import Settings
from huaye_window_ai.db.base import Base
from huaye_window_ai.db.catalog_models import DisplayAsset
from huaye_window_ai.main import create_app


@pytest.fixture
async def display_client(settings: Settings) -> AsyncIterator[tuple[AsyncClient, object, list[str]]]:
    """真实 SQL 配合签名替身，确认公开入口不会获得任意私有对象的签名能力。"""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        session.add_all(
            [
                DisplayAsset(id="home-before", image_path="home-hero-before.jpg", available=True),
                DisplayAsset(id="home-after", image_path="home-hero-mobile.jpg", available=True),
                DisplayAsset(id="private", image_path="photos/private.jpg", available=True),
            ]
        )
    settings.photo_storage_backend = "oss"
    app = create_app(settings)
    app.state.settings = settings
    app.state.session_factory = factory
    signed = []

    class Storage:
        async def signed_url(self, key: str, owner: str, expires_at: datetime) -> str:
            signed.append(key)
            return f"https://oss.example.test/{key}?signature=synthetic"

    app.state.photo_storage = Storage()
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory, signed
    finally:
        await engine.dispose()


async def test_home_assets_are_public_but_only_sign_registered_pair(display_client) -> None:
    client, _, signed = display_client
    response = await client.get("/v1/display-assets/home?path=photos/private.jpg")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["beforeUrl"].startswith("https://oss.example.test/catalog/home-hero-before.jpg?")
    assert data["afterUrl"].startswith("https://oss.example.test/catalog/home-hero-mobile.jpg?")
    assert datetime.fromisoformat(data["expiresAt"])
    assert set(signed) == {"catalog/home-hero-before.jpg", "catalog/home-hero-mobile.jpg"}


@pytest.mark.parametrize("invalid_path", [None, "../photos/private.jpg"])
async def test_missing_or_invalid_home_asset_fails_without_partial_pair(display_client, invalid_path) -> None:
    """缺图/下架不能把不完整的前后对比当作成功，越界路径沿用目录校验。"""
    client, factory, _ = display_client
    async with factory() as session, session.begin():
        asset = await session.get(DisplayAsset, "home-before")
        if invalid_path is None:
            asset.available = False
        else:
            asset.image_path = invalid_path
    response = await client.get("/v1/display-assets/home")
    assert response.status_code == 503
