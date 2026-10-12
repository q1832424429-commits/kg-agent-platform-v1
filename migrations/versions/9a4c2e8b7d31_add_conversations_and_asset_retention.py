"""新增会话目录，并允许已发布资产脱离已删除会话的任务归属。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "9a4c2e8b7d31"
down_revision: str | Sequence[str] | None = "f2a6c09d7e31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建会话侧栏表，并允许正式发布资产在来源会话删除后继续存在。"""

    op.create_table(
        "conversations",
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("conversation_id"),
    )
    # 历史任务已带 conversation_id；迁移时先填充目录，避免升级后旧会话在侧栏消失。
    op.execute(
        """
        INSERT INTO conversations (conversation_id, title, created_at, updated_at)
        SELECT conversation_id, MIN(title), MIN(created_at), MAX(updated_at)
        FROM master_tasks
        GROUP BY conversation_id
        """
    )
    with op.batch_alter_table("artifact_refs") as batch_op:
        batch_op.alter_column("task_id", existing_type=sa.Uuid(), nullable=True)
    with op.batch_alter_table("conversation_messages") as batch_op:
        batch_op.alter_column("task_id", existing_type=sa.Uuid(), nullable=True)
    with op.batch_alter_table("published_graphs") as batch_op:
        batch_op.alter_column("task_id", existing_type=sa.Uuid(), nullable=True)
        batch_op.alter_column(
            "source_candidate_artifact_id", existing_type=sa.Uuid(), nullable=True
        )
        batch_op.alter_column("approval_artifact_id", existing_type=sa.Uuid(), nullable=True)


def downgrade() -> None:
    """回滚仅适用于尚未删除任何来源会话的开发数据库。"""

    with op.batch_alter_table("published_graphs") as batch_op:
        batch_op.alter_column("approval_artifact_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.alter_column(
            "source_candidate_artifact_id", existing_type=sa.Uuid(), nullable=False
        )
        batch_op.alter_column("task_id", existing_type=sa.Uuid(), nullable=False)
    with op.batch_alter_table("artifact_refs") as batch_op:
        batch_op.alter_column("task_id", existing_type=sa.Uuid(), nullable=False)
    with op.batch_alter_table("conversation_messages") as batch_op:
        batch_op.alter_column("task_id", existing_type=sa.Uuid(), nullable=False)
    op.drop_table("conversations")
