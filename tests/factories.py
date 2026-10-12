"""构造符合公共契约的可重复测试对象。"""

from datetime import UTC, datetime
from uuid import UUID, uuid4

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.enums import ArtifactStatus, SubTaskStatus, TaskIntent
from kg_agent.domain.task_models import AgentSubTask, OntologyStrategy, TaskSpec

NOW = datetime(2026, 9, 3, 8, 0, tzinfo=UTC)


def make_task_spec(
    task_id: UUID,
    intent: TaskIntent = TaskIntent.START,
    revision: int = 1,
    goal: str = "根据制造业文档构建知识图谱",
) -> TaskSpec:
    """构造一个制造业测试 TaskSpec。"""

    return TaskSpec(
        task_id=task_id,
        revision=revision,
        task_intent=intent,
        business_goal=goal,
        ontology_strategy=OntologyStrategy(mode="BUILD_FROM_DOCUMENTS"),
        created_at=NOW,
    )


def make_subtask(
    task_id: UUID,
    *,
    subtask_type: str = "document_parse",
    agent_type: str = "document_parsing_agent",
    outputs: list[str] | None = None,
    dependencies: list[UUID] | None = None,
    inputs: list[UUID] | None = None,
    human_review_required: bool = False,
    decomposition_revision: int = 1,
    subtask_id: UUID | None = None,
) -> AgentSubTask:
    """构造一个总控 Agent 待发布的测试子任务。"""

    return AgentSubTask(
        subtask_id=subtask_id or uuid4(),
        master_task_id=task_id,
        decomposition_revision=decomposition_revision,
        name=f"执行 {subtask_type}",
        description=f"由 {agent_type} 处理测试任务",
        goal=f"产生 {outputs or ['SemanticDocument']}",
        subtask_type=subtask_type,
        assigned_agent_type=agent_type,
        dependency_subtask_ids=dependencies or [],
        input_artifact_ids=inputs or [],
        expected_output_types=outputs or ["SemanticDocument"],
        completion_criteria=["产生声明的输出 Artifact"],
        status=SubTaskStatus.READY if not dependencies else SubTaskStatus.BLOCKED,
        human_review_required=human_review_required,
        created_at=NOW,
    )


def make_decomposition(
    task_id: UUID,
    *,
    intent: TaskIntent = TaskIntent.START,
    revision: int = 1,
    subtasks: list[AgentSubTask] | None = None,
    clarification: bool = False,
) -> TaskDecompositionResult:
    """构造一个总控 Agent 结构化响应。"""

    return TaskDecompositionResult(
        task_intent=intent,
        task_spec=make_task_spec(task_id, intent, revision),
        subtasks=subtasks or [],
        requires_clarification=clarification,
        clarification_question="请明确要继续哪个任务。" if clarification else None,
    )


def make_artifact(
    task_id: UUID,
    artifact_type: str = "SemanticDocument",
    artifact_id: UUID | None = None,
) -> ArtifactRef:
    """构造一个只包含元数据和内容引用的测试 ArtifactRef。"""

    return ArtifactRef(
        artifact_id=artifact_id or uuid4(),
        artifact_type=artifact_type,
        schema_version="1.0.0",
        task_id=task_id,
        task_spec_revision=1,
        producer_agent="document_parsing_agent",
        producer_skill="parse_document",
        producer_run_id=uuid4(),
        content_uri="minio://test-private/path",
        content_summary="不包含完整文档的测试摘要",
        checksum="sha256:test",
        status=ArtifactStatus.AVAILABLE,
        created_at=NOW,
    )
