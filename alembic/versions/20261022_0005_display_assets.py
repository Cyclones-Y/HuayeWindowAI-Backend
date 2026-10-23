"""登记首页公开素材路径；不改方案、账户照片或现有业务记录。"""

import sqlalchemy as sa

from alembic import op

revision = "20261022_0005"
down_revision = "20261020_0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    """首次部署同步登记两张固定展示图；文件需先通过校验工具上传到 OSS。"""
    assets = op.create_table(
        "display_assets",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("image_path", sa.String(256), nullable=False),
        sa.Column("available", sa.Boolean, nullable=False),
    )
    op.bulk_insert(
        assets,
        [
            {"id": "home-before", "image_path": "home-hero-before.jpg", "available": True},
            {"id": "home-after", "image_path": "home-hero-mobile.jpg", "available": True},
        ],
    )


def downgrade() -> None:
    """回退仅删除展示配置表，不删除云端图片或账户数据。"""
    op.drop_table("display_assets")
