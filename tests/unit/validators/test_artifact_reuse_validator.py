"""验证总控 Agent 的复用建议必须经过确定性 TaskSpec 影响分析。"""

from typing import Any, cast
from uuid import uuid4

import pytest

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactReuseDecision, ArtifactReuseRequest
from kg_agent.domain.enums import ArtifactStatus, TaskIntent
from kg_agent.domain.task_models import OntologyStrategy
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.services.context_service import InMemoryContextService
from kg_agent.services.demo_service import RequestScopedArtifactReuseEvaluator
from kg_agent.validators.artifact_reuse_validator import ArtifactReuseValidator
from kg_agent.validators.subtask_validator import SubTaskValidationContext
from tests.factories import make_artifact, make_decomposition, make_subtask


class _PersistedArtifactLookup:
    """模拟请求外已发布 Artifact 的只读 PostgreSQL 查询。"""

    def __init__(self, *artifacts: Any) -> None:
        """按 Artifact ID 保存测试用历史引用。"""

        self._artifacts = {item.artifact_id: item for item in artifacts}

    async def get_ref(self, artifact_id):  # type: ignore[no-untyped-def]
        """返回历史父级引用；不存在时模拟数据库未命中。"""

        return self._artifacts.get(artifact_id)

    async def evaluate(self, request: ArtifactReuseRequest) -> ArtifactReuseDecision:
        """该测试只覆盖临时 Artifact 分支，其他请求不应进入这里。"""

        return ArtifactReuseDecision(
            artifact_id=request.artifact_id,
            reusable=False,
            reason_codes=["ARTIFACT_NOT_FOUND"],
        )


async def _current_with_artifact():
    """创建带历史 SemanticDocument 的任务快照。"""

    task_id = uuid4()
    artifact = make_artifact(task_id)
    service = InMemoryContextService()
    current = await service.publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[artifact],
        expected_version=None,
    )
    return current, artifact


@pytest.mark.asyncio
async def test_goal_expansion_keeps_compatible_artifact() -> None:
    """只扩展最终目标、不改变输入和抽取约束时可以沿用已完成成果。"""

    current, artifact = await _current_with_artifact()
    revised = current.current_task_spec.model_copy(
        update={
            "revision": 2,
            "task_intent": TaskIntent.MODIFY,
            "business_goal": "从实体抽取扩展为完整构图",
            "target_output_types": ["PublishedGraph"],
        }
    )
    result = TaskDecompositionResult(
        task_intent=TaskIntent.MODIFY,
        task_spec=revised,
        subtasks=[],
        reused_artifact_ids=[artifact.artifact_id],
    )
    issues = await ArtifactReuseValidator(ArtifactRegistry.v1()).validate(
        result,
        SubTaskValidationContext(task_id=current.task_id, current_context=current),
        {1: current.current_task_spec},
    )
    assert issues == []


@pytest.mark.asyncio
async def test_scope_change_invalidates_generated_artifact() -> None:
    """抽取范围变化时，旧 revision 的生成结果不能由 LLM 强行复用。"""

    current, original = await _current_with_artifact()
    artifact = original.model_copy(update={"artifact_type": "EntityDataset"})
    current = current.model_copy(update={"artifact_refs": [artifact]})
    revised = current.current_task_spec.model_copy(
        update={
            "revision": 2,
            "task_intent": TaskIntent.MODIFY,
            "extraction_scope": {"entity_types": ["Equipment"]},
        }
    )
    result = TaskDecompositionResult(
        task_intent=TaskIntent.MODIFY,
        task_spec=revised,
        subtasks=[],
        reused_artifact_ids=[artifact.artifact_id],
    )
    issues = await ArtifactReuseValidator(ArtifactRegistry.v1()).validate(
        result,
        SubTaskValidationContext(task_id=current.task_id, current_context=current),
        {1: current.current_task_spec},
    )
    assert "REUSE_TASK_SPEC_REVISION_INCOMPATIBLE" in {item.code for item in issues}


