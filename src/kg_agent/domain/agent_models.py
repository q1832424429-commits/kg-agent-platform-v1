"""定义专业 Agent 运行结果和总控 Agent 结构化输出契约。"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from kg_agent.domain.enums import AgentRunStatus, TaskIntent
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.domain.task_models import AgentSubTask, TaskSpec


class ProposedSubTask(BaseModel):
    """表示专业 Agent 提出的后续工作建议，而不是正式子任务。"""

    name: str
    goal: str
    reason: str
    proposed_agent_type: str | None = None
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    expected_output_types: list[str] = Field(default_factory=list)


class QualitySignal(BaseModel):
    """表示 Agent 对本次结果给出的结构化质量信号。"""

    name: str
    value: float | str | bool
    evidence_refs: list[str] = Field(default_factory=list)


class SkillArtifactBinding(BaseModel):
    """记录一次 Skill 调用使用和生成的真实 Artifact 编号。"""

    invocation_id: UUID
    skill_name: str
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_types: list[str] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    """保存专业 Agent 完成一次 Assignment 后的标准结果。"""

    task_id: UUID
    subtask_id: UUID
    assignment_id: UUID
    agent_name: str
    run_id: UUID
    status: AgentRunStatus
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    skill_artifact_bindings: list[SkillArtifactBinding] = Field(default_factory=list)
    skills_used: list[str] = Field(default_factory=list)
    quality_signals: list[QualitySignal] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    recommended_next_agent: str | None = None
    proposed_subtasks: list[ProposedSubTask] = Field(default_factory=list)
    requires_user_input: bool = False
    user_question: str | None = None
    retryable: bool = False
    metrics: dict[str, int | float | str] = Field(default_factory=dict)
    execution_trace_id: UUID
    memory_entries: list[MemoryRef] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime

    @model_validator(mode="after")
    def validate_result_consistency(self) -> "AgentRunResult":
        """校验文档已经明确的运行状态与关联字段约束。"""

        needs_input = self.status is AgentRunStatus.NEEDS_USER_INPUT or self.requires_user_input
        if needs_input and not self.user_question:
            raise ValueError("需要用户输入时必须提供 user_question")
        if self.status is AgentRunStatus.SUCCESS and not self.output_artifact_ids:
            raise ValueError("SUCCESS 必须至少包含一个输出 Artifact")
        if self.skill_artifact_bindings:
            bound_ids = [
                artifact_id
                for binding in self.skill_artifact_bindings
                for artifact_id in binding.output_artifact_ids
            ]
            if len(bound_ids) != len(set(bound_ids)):
                raise ValueError("同一输出 Artifact 不能绑定到多个 Skill 调用")
            if set(bound_ids) != set(self.output_artifact_ids):
                raise ValueError("Skill Artifact 映射必须覆盖全部输出 Artifact")
            if any(
                len(binding.output_artifact_ids) != len(binding.output_artifact_types)
                for binding in self.skill_artifact_bindings
            ):
                raise ValueError("Skill 输出 Artifact 编号与类型必须一一对应")
        return self


class TaskDecompositionResult(BaseModel):
    """表示总控 Agent 完成意图、TaskSpec 和子任务拆解后的候选输出。"""

    task_intent: TaskIntent
    task_spec: TaskSpec
    subtasks: list[AgentSubTask]
    reused_artifact_ids: list[UUID] = Field(default_factory=list)
    requires_clarification: bool = False
    clarification_question: str | None = None

    @model_validator(mode="after")
    def validate_clarification(self) -> "TaskDecompositionResult":
        """确保请求澄清时存在可展示给用户的问题。"""

        if self.requires_clarification and not self.clarification_question:
            raise ValueError("requires_clarification=True 时必须提供 clarification_question")
        return self
