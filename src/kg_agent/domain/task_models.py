"""定义 MasterTask、TaskSpec 与 AgentSubTask 核心契约。

本模块只表达结构和已确认的字段级约束，不实现任务状态迁移、持久化或 Agent 调度。
"""

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from kg_agent.domain.enums import (
    AssignmentMode,
    MasterTaskStatus,
    SubTaskStatus,
    TaskIntent,
)


class OntologyStrategy(BaseModel):
    """描述本任务如何获得可用于实例抽取的本体。"""

    mode: Literal[
        "REUSE_PUBLISHED",
        "IMPORT_EXTERNAL_JSON",
        "BUILD_FROM_DOCUMENTS",
    ]
    selected_ontology_id: UUID | None = None
    selected_ontology_version: str | None = None
    external_ontology_artifact_id: UUID | None = None


class QualityRequirements(BaseModel):
    """保存用户明确提出或平台默认采用的质量要求。"""

    requirements: list[str] = Field(default_factory=list)
    thresholds: dict[str, float] = Field(default_factory=dict)


class TaskSpec(BaseModel):
    """总控 Agent 对用户当前目标的结构化、版本化描述。"""

    task_id: UUID
    revision: int = Field(ge=1)
    task_intent: TaskIntent
    business_goal: str
    domain: str = "manufacturing"
    source_artifact_ids: list[UUID] = Field(default_factory=list)
    target_output_types: list[str] = Field(default_factory=list)
    ontology_strategy: OntologyStrategy
    extraction_scope: dict[str, Any] = Field(default_factory=dict)
    quality_requirements: QualityRequirements = Field(default_factory=QualityRequirements)
    reuse_policy: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    created_by: Literal["controller_agent"] = "controller_agent"
    created_at: datetime


class MasterTask(BaseModel):
    """表示用户总目标及其任务生命周期根对象。"""

    task_id: UUID
    conversation_id: UUID
    workspace_id: str = "default-workspace"
    user_id: str = "default-user"
    title: str
    current_task_spec_revision: int
    current_decomposition_revision: int
    status: MasterTaskStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None


class RetryPolicy(BaseModel):
    """定义子任务发生可重试错误时的重试限制。"""

    max_attempts: int = Field(default=3, ge=1)
    backoff_seconds: int = Field(default=1, ge=0)


class AgentSubTask(BaseModel):
    """由总控 Agent 发布并交给一个专业 Agent 负责的逻辑工作单元。"""

    subtask_id: UUID
    master_task_id: UUID
    decomposition_revision: int = Field(ge=1)
    name: str
    description: str
    goal: str
    subtask_type: str
    assigned_agent_type: str
    assignment_mode: AssignmentMode = AssignmentMode.DIRECT
    dependency_subtask_ids: list[UUID] = Field(default_factory=list)
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    expected_output_types: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    quality_requirements: QualityRequirements = Field(default_factory=QualityRequirements)
    human_review_required: bool = False
    priority: int = 0
    status: SubTaskStatus
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    created_by: Literal["controller_agent"] = "controller_agent"
    created_at: datetime
