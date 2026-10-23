"""目录只读查询及显式种子导入；业务请求不会自动写入或重置商品配置。"""

from __future__ import annotations

import json
from importlib.resources import files

from sqlalchemy import ColumnElement, String, cast, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from huaye_window_ai.db.catalog_models import DisplayAsset, SampleScene, WindowSolution
from huaye_window_ai.schemas.catalog import NeedType, RoomType


class CatalogRepository:
    """统一执行上架过滤和稳定排序，确保列表与详情不会暴露已下架方案。"""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def home_display_assets(self) -> dict[str, str]:
        """固定查询首页前后图，不接受客户端对象路径，避免公开接口变成任意签名服务。"""
        rows = await self.session.scalars(
            select(DisplayAsset).where(
                DisplayAsset.id.in_(("home-before", "home-after")), DisplayAsset.available.is_(True)
            )
        )
        return {row.id: row.image_path for row in rows}

    async def list_solutions(
        self,
        *,
        room: RoomType | None,
        need: NeedType | None,
        keyword: str | None,
        page: int,
        size: int,
    ) -> tuple[list[WindowSolution], int]:
        """先按同一组条件计数，再做数据库分页，避免全部载入内存后截断。"""

        conditions: list[ColumnElement[bool]] = [WindowSolution.available.is_(True)]
        if room is not None:
            conditions.append(WindowSolution.room == room.value)
        if need is not None:
            # 匹配 JSON 中带引号的完整枚举，既兼容 MySQL/SQLite，也不会把相似代码误当成同一需求。
            conditions.append(cast(WindowSolution.needs, String).contains(json.dumps(need.value), autoescape=True))
        if keyword and (term := keyword.strip()):
            # 转义 LIKE 通配符，用户输入的 %、_ 只表示字面文本，不扩大匹配范围。
            conditions.append(
                or_(
                    WindowSolution.name.contains(term, autoescape=True),
                    WindowSolution.subtitle.contains(term, autoescape=True),
                    WindowSolution.description.contains(term, autoescape=True),
                )
            )
        total = await self.session.scalar(select(func.count()).select_from(WindowSolution).where(*conditions))
        rows = await self.session.scalars(
            select(WindowSolution)
            .where(*conditions)
            .order_by(WindowSolution.sort_order, WindowSolution.id)
            .offset((page - 1) * size)
            .limit(size)
        )
        return list(rows), int(total or 0)

    async def get_solution(self, solution_id: str) -> WindowSolution | None:
        """对不存在和下架的方案统一返回空，调用方映射为同一种 404。"""

        rows = await self.session.scalars(
            select(WindowSolution).where(WindowSolution.id == solution_id, WindowSolution.available.is_(True))
        )
        return rows.one_or_none()

    async def list_sample_scenes(self) -> list[SampleScene]:
        """样例为小规模运营目录，仅公开可用项，顺序由服务端维护。"""

        rows = await self.session.scalars(
            select(SampleScene).where(SampleScene.available.is_(True)).order_by(SampleScene.sort_order, SampleScene.id)
        )
        return list(rows)


async def seed_catalog(session: AsyncSession, *, replace: bool = False) -> dict[str, int]:
    """导入随包发布的已审查目录，不提交事务，由脚本或测试控制原子性。

    默认只补缺失主键，保护运营已调整的上下架、默认配置与排序；仅显式 replace 才覆盖种子项。
    数据复用小程序 data/solutions.ts，描述沿用原副标题，避免虚构价格或检测结论。
    """

    data = json.loads(files("huaye_window_ai").joinpath("data/catalog.json").read_text(encoding="utf-8"))
    counts = {"solutions": 0, "sampleScenes": 0}
    for key, model in (("solutions", WindowSolution), ("sampleScenes", SampleScene)):
        for item in data[key]:
            existing = await session.get(model, item["id"])
            if existing is None:
                session.add(model(**item))
                counts[key] += 1
            elif replace:
                for field, value in item.items():
                    if field != "id":
                        setattr(existing, field, value)
                counts[key] += 1
    await session.flush()
    return counts
