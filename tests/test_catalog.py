"""目录公开契约、交集筛选、分页边界和下架隐藏的 HTTP 回归测试。"""

from __future__ import annotations

import io
from collections.abc import AsyncIterator
from urllib.parse import urlsplit

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from huaye_window_ai.core.config import Settings
from huaye_window_ai.db.base import Base
from huaye_window_ai.db.catalog_models import SampleScene, WindowSolution
from huaye_window_ai.main import create_app
from huaye_window_ai.repositories.catalog import seed_catalog


@pytest.fixture
async def catalog_client(settings: Settings) -> AsyncIterator[tuple[AsyncClient, async_sessionmaker[AsyncSession]]]:
    """使用真实 SQL 查询和种子，只替换数据库连接，避免测试依赖 MySQL/Redis 服务。"""

    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with factory() as session, session.begin():
        await seed_catalog(session)
    settings.catalog_public_base_url = "https://static.example.test/windows/"
    app = create_app(settings)
    app.state.settings = settings
    app.state.session_factory = factory
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
            yield client, factory
    finally:
        await engine.dispose()


async def test_public_solution_pagination(
    catalog_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """分页必须按运营排序返回确定结果，并正确表示末页与越界空页。"""

    client, _ = catalog_client
    first = await client.get("/v1/solutions?size=2", headers={"X-Request-Id": "catalog_request_123"})
    assert first.status_code == 200
    body = first.json()
    assert body["requestId"] == "catalog_request_123"
    data = body["data"]
    assert [item["id"] for item in data["items"]] == ["solution-living-panorama", "solution-bedroom-quiet"]
    assert data["items"][0]["thumbnailUrl"] == "https://static.example.test/windows/living-after-thumb.jpg"
    assert {key: data[key] for key in ("page", "size", "total", "hasNext")} == {
        "page": 1,
        "size": 2,
        "total": 3,
        "hasNext": True,
    }
    second = (await client.get("/v1/solutions?page=2&size=2")).json()["data"]
    assert [item["id"] for item in second["items"]] == ["solution-balcony-seal"]
    assert second["hasNext"] is False
    beyond = (await client.get("/v1/solutions?page=3&size=2")).json()["data"]
    assert beyond["items"] == []
    assert beyond["total"] == 3
    assert beyond["hasNext"] is False


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        ({"room": "BEDROOM"}, ["solution-bedroom-quiet"]),
        ({"need": "QUIET"}, ["solution-living-panorama", "solution-bedroom-quiet"]),
        ({"room": "BALCONY", "need": "QUIET"}, []),
        ({"room": "BEDROOM", "need": "SAFETY", "keyword": "临街"}, ["solution-bedroom-quiet"]),
        ({"keyword": "  通景  "}, ["solution-balcony-seal"]),
        ({"keyword": "%"}, []),
        ({"keyword": "_"}, []),
    ],
)
async def test_solution_filters(
    catalog_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]], query: dict[str, str], expected: list[str]
) -> None:
    """组合条件取交集；通配符按字面搜索，防止用户输入意外获得全部目录。"""

    client, _ = catalog_client
    response = await client.get("/v1/solutions", params=query)
    assert response.status_code == 200
    data = response.json()["data"]
    assert [item["id"] for item in data["items"]] == expected
    assert data["total"] == len(expected)


@pytest.mark.parametrize("query", ["size=51", "size=0", "page=0", "room=KITCHEN", "need=UNKNOWN"])
async def test_invalid_catalog_parameters(
    catalog_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]], query: str
) -> None:
    client, _ = catalog_client
    response = await client.get(f"/v1/solutions?{query}")
    assert response.status_code == 400
    assert response.json()["code"] == "INVALID_ARGUMENT"


