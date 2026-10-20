"""持久化生成队列与结果；升级不写入或覆盖已有用户、照片和方案。"""

import sqlalchemy as sa

from alembic import op

revision = "20261008_0003"
down_revision = "20260907_0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """结构快照显式写在迁移里，后续 ORM 演进不改变历史升级行为。"""
    op.create_table(
        "render_tasks",
        sa.Column("id", sa.String(41), primary_key=True),
        sa.Column("user_id", sa.String(41), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("idempotency_key", sa.String(128), nullable=False),
        sa.Column("client_request_id", sa.String(36), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("source_type", sa.String(24), nullable=False),
        sa.Column("photo_id", sa.String(41), sa.ForeignKey("photos.id"), nullable=True),
        sa.Column("input_photo_id", sa.String(41), sa.ForeignKey("photos.id"), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("model", sa.String(100), nullable=False),
        sa.Column("status", sa.String(16), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("failure", sa.JSON(), nullable=True),
        sa.Column("result_id", sa.String(41), nullable=True),
        sa.Column("retry_of", sa.String(41), nullable=True),
        sa.Column("worker_token", sa.String(36), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "idempotency_key", name="uq_render_tasks_user_key"),
        sa.UniqueConstraint("user_id", "client_request_id", name="uq_render_tasks_user_client"),
    )
    op.create_index("ix_render_tasks_user_id", "render_tasks", ["user_id"])
    op.create_index("ix_render_tasks_photo_id", "render_tasks", ["photo_id"])
    op.create_index("ix_render_tasks_queue", "render_tasks", ["status", "created_at"])
    op.create_table(
        "render_results",
        sa.Column("id", sa.String(41), primary_key=True),
        sa.Column("task_id", sa.String(41), sa.ForeignKey("render_tasks.id"), nullable=False),
        sa.Column("user_id", sa.String(41), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("before_photo_id", sa.String(41), sa.ForeignKey("photos.id"), nullable=False),
        sa.Column("after_photo_id", sa.String(41), sa.ForeignKey("photos.id"), nullable=False),
        sa.Column("width", sa.Integer(), nullable=False),
        sa.Column("height", sa.Integer(), nullable=False),
        sa.Column("snapshot", sa.JSON(), nullable=False),
        sa.Column("result_type", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("task_id"),
    )
    op.create_index("ix_render_results_user_id", "render_results", ["user_id"])


def downgrade() -> None:
    """回退只移除本阶段表；运行环境执行前应先停用生成及保存所需结果。"""
    op.drop_table("render_results")
    op.drop_table("render_tasks")
