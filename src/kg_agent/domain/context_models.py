"""定义单个 MasterTask 对应的 SharedTaskContext 快照契约。

SharedTaskContext 是逻辑共享事实来源，但 Agent 实际调用时只能获得裁剪后的视图。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.memory_models import ConversationContextRef, MemoryRef
from kg_agent.domain.task_models import MasterTask, TaskSpec


class SharedTaskContext(BaseModel):
    """保存一个 MasterTask 当前可审计的共享上下文快照。"""

    task_id: UUID
    version: int = Field(ge=1)
    conversation: ConversationContextRef
    master_task: MasterTask
    current_task_spec: TaskSpec
    task_spec_revision_refs: list[int] = Field(default_factory=list)
    subtask_ids: list[UUID] = Field(default_factory=list)
    assignment_ids: list[UUID] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    agent_run_result_ids: list[UUID] = Field(default_factory=list)
    human_review_request_ids: list[UUID] = Field(default_factory=list)
    short_term_memories: list[MemoryRef] = Field(default_factory=list)
    long_term_memory_refs: list[MemoryRef] = Field(default_factory=list)
    execution_trace_ids: list[UUID] = Field(default_factory=list)
    updated_at: datetime