async def test_solution_detail_preserves_option_order_and_codes(
    catalog_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, _ = catalog_client
    response = await client.get("/v1/solutions/solution-bedroom-quiet")
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["defaultFrameColorCode"] == "DARK_COFFEE"
    assert data["defaultGlassCode"] == "TRIPLE_LAMINATED"
    assert data["frameColors"][0] == {
        "code": "DARK_COFFEE",
        "label": "深咖灰",
        "colorHex": "#554f48",
        "available": True,
    }
    assert [item["code"] for item in data["glassOptions"]] == ["TRIPLE_LAMINATED", "LAMINATED_HOLLOW"]
    assert data["coverUrl"] == "https://static.example.test/windows/bedroom-after.jpg"
    assert data["galleryUrls"] == []
    assert data["pricingNote"] == "量尺后按隔音配置报价"


async def test_unavailable_solution_and_sample_are_hidden(
    catalog_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """公开查询必须统一隐藏下架项，旧链接不能绕过列表的可用性过滤。"""

    client, factory = catalog_client
    async with factory() as session, session.begin():
        await session.execute(
            update(WindowSolution).where(WindowSolution.id == "solution-bedroom-quiet").values(available=False)
        )
        await session.execute(update(SampleScene).where(SampleScene.id == "sample_bedroom_01").values(available=False))
    listing = (await client.get("/v1/solutions")).json()["data"]
    assert listing["total"] == 2
    for solution_id in ("solution-bedroom-quiet", "not-found"):
        response = await client.get(f"/v1/solutions/{solution_id}")
        assert response.status_code == 404
        assert response.json()["code"] == "RESOURCE_NOT_FOUND"
    scenes = (await client.get("/v1/sample-scenes")).json()["data"]["items"]
    assert [item["id"] for item in scenes] == ["sample_living_room_01", "sample_balcony_01"]


async def test_samples_publish_actual_dimensions_and_default_selection(
    catalog_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    client, _ = catalog_client
    response = await client.get("/v1/sample-scenes")
    assert response.status_code == 200
    scenes = response.json()["data"]["items"]
    assert len(scenes) == 3
    assert scenes[0] == {
        "id": "sample_living_room_01",
        "name": "客厅落地窗",
        "sceneType": "LIVING_ROOM",
        "thumbnailUrl": "https://static.example.test/windows/living-before.jpg",
        "width": 810,
        "height": 1215,
        "defaultSelection": {"x": 0.12, "y": 0.12, "width": 0.76, "height": 0.7},
    }


async def test_repeated_seed_preserves_edited_availability(
    catalog_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """重复部署导入种子不能重新上架被运营下架的方案，显式 replace 才重置。"""

    _, factory = catalog_client
    async with factory() as session, session.begin():
        await session.execute(
            update(WindowSolution).where(WindowSolution.id == "solution-bedroom-quiet").values(available=False)
        )
        assert await seed_catalog(session) == {"solutions": 0, "sampleScenes": 0}
    async with factory() as session:
        hidden = await session.get(WindowSolution, "solution-bedroom-quiet")
        assert hidden is not None and hidden.available is False
    async with factory() as session, session.begin():
        assert await seed_catalog(session, replace=True) == {"solutions": 3, "sampleScenes": 3}
    async with factory() as session:
        restored = await session.get(WindowSolution, "solution-bedroom-quiet")
        assert restored is not None and restored.available is True


async def test_seed_options_and_bundled_images_are_consistent(
    catalog_client: tuple[AsyncClient, async_sessionmaker[AsyncSession]],
) -> None:
    """真实种子默认项必须可选，资源必须随应用可访问，防止目录可查询但无法展示或生成。"""

    client, _ = catalog_client
    summaries = (await client.get("/v1/solutions")).json()["data"]["items"]
    for summary in summaries:
        detail = (await client.get(f"/v1/solutions/{summary['id']}")).json()["data"]
        assert detail["defaultFrameColorCode"] in {item["code"] for item in detail["frameColors"] if item["available"]}
        assert detail["defaultGlassCode"] in {item["code"] for item in detail["glassOptions"] if item["available"]}
        for url in (summary["thumbnailUrl"], detail["coverUrl"], *detail["galleryUrls"]):
            filename = urlsplit(url).path.rsplit("/", maxsplit=1)[-1]
            asset = await client.get(f"/catalog/{filename}")
            assert asset.status_code == 200
            assert asset.headers["content-type"] == "image/jpeg"
    scenes = (await client.get("/v1/sample-scenes")).json()["data"]["items"]
    for scene in scenes:
        filename = urlsplit(scene["thumbnailUrl"]).path.rsplit("/", maxsplit=1)[-1]
        asset = await client.get(f"/catalog/{filename}")
        assert asset.status_code == 200
        with Image.open(io.BytesIO(asset.content)) as image:
            assert image.size == (scene["width"], scene["height"])
