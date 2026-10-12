"""定义专业 Agent 内部的 Skill 选择请求与结构化决策。

这些类型服务于 Agent 运行时，不改变 docs/contracts.md 中已经冻结的公共业务契约。
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from kg_agent.domain.agent_models import AgentRunResult, ProposedSubTask
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_view_models import ProfessionalAgentContextView
from kg_agent.domain.enums import AssignmentStatus
from kg_agent.domain.task_models import AgentSubTask


class SkillCallPlan(BaseModel):
    """表示专业 Agent 选择的一次顺序 Skill 调用。"""

    skill_name: str
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class ProfessionalAgentDecision(BaseModel):
    """表示 LLM 对当前 Assignment 的可校验执行决策。"""

    decision_summary: str
    skill_calls: list[SkillCallPlan] = Field(default_factory=list, max_length=8)
    proposed_subtasks: list[ProposedSubTask] = Field(default_factory=list)
    requires_user_input: bool = False
    user_question: str | None = None

    @model_validator(mode="after")
    def validate_user_input_branch(self) -> "ProfessionalAgentDecision":
        """等待用户输入时必须有问题，且不能同时执行业务 Skill。"""

        if self.requires_user_input:
            if not self.user_question:
                raise ValueError("等待用户输入时必须提供 user_question")
            if self.skill_calls:
                raise ValueError("等待用户输入时不能同时执行 Skill")
        elif not self.skill_calls:
            raise ValueError("非等待用户分支必须至少选择一个 Skill")
        return self


class ProfessionalAgentRequest(BaseModel):
    """保存专业 Agent 运行所需的角色视图、正式输入和 Assignment 凭证。"""

    assignment: AgentAssignment
    subtask: AgentSubTask
    task_spec_revision: int = Field(ge=1)
    context_view: ProfessionalAgentContextView
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    worker_id: str

    @model_validator(mode="after")
    def validate_assignment_view(self) -> "ProfessionalAgentRequest":
        """防止 Worker 把其他 Assignment 或其他角色的视图交给当前运行。"""

        if self.assignment.assignment_id != self.context_view.assignment.assignment_id:
            raise ValueError("Assignment 与 Context View 不一致")
        if self.assignment.subtask_id != self.context_view.subtask_id:
            raise ValueError("Assignment 与 Context View 子任务不一致")
        if self.assignment.subtask_id != self.subtask.subtask_id:
            raise ValueError("Assignment 与 AgentSubTask 不一致")
        if self.subtask.master_task_id != self.context_view.task_id:
            raise ValueError("AgentSubTask 与 Context View 任务不一致")
        roles = {
            self.assignment.assigned_agent_type,
            self.subtask.assigned_agent_type,
            self.context_view.assignment.assigned_agent_type,
        }
        if len(roles) != 1:
            raise ValueError("Assignment、AgentSubTask 与 Context View 角色不一致")
        if self.subtask.subtask_type != self.context_view.subtask_type:
            raise ValueError("AgentSubTask 与 Context View 子任务类型不一致")
        if self.assignment.status is not AssignmentStatus.RUNNING:
            raise ValueError("专业 Agent 只能执行 RUNNING Assignment")
        if self.assignment.claimed_by != self.worker_id:
            raise ValueError("ProfessionalAgentRequest.worker_id 不是 Assignment 持有者")
        input_ids = {item.artifact_id for item in self.input_artifacts}
        context_input_ids = {item.artifact_id for item in self.context_view.input_artifacts}
        if input_ids != set(self.subtask.input_artifact_ids):
            raise ValueError("正式输入 Artifact 与 AgentSubTask 分配不一致")
        if input_ids != context_input_ids:
            raise ValueError("正式输入 Artifact 与 Context View 不一致")
        return self


class ProfessionalAgentRunOutput(BaseModel):
    """返回专业 Agent 生成的标准结果；持久化由 Worker 提交服务负责。"""

    result: AgentRunResult
