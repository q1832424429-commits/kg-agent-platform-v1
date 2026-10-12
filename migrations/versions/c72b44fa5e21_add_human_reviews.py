"""增加第六阶段人工审核请求表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c72b44fa5e21"
down_revision: str | Sequence[str] | None = "a51f9a8d2c44"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建带不可变目标快照和决策 Artifact 引用的审核表。"""

    op.create_table(
        "human_review_requests",
        sa.Column("review_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("subtask_id", sa.Uuid(), nullable=True),
        sa.Column("target_type", sa.String(length=16), nullable=False),
        sa.Column("target_artifact_id", sa.Uuid(), nullable=False),
        sa.Column("target_artifact_checksum", sa.String(length=80), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("decision_artifact_id", sa.Uuid(), nullable=True),
        sa.Column("reviewer_feedback", sa.Text(), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["decision_artifact_id"],
            ["artifact_refs.artifact_id"],
        ),
        sa.ForeignKeyConstraint(["subtask_id"], ["agent_subtasks.subtask_id"]),
        sa.ForeignKeyConstraint(
            ["target_artifact_id"],
            ["artifact_refs.artifact_id"],
        ),
        sa.ForeignKeyConstraint(["task_id"], ["master_tasks.task_id"]),
        sa.PrimaryKeyConstraint("review_id"),
        sa.UniqueConstraint("decision_artifact_id"),
        sa.UniqueConstraint(
            "task_id",
            "target_artifact_id",
            "target_artifact_checksum",
            name="uq_human_review_target_version",
        ),
    )
    op.create_index(
        "ix_human_review_task_status",
        "human_review_requests",
        ["task_id", "status"],
    )
    op.create_index(
        "ix_human_review_pending_target",
        "human_review_requests",
        [
            "task_id",
            "target_artifact_id",
            "target_artifact_checksum",
            "status",
        ],
    )


def downgrade() -> None:
    """删除第六阶段人工审核请求表。"""

    op.drop_index(
        "ix_human_review_pending_target",
        table_name="human_review_requests",
    )
    op.drop_index(
        "ix_human_review_task_status",
        table_name="human_review_requests",
    )
    op.drop_table("human_review_requests")
