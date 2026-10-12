"""定义 SharedContext 之外的追加式任务审计事件契约。"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from kg_agent.domain.enums import TaskEventType


class TaskEvent(BaseModel):
    """记录一次已经提交的确定性任务状态变化。"""

    event_id: UUID
    sequence: int = Field(ge=1)
    task_id: UUID
    subtask_id: UUID | None = None
    assignment_id: UUID | None = None
    event_type: TaskEventType
    actor_type: Literal["USER", "AGENT", "SERVICE", "WORKER"]
    actor_id: str
    reason_code: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
