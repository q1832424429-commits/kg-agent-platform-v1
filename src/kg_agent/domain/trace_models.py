"""定义可审计但不包含隐藏思维过程的执行轨迹契约。"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class TraceEvent(BaseModel):
    """表示执行轨迹中的一个结构化事件。"""

    event_id: UUID
    event_type: str
    occurred_at: datetime
    actor_type: Literal["USER", "AGENT", "SKILL", "SERVICE", "MODEL"]
    actor_name: str
    summary: str
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    metrics: dict[str, int | float | str] = Field(default_factory=dict)
    error_code: str | None = None


class ExecutionTrace(BaseModel):
    """关联一次端到端任务或 Agent Run 的可审计轨迹。"""

    trace_id: UUID
    task_id: UUID
    subtask_id: UUID | None = None
    assignment_id: UUID | None = None
    agent_run_id: UUID | None = None
    parent_trace_id: UUID | None = None
    prompt_version: str | None = None
    model_name: str | None = None
    events: list[TraceEvent] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime | None = None
    status: str
