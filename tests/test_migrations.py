"""在独立临时数据库验证第一批表迁移；不连接开发者的 MySQL 或修改已有数据。"""

from pathlib import Path

import pytest
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import create_engine, inspect

from alembic import command
from huaye_window_ai.db.base import Base


def test_catalog_photo_migration_round_trip_and_model_parity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """升级结果必须与 ORM 一致，回退只能移除本阶段三张表。"""

    database = tmp_path / "migration.sqlite"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{database.as_posix()}")
    config = Config("alembic.ini")
    command.upgrade(config, "head")
    engine = create_engine(f"sqlite:///{database.as_posix()}")
    try:
        with engine.connect() as connection:
            tables = set(inspect(connection).get_table_names())
            assert {"users", "wechat_identities", "photos", "window_solutions", "sample_scenes"} <= tables
            context = MigrationContext.configure(
                connection,
                opts={"include_object": lambda obj, name, *args: name != "alembic_version"},
            )
            assert compare_metadata(context, Base.metadata) == []
        command.downgrade(config, "20260902_0001")
        assert set(inspect(engine).get_table_names()) == {"users", "wechat_identities", "alembic_version"}
        command.upgrade(config, "head")
        assert "photos" in inspect(engine).get_table_names()
    finally:
        engine.dispose()
