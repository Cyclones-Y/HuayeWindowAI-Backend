"""显式导入方案与样例种子：先执行 Alembic 迁移，再运行本脚本。

默认仅新增缺失主键，不覆盖运营编辑；--replace 用于明确重置种子内方案的配置。
"""

from __future__ import annotations

import argparse
import asyncio

from huaye_window_ai.core.config import DatabaseSettings
from huaye_window_ai.db.session import create_engine, create_session_factory
from huaye_window_ai.repositories.catalog import seed_catalog


async def run(*, replace: bool) -> None:
    """在单个事务中写入目录；数据库配置不要求微信凭证或 Redis 服务可用。"""

    config = DatabaseSettings()
    engine = create_engine(config.resolved_database_url)
    try:
        factory = create_session_factory(engine)
        async with factory() as session, session.begin():
            counts = await seed_catalog(session, replace=replace)
        print(f"目录导入完成：方案 {counts['solutions']} 条，样例 {counts['sampleScenes']} 条")
    finally:
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="导入小程序现有方案与公开样例")
    parser.add_argument("--replace", action="store_true", help="显式覆盖已有种子项（包含上下架与排序）")
    arguments = parser.parse_args()
    asyncio.run(run(replace=arguments.replace))
