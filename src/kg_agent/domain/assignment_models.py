"""定义 AgentSubTask 一次具体执行分配的契约。

Assignment 状态已经在第二阶段冻结；认领状态机由确定性 AssignmentService 实现。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from kg_agent.domain.enums import AssignmentStatus


class AgentAssignment(BaseModel):
    """保存一个 AgentSubTask 的具体执行尝试和租约信息。"""

    assignment_id: UUID
    subtask_id: UUID
    assigned_agent_type: str
    claimed_by: str | None = None
    status: AssignmentStatus
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    fencing_token: int = Field(default=0, ge=0)
    attempt: int = Field(default=1, ge=1)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
