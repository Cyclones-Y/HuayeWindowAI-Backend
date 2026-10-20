"""在随机临时 MySQL 库验证迁移与生成竞态；不修改业务表，不输出 DSN 或密钥。

使用现有后端数据库连接参数连接服务器，必须具有新建临时数据库权限。
仅删除本脚本成功创建且名称严格匹配的库；可用 python scripts/verify_mysql_render.py 运行。
"""

import asyncio
import os
import re
import subprocess
import sys
import uuid

from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from alembic import command
from huaye_window_ai.core.config import get_settings
from huaye_window_ai.db.base import Base


async def database_action(name: str, *, create: bool) -> None:
    """只创建/删除由 UUID 构成的临时库；标识不含任意 SQL 或业务库名称。"""
    if not re.fullmatch(r"huaye_render_verify_[0-9a-f]{32}", name):
        raise ValueError("拒绝操作非临时数据库")
    target = make_url(get_settings().resolved_database_url).set(database=None)
    engine = create_async_engine(target, hide_parameters=True, isolation_level="AUTOCOMMIT")
    try:
        async with engine.connect() as connection:
            sql = (
                f"CREATE DATABASE `{name}` CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci"
                if create
                else f"DROP DATABASE `{name}`"
            )
            await connection.execute(text(sql))
    finally:
        await engine.dispose()


async def verify_parity(database_url: str) -> None:
    """迁移输出必须和 ORM 一致；Alembic 版本表不参与业务元数据比较。"""
    engine = create_async_engine(database_url, hide_parameters=True)
    try:
        async with engine.connect() as connection:

            def compare(sync_connection):
                context = MigrationContext.configure(
                    sync_connection, opts={"include_object": lambda obj, name, *args: name != "alembic_version"}
                )
                return compare_metadata(context, Base.metadata)

            differences = await connection.run_sync(compare)
            if differences:
                raise RuntimeError("临时库迁移与 ORM 不一致")
    finally:
        await engine.dispose()


def main() -> None:
    """先验证迁移往返，再用真实 MySQL 连接运行生成测试，最后回收独立库。"""
    name = "huaye_render_verify_" + uuid.uuid4().hex
    original_url = os.environ.get("DATABASE_URL")
    asyncio.run(database_action(name, create=True))
    try:
        database_url = (
            make_url(get_settings().resolved_database_url).set(database=name).render_as_string(hide_password=False)
        )
        os.environ["DATABASE_URL"] = database_url
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        asyncio.run(verify_parity(database_url))
        command.downgrade(config, "20260907_0002")
        command.upgrade(config, "head")
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "tests/test_render.py", "-q", "--tb=short"],
            env={**os.environ, "RENDER_TEST_DATABASE_URL": database_url},
            check=False,
        )
        if result.returncode:
            raise RuntimeError("MySQL 生成回归验证失败")
        print("MySQL 临时库迁移往返、ORM 一致性及生成回归通过")
    finally:
        if original_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = original_url
        asyncio.run(database_action(name, create=False))


if __name__ == "__main__":
    main()
