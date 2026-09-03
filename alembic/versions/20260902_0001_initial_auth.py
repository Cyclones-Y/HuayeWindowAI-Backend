"""建立用户与微信身份表。

Revision ID: 20260902_0001
Revises:
Create Date: 2026-09-02
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_0001"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.String(length=41), nullable=False),
        sa.Column("phone_number", sa.String(length=24), nullable=False),
        sa.Column("phone_country_code", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_users")),
        sa.UniqueConstraint("phone_number", name=op.f("uq_users_phone_number")),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_table(
        "wechat_identities",
        sa.Column("id", sa.String(length=41), nullable=False),
        sa.Column("user_id", sa.String(length=41), nullable=False),
        sa.Column("app_id", sa.String(length=32), nullable=False),
        sa.Column("open_id", sa.String(length=128), nullable=False),
        sa.Column("union_id", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], name=op.f("fk_wechat_identities_user_id_users"), ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_wechat_identities")),
        sa.UniqueConstraint("app_id", "open_id", name="uq_wechat_identities_app_open"),
        mysql_charset="utf8mb4",
        mysql_collate="utf8mb4_0900_ai_ci",
    )
    op.create_index(op.f("ix_wechat_identities_user_id"), "wechat_identities", ["user_id"], unique=False)
    op.create_index("ix_wechat_identities_union_id", "wechat_identities", ["union_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_wechat_identities_union_id", table_name="wechat_identities")
    op.drop_index(op.f("ix_wechat_identities_user_id"), table_name="wechat_identities")
    op.drop_table("wechat_identities")
    op.drop_table("users")