@pytest.mark.asyncio
async def test_published_ontology_transition_keeps_matching_entity_artifact() -> None:
    """自建本体发布后改为复用同一版本，不应误伤已经按该版本抽取的实体。"""

    current, original = await _current_with_artifact()
    ontology_id = uuid4()
    artifact = original.model_copy(
        update={
            "artifact_type": "EntityDataset",
            "ontology_id": ontology_id,
            "ontology_version": "1.0.0",
        }
    )
    current = current.model_copy(update={"artifact_refs": [artifact]})
    revised = current.current_task_spec.model_copy(
        update={
            "revision": 2,
            "task_intent": TaskIntent.MODIFY,
            "business_goal": "复用已有实体继续构建候选图谱",
            "target_output_types": ["CandidateGraph"],
            "ontology_strategy": OntologyStrategy(
                mode="REUSE_PUBLISHED",
                selected_ontology_id=ontology_id,
                selected_ontology_version="1.0.0",
            ),
            "extraction_scope": {
                "requires_alignment_fusion": False,
                "requires_relation_alignment": False,
                "knowledge_extract": True,
            },
        }
    )
    result = TaskDecompositionResult(
        task_intent=TaskIntent.MODIFY,
        task_spec=revised,
        subtasks=[],
        reused_artifact_ids=[artifact.artifact_id],
    )

    issues = await ArtifactReuseValidator(ArtifactRegistry.v1()).validate(
        result,
        SubTaskValidationContext(task_id=current.task_id, current_context=current),
        {1: current.current_task_spec},
    )

    assert issues == []


@pytest.mark.asyncio
async def test_missing_parent_snapshot_rejects_reuse() -> None:
    """父输入不在可验证快照时不能假定血缘仍然有效。"""

    current, artifact = await _current_with_artifact()
    orphan = artifact.model_copy(update={"parent_artifact_ids": [uuid4()]})
    result = TaskDecompositionResult(
        task_intent=TaskIntent.CONTINUE,
        task_spec=current.current_task_spec,
        subtasks=[],
        reused_artifact_ids=[orphan.artifact_id],
    )
    context = current.model_copy(update={"artifact_refs": [orphan]})
    issues = await ArtifactReuseValidator(ArtifactRegistry.v1()).validate(
        result,
        SubTaskValidationContext(task_id=current.task_id, current_context=context),
        {1: current.current_task_spec},
    )
    assert "REUSE_PARENT_LINEAGE_MISMATCH" in {item.code for item in issues}


@pytest.mark.asyncio
async def test_request_scoped_import_resolves_persisted_published_ontology_parent() -> None:
    """新任务导入已发布本体时，应能校验其历史任务中的受控来源。"""

    source_task_id, target_task_id = uuid4(), uuid4()
    ontology_id = uuid4()
    source = make_artifact(source_task_id, artifact_type="PublishedOntology").model_copy(
        update={"ontology_id": ontology_id, "ontology_version": "1.0.0"}
    )
    imported = make_artifact(target_task_id, artifact_type="PublishedOntology").model_copy(
        update={
            "producer_agent": "system",
            "producer_skill": "published_asset_import",
            "parent_artifact_ids": [source.artifact_id],
            "ontology_id": ontology_id,
            "ontology_version": "1.0.0",
            "status": ArtifactStatus.AVAILABLE,
        }
    )
    evaluator = RequestScopedArtifactReuseEvaluator(
        [imported],
        cast(Any, _PersistedArtifactLookup(source)),
    )

    decision = await evaluator.evaluate(
        ArtifactReuseRequest(
            artifact_id=imported.artifact_id,
            expected_artifact_type="PublishedOntology",
            expected_schema_version="1.0.0",
            compatible_task_spec_revisions={1},
            # Controller 的模型可见输入不包含跨任务来源；校验快照必须从权威
            # PostgreSQL 父级补齐 checksum，而不是把空集合误判成内容变化。
            expected_parent_checksums=set(),
            expected_ontology_id=ontology_id,
            expected_ontology_version="1.0.0",
        )
    )

    assert decision.reusable is True
    assert decision.reason_codes == []
