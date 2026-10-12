"""增加第五阶段 AgentRunResult 与 ExecutionTrace 表。"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a51f9a8d2c44"
down_revision: str | Sequence[str] | None = "8d87fcdb341e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """创建不可变专业 Agent 运行结果和公开轨迹表。"""

    op.create_table(
        "agent_run_results",
        sa.Column("run_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("subtask_id", sa.Uuid(), nullable=False),
        sa.Column("assignment_id", sa.Uuid(), nullable=False),
        sa.Column("agent_name", sa.String(length=128), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["assignment_id"], ["agent_assignments.assignment_id"]),
        sa.ForeignKeyConstraint(["subtask_id"], ["agent_subtasks.subtask_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["master_tasks.task_id"]),
        sa.PrimaryKeyConstraint("run_id"),
        sa.UniqueConstraint("assignment_id"),
    )
    op.create_index("ix_agent_run_results_task_id", "agent_run_results", ["task_id"])
    op.create_index("ix_agent_run_results_status", "agent_run_results", ["status"])
    op.create_table(
        "execution_traces",
        sa.Column("trace_id", sa.Uuid(), nullable=False),
        sa.Column("task_id", sa.Uuid(), nullable=False),
        sa.Column("subtask_id", sa.Uuid(), nullable=True),
        sa.Column("assignment_id", sa.Uuid(), nullable=True),
        sa.Column("agent_run_id", sa.Uuid(), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["agent_run_id"], ["agent_run_results.run_id"]),
        sa.ForeignKeyConstraint(["task_id"], ["master_tasks.task_id"]),
        sa.PrimaryKeyConstraint("trace_id"),
        sa.UniqueConstraint("agent_run_id"),
    )
    op.create_index("ix_execution_traces_task_id", "execution_traces", ["task_id"])


def downgrade() -> None:
    """按外键顺序删除第五阶段表。"""

    op.drop_index("ix_execution_traces_task_id", table_name="execution_traces")
    op.drop_table("execution_traces")
    op.drop_index("ix_agent_run_results_status", table_name="agent_run_results")
    op.drop_index("ix_agent_run_results_task_id", table_name="agent_run_results")
    op.drop_table("agent_run_results")
