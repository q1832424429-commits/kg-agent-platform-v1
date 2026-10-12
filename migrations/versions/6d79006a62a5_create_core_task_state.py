"""创建第二阶段任务状态、Assignment、事件和上下文快照表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "6d79006a62a5"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建第二阶段全部核心持久化表、索引和约束。"""
    op.create_table(
        "master_tasks",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("workspace_id", sa.String(length=128), nullable=False),
        sa.Column("user_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("current_task_spec_revision", sa.Integer(), nullable=False),
        sa.Column("current_decomposition_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("task_id"),
    )
    op.create_index(
        op.f("ix_master_tasks_conversation_id"), "master_tasks", ["conversation_id"], unique=False
    )
    op.create_index(op.f("ix_master_tasks_status"), "master_tasks", ["status"], unique=False)
    op.create_table(
        "artifact_refs",
        sa.Column("artifact_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("artifact_type", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["master_tasks.task_id"],
        ),
        sa.PrimaryKeyConstraint("artifact_id"),
    )
    op.create_index(
        op.f("ix_artifact_refs_artifact_type"), "artifact_refs", ["artifact_type"], unique=False
    )
    op.create_index(op.f("ix_artifact_refs_task_id"), "artifact_refs", ["task_id"], unique=False)
    op.create_table(
        "decomposition_revisions",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("task_spec_revision", sa.Integer(), nullable=False),
        sa.Column("task_intent", sa.String(length=16), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["master_tasks.task_id"],
        ),
        sa.PrimaryKeyConstraint("task_id", "revision"),
    )
    op.create_table(
        "shared_context_snapshots",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["master_tasks.task_id"],
        ),
        sa.PrimaryKeyConstraint("task_id", "version"),
    )
    op.create_table(
        "task_events",
        sa.Column("sequence", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("subtask_id", sa.Uuid(), nullable=True),
        sa.Column("assignment_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=64), nullable=False),
        sa.Column("actor_type", sa.String(length=16), nullable=False),
        sa.Column("actor_id", sa.String(length=256), nullable=False),
        sa.Column("reason_code", sa.String(length=128), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["master_tasks.task_id"],
        ),
        sa.PrimaryKeyConstraint("sequence"),
        sa.UniqueConstraint("event_id"),
    )
    op.create_index(op.f("ix_task_events_event_type"), "task_events", ["event_type"], unique=False)
    op.create_index(op.f("ix_task_events_task_id"), "task_events", ["task_id"], unique=False)
    op.create_table(
        "task_spec_revisions",
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("task_intent", sa.String(length=16), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["master_tasks.task_id"],
        ),
        sa.PrimaryKeyConstraint("task_id", "revision"),
    )
    op.create_table(
        "agent_subtasks",
        sa.Column("subtask_id", sa.Uuid(), nullable=False),
        sa.Column("master_task_id", sa.Uuid(), nullable=False),
        sa.Column("decomposition_revision", sa.Integer(), nullable=False),
        sa.Column("subtask_type", sa.String(length=128), nullable=False),
        sa.Column("assigned_agent_type", sa.String(length=128), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("human_review_required", sa.Boolean(), nullable=False),
        sa.Column("retry_max_attempts", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["master_task_id", "decomposition_revision"],
            ["decomposition_revisions.task_id", "decomposition_revisions.revision"],
        ),
        sa.PrimaryKeyConstraint("subtask_id"),
    )
    op.create_index(
        op.f("ix_agent_subtasks_assigned_agent_type"),
        "agent_subtasks",
        ["assigned_agent_type"],
        unique=False,
    )
    op.create_index(op.f("ix_agent_subtasks_status"), "agent_subtasks", ["status"], unique=False)
    op.create_index(
        op.f("ix_agent_subtasks_subtask_type"), "agent_subtasks", ["subtask_type"], unique=False
    )
    op.create_index(
        "ix_subtasks_task_status", "agent_subtasks", ["master_task_id", "status"], unique=False
    )
    op.create_table(
        "agent_assignments",
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("subtask_id", sa.Uuid(), nullable=False),
        sa.Column("assigned_agent_type", sa.String(length=128), nullable=False),
        sa.Column("claimed_by", sa.String(length=256), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("fencing_token", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["subtask_id"],
            ["agent_subtasks.subtask_id"],
        ),
        sa.PrimaryKeyConstraint("assignment_id"),
        sa.UniqueConstraint("subtask_id", "attempt", name="uq_assignment_subtask_attempt"),
    )
    op.create_index(
        op.f("ix_agent_assignments_status"), "agent_assignments", ["status"], unique=False
    )
    op.create_index(
        "ix_assignment_claim",
        "agent_assignments",
        ["assigned_agent_type", "status", "created_at"],
        unique=False,
    )
    op.create_table(
        "subtask_dependencies",
        sa.Column("subtask_id", sa.Uuid(), nullable=False),
        sa.Column("dependency_subtask_id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["dependency_subtask_id"],
            ["agent_subtasks.subtask_id"],
        ),
        sa.ForeignKeyConstraint(
            ["subtask_id"],
            ["agent_subtasks.subtask_id"],
        ),
        sa.PrimaryKeyConstraint("subtask_id", "dependency_subtask_id"),
    )


def downgrade() -> None:
    """按外键依赖逆序删除第二阶段核心持久化表。"""
    op.drop_table("subtask_dependencies")
    op.drop_index("ix_assignment_claim", table_name="agent_assignments")
    op.drop_index(op.f("ix_agent_assignments_status"), table_name="agent_assignments")
    op.drop_table("agent_assignments")
    op.drop_index("ix_subtasks_task_status", table_name="agent_subtasks")
    op.drop_index(op.f("ix_agent_subtasks_subtask_type"), table_name="agent_subtasks")
    op.drop_index(op.f("ix_agent_subtasks_status"), table_name="agent_subtasks")
    op.drop_index(op.f("ix_agent_subtasks_assigned_agent_type"), table_name="agent_subtasks")
    op.drop_table("agent_subtasks")
    op.drop_table("task_spec_revisions")
    op.drop_index(op.f("ix_task_events_task_id"), table_name="task_events")
    op.drop_index(op.f("ix_task_events_event_type"), table_name="task_events")
    op.drop_table("task_events")
    op.drop_table("shared_context_snapshots")
    op.drop_table("decomposition_revisions")
    op.drop_index(op.f("ix_artifact_refs_task_id"), table_name="artifact_refs")
    op.drop_index(op.f("ix_artifact_refs_artifact_type"), table_name="artifact_refs")
    op.drop_table("artifact_refs")
    op.drop_index(op.f("ix_master_tasks_status"), table_name="master_tasks")
    op.drop_index(op.f("ix_master_tasks_conversation_id"), table_name="master_tasks")
    op.drop_table("master_tasks")
