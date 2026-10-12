"""验证总控 Agent 发布前的 Artifact 交接边界。"""

from uuid import uuid4

import pytest

from kg_agent.domain.enums import ArtifactStatus, TaskIntent
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.services.context_service import InMemoryContextService
from kg_agent.validators.handoff_validator import HandoffValidator
from kg_agent.validators.subtask_validator import SubTaskValidationContext
from tests.factories import make_artifact, make_decomposition, make_subtask


@pytest.mark.asyncio
async def test_historical_input_must_declare_reuse() -> None:
    """历史输入不能绕过 reused_artifact_ids 和后续复用门禁。"""

    task_id = uuid4()
    artifact = make_artifact(task_id)
    contexts = InMemoryContextService()
    current = await contexts.publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[artifact],
        expected_version=None,
    )
    result = make_decomposition(
        task_id,
        intent=TaskIntent.MODIFY,
        revision=2,
        subtasks=[
            make_subtask(
                task_id,
                subtask_type="ontology_extract",
                agent_type="knowledge_extraction_agent",
                outputs=["CandidateOntology"],
                inputs=[artifact.artifact_id],
                decomposition_revision=2,
            )
        ],
    )

    issues = HandoffValidator(ArtifactRegistry.v1()).validate(
        result,
        SubTaskValidationContext(
            task_id=task_id,
            current_context=current,
            available_artifacts=[artifact],
        ),
    )

    assert "UNDECLARED_ARTIFACT_REUSE" in {item.code for item in issues}


def test_handoff_rejects_cross_task_and_invalidated_artifact() -> None:
    """专业 Agent 不能收到其他任务或已经失效的 Artifact。"""

    task_id = uuid4()
    artifact = make_artifact(uuid4()).model_copy(update={"status": ArtifactStatus.INVALIDATED})
    result = make_decomposition(
        task_id,
        subtasks=[make_subtask(task_id, inputs=[artifact.artifact_id])],
    )
    issues = HandoffValidator(ArtifactRegistry.v1()).validate(
        result,
        SubTaskValidationContext(task_id=task_id, available_artifacts=[artifact]),
    )
    codes = {item.code for item in issues}
    assert {"CROSS_TASK_ARTIFACT", "ARTIFACT_NOT_AVAILABLE"} <= codes


def test_handoff_rejects_unregistered_output_type() -> None:
    """模型不能创建 Artifact Registry 之外的输出契约。"""

    task_id = uuid4()
    result = make_decomposition(
        task_id,
        subtasks=[make_subtask(task_id, outputs=["ImaginaryArtifact"])],
    )
    issues = HandoffValidator(ArtifactRegistry.v1()).validate(
        result,
        SubTaskValidationContext(task_id=task_id),
    )
    assert "UNREGISTERED_OUTPUT_TYPE" in {item.code for item in issues}
