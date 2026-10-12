"""定义总控 Agent 开发级案例和后续 Golden Dataset 共用的记录契约。"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel

from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.enums import TaskIntent


class GoldenEvaluationRecord(BaseModel):
    """保存一个可重复评测意图、拆解、分配和复用行为的标注案例。"""

    case_id: str
    conversation_history: list[dict[str, Any]]
    shared_context: dict[str, Any]
    current_task: dict[str, Any] | None
    existing_artifacts: list[ArtifactRef]
    user_message: str
    expected_task_intent: TaskIntent
    expected_task_spec: dict[str, Any]
    expected_subtasks: list[dict[str, Any]]
    expected_assignments: list[dict[str, Any]]
    expected_reused_artifacts: list[UUID]
    expected_clarification: bool
    forbidden_actions: list[str]
