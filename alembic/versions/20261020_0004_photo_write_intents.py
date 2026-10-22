"""登记跨存储写入，以便崩溃和补偿失败后恢复清理；不修改既有照片或任务。"""

import sqlalchemy as sa

from alembic import op

revision = "20261020_0004"
down_revision = "20261008_0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """独立登记不依赖业务行，业务事务失败后仍可保留对象定位信息。"""
    op.create_table(
        "photo_write_intents",
        sa.Column("photo_id", sa.String(41), primary_key=True),
        sa.Column("storage_key", sa.String(512), nullable=False),
        sa.Column("thumbnail_key", sa.String(512), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_photo_write_intents_created_at", "photo_write_intents", ["created_at"])
    op.create_index("ix_photo_write_intents_next_attempt_at", "photo_write_intents", ["next_attempt_at"])


def downgrade() -> None:
    """回退前应停写并处理未完成登记，回退结构本身不删除图片对象。"""
    op.drop_table("photo_write_intents")
