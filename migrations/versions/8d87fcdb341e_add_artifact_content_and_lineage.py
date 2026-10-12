"""增加第三阶段 Artifact 对象定位与规范化血缘表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "8d87fcdb341e"
down_revision: str | Sequence[str] | None = "6d79006a62a5"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建对象存储定位表、血缘边及查询索引。"""

    op.create_table(
        "artifact_contents",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("object_key", sa.String(length=1024), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["artifact_id"], ["artifact_refs.artifact_id"]),
        sa.PrimaryKeyConstraint("artifact_id"),
        sa.UniqueConstraint("object_key"),
    )
    op.create_table(
        "artifact_lineage",
        sa.Column("child_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("parent_artifact_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(["child_artifact_id"], ["artifact_refs.artifact_id"]),
        sa.ForeignKeyConstraint(["parent_artifact_id"], ["artifact_refs.artifact_id"]),
        sa.PrimaryKeyConstraint("child_artifact_id", "parent_artifact_id"),
    )
    op.create_index(
        "ix_artifact_lineage_parent",
        "artifact_lineage",
        ["parent_artifact_id"],
        unique=False,
    )


def downgrade() -> None:
    """按依赖顺序移除第三阶段 Artifact 表。"""

    op.drop_index("ix_artifact_lineage_parent", table_name="artifact_lineage")
    op.drop_table("artifact_lineage")
    op.drop_table("artifact_contents")
