"""定义按 Agent 角色裁剪的 SharedContext 只读视图。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field

from kg_agent.domain.enums import AssignmentStatus, SubTaskStatus
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.domain.task_models import QualityRequirements, TaskSpec


class ArtifactContextItem(BaseModel):
    """向 Agent 暴露 Artifact 摘要，不包含内部内容地址。"""

    artifact_id: UUID
    artifact_type: str
    schema_version: str
    content_summary: str
    checksum: str
    ontology_id: UUID | None = None
    ontology_version: str | None = None
    parent_artifact_ids: list[UUID] = Field(default_factory=list)


class SubTaskContextItem(BaseModel):
    """向总控 Agent 暴露子任务的最小状态摘要。"""

    subtask_id: UUID
    name: str
    subtask_type: str
    assigned_agent_type: str
    status: SubTaskStatus
    dependency_subtask_ids: list[UUID] = Field(default_factory=list)
    expected_output_types: list[str] = Field(default_factory=list)


class AssignmentContextItem(BaseModel):
    """向 Agent 暴露 Assignment 的最小租约状态摘要。"""

    assignment_id: UUID
    subtask_id: UUID
    assigned_agent_type: str
    status: AssignmentStatus
    attempt: int
    lease_expires_at: datetime | None = None


class ControllerContextView(BaseModel):
    """提供给总控 Agent 的任务级完整摘要视图。"""

    task_id: UUID
    context_version: int
    current_task_spec: TaskSpec
    task_spec_revisions: list[int] = Field(default_factory=list)
    subtasks: list[SubTaskContextItem] = Field(default_factory=list)
    assignments: list[AssignmentContextItem] = Field(default_factory=list)
    artifacts: list[ArtifactContextItem] = Field(default_factory=list)
    human_review_request_ids: list[UUID] = Field(default_factory=list)
    short_term_memories: list[MemoryRef] = Field(default_factory=list)
    long_term_memory_refs: list[MemoryRef] = Field(default_factory=list)


class ProfessionalAgentContextView(BaseModel):
    """提供给单个专业 Agent 的 Assignment 级最小工作视图。"""

    task_id: UUID
    context_version: int
    assignment: AssignmentContextItem
    subtask_id: UUID
    subtask_type: str
    goal: str
    completion_criteria: list[str] = Field(default_factory=list)
    quality_requirements: QualityRequirements = Field(default_factory=QualityRequirements)
    input_artifacts: list[ArtifactContextItem] = Field(default_factory=list)
    business_goal: str
    domain: str
    extraction_scope: dict[str, object] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    short_term_memories: list[MemoryRef] = Field(default_factory=list)
    long_term_memory_refs: list[MemoryRef] = Field(default_factory=list)
