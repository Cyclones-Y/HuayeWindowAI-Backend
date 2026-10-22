"""迁移脚本默认只列计划，上传须显式开启；对象同名时不能覆盖已有不同内容。"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from huaye_window_ai.core.config import Settings
from huaye_window_ai.core.errors import AppError


def test_catalog_plan_preserves_relative_paths_and_prefix(tmp_path: Path) -> None:
    from huaye_window_ai.integrations.oss_migration import catalog_plan

    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "window.jpg").write_bytes(b"image")
    (tmp_path / "notes.txt").write_text("not an image")
    plan = catalog_plan(tmp_path, "catalog")
    assert plan == [("catalog/sub/window.jpg", tmp_path / "sub" / "window.jpg")]


async def test_upload_is_idempotent_but_never_replaces_different_remote_content(tmp_path: Path) -> None:
    from huaye_window_ai.integrations.oss_migration import upload_verified

    class ExistingStorage:
        """远端对象已存在时 OSS 拒绝写入；脚本须核对内容而不是忽略所有冲突。"""

        content = b"same"

        async def put(self, *args: object) -> None:
            raise AppError("PHOTO_STORAGE_UNAVAILABLE", "write rejected", status_code=503)

        async def read(self, key: str) -> bytes:
            return self.content

    source = tmp_path / "window.jpg"
    source.write_bytes(b"same")
    storage = ExistingStorage()
    await upload_verified(storage, "catalog/window.jpg", source)
    storage.content = b"different"
    with pytest.raises(ValueError, match="同名"):
        await upload_verified(storage, "catalog/window.jpg", source)


async def test_local_migration_only_includes_active_database_photos(
    tmp_path: Path, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """删除墓碑及磁盘孤儿不能在迁移后重新进入 OSS；扫描不得改动数据库中的路径。"""
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from huaye_window_ai.db.base import Base
    from huaye_window_ai.db.models import User
    from huaye_window_ai.db.photo_models import Photo
    from huaye_window_ai.integrations import oss_migration
    from huaye_window_ai.integrations.oss_migration import local_photo_plan

    database = f"sqlite+aiosqlite:///{tmp_path / 'migration.db'}"
    engine = create_async_engine(database)
    # 生产工厂固定执行 MySQL 时区语句；这里只替换连接边界，业务 SELECT 仍使用真实 SQLite。
    monkeypatch.setattr(oss_migration, "create_engine", create_async_engine)
    active = "photos/" + "a" * 32 + "/original.jpg"
    deleted = "photos/" + "b" * 32 + "/original.jpg"
    orphan = "photos/" + "c" * 32 + "/original.jpg"
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        async with async_sessionmaker(engine)() as session, session.begin():
            session.add(User(id="u1", phone_number="synthetic", phone_country_code="86"))
            for key, removed in [(active, False), (deleted, True)]:
                session.add(
                    Photo(
                        id="photo_" + ("b" if removed else "a") * 32,
                        user_id="u1",
                        purpose="WINDOW_PREVIEW",
                        storage_key=key,
                        thumbnail_key=key,
                        width=1,
                        height=1,
                        size_bytes=1,
                        mime_type="image/jpeg",
                        deleted_at=datetime.now(UTC) if removed else None,
                    )
                )
        for key in [active, deleted, orphan]:
            path = tmp_path / key
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"x")
        configured = settings.model_copy(update={"database_url": database, "photo_local_directory": tmp_path})
        assert await local_photo_plan(configured) == [(active, tmp_path / active)]
    finally:
        await engine.dispose()
