"""定义总控模型使用的轻量语义计划，不承载数据库和运行时事实。

总控大模型只判断用户意图、业务目标、Agent 级任务和依赖。UUID、revision、状态、
时间、重试策略及 Registry 原子输出由确定性计划编译器补齐，最终仍发布既有
``TaskDecompositionResult`` 公共契约。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from kg_agent.domain.enums import TaskIntent
from kg_agent.domain.task_models import OntologyStrategy, QualityRequirements


class TaskSpecDraft(BaseModel):
    """保存需要由总控理解的 TaskSpec 语义字段。"""

    business_goal: str
    domain: str = "manufacturing"
    source_artifact_ids: list[UUID] = Field(default_factory=list)
    target_output_types: list[str] = Field(default_factory=list)
    ontology_strategy: OntologyStrategy
    extraction_scope: dict[str, Any] = Field(default_factory=dict)
    quality_requirements: QualityRequirements = Field(default_factory=QualityRequirements)
    reuse_policy: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)


class AgentSubTaskDraft(BaseModel):
    """描述一个 Agent 级语义任务及其计划内别名依赖。"""

    node_id: str = Field(min_length=1)
    name: str
    description: str = ""
    goal: str
    subtask_type: str
    assigned_agent_type: str
    dependency_node_ids: list[str] = Field(default_factory=list)
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    expected_output_types: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    human_review_required: bool = False


class TaskPlanDraft(BaseModel):
    """表示总控模型的一次轻量语义规划结果。"""

    task_intent: TaskIntent
    task_spec: TaskSpecDraft
    subtasks: list[AgentSubTaskDraft]
    reused_artifact_ids: list[UUID] = Field(default_factory=list)
    requires_clarification: bool = False
    clarification_question: str | None = None

    @model_validator(mode="before")
    @classmethod
    def accept_legacy_full_contract(cls, value: Any) -> Any:
        """兼容测试和滚动升级期间仍返回完整公共契约的模型响应。

        正式 Prompt 只展示轻量契约；该兼容入口不会把模型提供的 UUID、revision、
        状态或时间带入运行时，只把它们转换为单次计划内的临时节点别名。
        """

        if not isinstance(value, dict):
            return value
        task_spec = value.get("task_spec")
        subtasks = value.get("subtasks")
        if not isinstance(task_spec, dict) or not isinstance(subtasks, list):
            return value
        converted = dict(value)
        converted["task_spec"] = {
            key: item
            for key, item in task_spec.items()
            if key
            in {
                "business_goal",
                "domain",
                "source_artifact_ids",
                "target_output_types",
                "ontology_strategy",
                "extraction_scope",
                "quality_requirements",
                "reuse_policy",
                "constraints",
            }
        }
        converted_subtasks: list[Any] = []
        for index, item in enumerate(subtasks):
            if not isinstance(item, dict) or "node_id" in item:
                converted_subtasks.append(item)
                continue
            converted_subtasks.append(
                {
                    "node_id": str(item.get("subtask_id", f"node-{index + 1}")),
                    "name": item.get("name", item.get("subtask_type", f"任务 {index + 1}")),
                    "description": item.get("description", ""),
                    "goal": item.get("goal", "完成声明的业务输出"),
                    "subtask_type": item.get("subtask_type", ""),
                    "assigned_agent_type": item.get("assigned_agent_type", ""),
                    "dependency_node_ids": [
                        str(dependency) for dependency in item.get("dependency_subtask_ids", [])
                    ],
                    "input_artifact_ids": item.get("input_artifact_ids", []),
                    "expected_output_types": item.get("expected_output_types", []),
                    "completion_criteria": item.get("completion_criteria", []),
                    "human_review_required": item.get("human_review_required", False),
                }
            )
        converted["subtasks"] = converted_subtasks
        return converted

    @model_validator(mode="after")
    def validate_draft(self) -> TaskPlanDraft:
        """确保澄清问题和计划内节点别名满足轻量契约。"""

        if self.requires_clarification and not self.clarification_question:
            raise ValueError("requires_clarification=True 时必须提供 clarification_question")
        node_ids = [item.node_id for item in self.subtasks]
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("TaskPlanDraft.node_id 必须在本次计划内唯一")
        return self
