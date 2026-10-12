"""集中导出核心契约的 JSON Schema，供测试和后续 API 使用。"""

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from kg_agent.domain.agent_models import (
    AgentRunResult,
    ProposedSubTask,
    QualitySignal,
    TaskDecompositionResult,
)
from kg_agent.domain.artifact_models import (
    ArtifactCreateCommand,
    ArtifactRecord,
    ArtifactRef,
    ArtifactReuseDecision,
    ArtifactReuseRequest,
)
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.context_view_models import (
    ArtifactContextItem,
    AssignmentContextItem,
    ControllerContextView,
    ProfessionalAgentContextView,
    SubTaskContextItem,
)
from kg_agent.domain.evaluation_models import GoldenEvaluationRecord
from kg_agent.domain.event_models import TaskEvent
from kg_agent.domain.memory_models import ConversationContextRef, MemoryRef
from kg_agent.domain.registry_models import AgentRegistration, SkillRegistration
from kg_agent.domain.review_models import HumanReviewRequest
from kg_agent.domain.skill_models import SkillArtifactOutput, SkillInvocation, SkillRunResult
from kg_agent.domain.task_models import (
    AgentSubTask,
    MasterTask,
    OntologyStrategy,
    QualityRequirements,
    RetryPolicy,
    TaskSpec,
)
from kg_agent.domain.trace_models import ExecutionTrace, TraceEvent

CONTRACT_MODELS: Mapping[str, type[BaseModel]] = {
    model.__name__: model
    for model in (
        OntologyStrategy,
        QualityRequirements,
        TaskSpec,
        MasterTask,
        RetryPolicy,
        AgentSubTask,
        AgentAssignment,
        ArtifactRef,
        ArtifactRecord,
        ArtifactCreateCommand,
        ArtifactReuseRequest,
        ArtifactReuseDecision,
        SkillInvocation,
        SkillArtifactOutput,
        SkillRunResult,
        ConversationContextRef,
        MemoryRef,
        SharedTaskContext,
        ArtifactContextItem,
        SubTaskContextItem,
        AssignmentContextItem,
        ControllerContextView,
        ProfessionalAgentContextView,
        TaskEvent,
        ProposedSubTask,
        QualitySignal,
        AgentRunResult,
        HumanReviewRequest,
        TraceEvent,
        ExecutionTrace,
        TaskDecompositionResult,
        AgentRegistration,
        SkillRegistration,
        GoldenEvaluationRecord,
    )
}


def get_contract_schema(name: str) -> dict[str, Any]:
    """返回指定核心契约的 JSON Schema。

    Args:
        name: Pydantic 模型类名。

    Returns:
        由 Pydantic 生成的 JSON Schema。

    Raises:
        KeyError: 当契约名称不存在时抛出。
    """

    return CONTRACT_MODELS[name].model_json_schema()


def export_all_contract_schemas() -> dict[str, dict[str, Any]]:
    """返回全部核心契约的 JSON Schema 映射。"""

    return {name: model.model_json_schema() for name, model in CONTRACT_MODELS.items()}
