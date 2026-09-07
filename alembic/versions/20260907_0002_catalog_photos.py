"""建立方案目录、样例场景与用户私有照片资产。

Revision ID: 20260907_0002
Revises: 20260902_0001
Create Date: 2026-09-07
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260907_0002"
down_revision: str | None = "20260902_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """目录配置保存稳定代码；照片只记录私有对象键，不持久化即将过期的签名 URL。"""

    op.create_table(
        "window_solutions",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("subtitle", sa.String(240), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("thumbnail_path", sa.String(256), nullable=False),
        sa.Column("cover_path", sa.String(256), nullable=False),
        sa.Column("gallery_paths", sa.JSON(), nullable=False),
        sa.Column("room", sa.String(32), nullable=False),
        sa.Column("needs", sa.JSON(), nullable=False),
        sa.Column("features", sa.JSON(), nullable=False),
        sa.Column("frame_colors", sa.JSON(), nullable=False),
        sa.Column("glass_options", sa.JSON(), nullable=False),
        sa.Column("pricing_note", sa.String(240), nullable=False),
        sa.Column("default_frame_color_code", sa.String(64), nullable=False),
        sa.Column("default_glass_code", sa.String(64), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_window_solutions")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_window_solutions_listing", "window_solutions", ["available", "room", "sort_order"])
    op.create_table(
        "sample_scenes",
        sa.Column("id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(120), nullable=False),
        sa.Column("scene_type", sa.String(32), nullable=False),
        sa.Column("thumbnail_path", sa.String(256), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("default_selection", sa.JSON(), nullable=False),
        sa.Column("available", sa.Boolean(), nullable=False),
        sa.Column("sort_order", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_sample_scenes")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index("ix_sample_scenes_listing", "sample_scenes", ["available", "sort_order"])
    op.create_table(
        "photos",
        sa.Column("id", sa.String(41), nullable=False),
        sa.Column("user_id", sa.String(41), nullable=False),
        sa.Column("purpose", sa.String(32), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("thumbnail_key", sa.String(512), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("mime_type", sa.String(64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_photos")),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], name=op.f("fk_photos_user_id_users"), ondelete="CASCADE"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(op.f("ix_photos_user_id"), "photos", ["user_id"])


def downgrade() -> None:
    """仅回退本阶段表；对象文件由单独的存储保留策略管理，迁移不删除文件。"""

    op.drop_index(op.f("ix_photos_user_id"), table_name="photos")
    op.drop_table("photos")
    op.drop_index("ix_sample_scenes_listing", table_name="sample_scenes")
    op.drop_table("sample_scenes")
    op.drop_index("ix_window_solutions_listing", table_name="window_solutions")
    op.drop_table("window_solutions")
