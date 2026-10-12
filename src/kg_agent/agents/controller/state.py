"""定义总控 Agent LangGraph 使用的内部请求、状态和运行结果。

这些模型是运行时内部类型，不修改 docs/contracts.md 中的公共业务契约。
"""

from typing import Any, Literal, TypedDict
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from kg_agent.agents.controller.draft_models import TaskPlanDraft
from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.domain.task_models import AgentSubTask, TaskSpec
from kg_agent.validators.subtask_validator import ValidationIssue


class ControllerRequest(BaseModel):
    """保存一次总控 Agent 调用所需的最小用户输入和引用。

    ``operation`` 区分真实用户轮次和协调器发起的最终验收。最终验收不是用户修改
    任务，必须沿用当前 TaskSpec；显式建模后可避免模型把系统检查误判为 START。
    """

    task_id: UUID
    conversation_id: UUID
    user_message: str = Field(min_length=1)
    conversation_history: list[dict[str, Any]] = Field(default_factory=list)
    current_task_id: UUID | None = None
    existing_artifacts: list[ArtifactRef] = Field(default_factory=list)
    recalled_long_term_memories: list[MemoryRef] = Field(default_factory=list)
    operation: Literal[
        "USER_TURN",
        "SYSTEM_CONTINUATION",
        "FINAL_VERIFICATION",
    ] = "USER_TURN"
    publish_decomposition: bool = True

    @model_validator(mode="after")
    def validate_current_task_id(self) -> "ControllerRequest":
        """确保继续或修改时读取的当前任务与目标任务一致。"""

        if self.current_task_id is not None and self.current_task_id != self.task_id:
            raise ValueError("current_task_id 必须与 task_id 一致")
        return self


class ControllerState(TypedDict, total=False):
    """LangGraph 节点之间传递的总控 Agent 内部状态。"""

    request: ControllerRequest
    current_context: SharedTaskContext | None
    known_subtasks: list[AgentSubTask]
    task_spec_history: dict[int, TaskSpec]
    draft: TaskPlanDraft
    candidate: TaskDecompositionResult
    validation_issues: list[ValidationIssue]
    repair_attempts: int
    excluded_reuse_artifact_ids: set[UUID]
    published_context: SharedTaskContext | None


class ControllerRunResult(BaseModel):
    """返回总控 Agent 最终候选结果和可选的新 SharedContext 快照。"""

    decomposition: TaskDecompositionResult
    shared_context: SharedTaskContext | None
    validation_issues: list[ValidationIssue] = Field(default_factory=list)
