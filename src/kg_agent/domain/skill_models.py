"""定义业务 Skill 的调用与输出契约。

本模块不运行专业 Agent；它只约束专业 Agent 在后续阶段如何调用已注册 Skill。
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.enums import AgentRunStatus


class SkillInvocation(BaseModel):
    """专业 Agent 发起的一次 Skill 调用。"""

    invocation_id: UUID
    agent_type: str
    skill_name: str
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class SkillArtifactOutput(BaseModel):
    """Skill 准备交给 ArtifactService 持久化的单个输出。"""

    artifact_type: str
    schema_version: str = "1.0.0"
    content: dict[str, Any]
    content_summary: str
    media_type: str = "application/json"
    parent_artifact_ids: list[UUID] = Field(default_factory=list)
    ontology_id: UUID | None = None
    ontology_version: str | None = None
    # 仅 PublishedOntology / PublishedGraph 从已批准的审核结果携带该业务名称。
    publication_title: str | None = Field(default=None, max_length=120)


class SkillRunResult(BaseModel):
    """Skill 的确定性执行结果；失败时不产生正式 ArtifactRef。"""

    invocation_id: UUID
    skill_name: str
    status: AgentRunStatus
    outputs: list[SkillArtifactOutput] = Field(default_factory=list)
    quality_signals: dict[str, float] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool = False
