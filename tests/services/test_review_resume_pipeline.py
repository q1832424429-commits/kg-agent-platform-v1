"""验证真实 DOCX 与可编排模型响应经过两次人工审核后发布最终图谱。"""

from io import BytesIO
from pathlib import Path
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from kg_agent.agents.professional.models import ProfessionalAgentDecision, SkillCallPlan
from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.enums import HumanReviewStatus, SubTaskStatus, TaskIntent
from kg_agent.domain.task_models import AgentSubTask, OntologyStrategy, TaskSpec
from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.services.demo_service import DemoOrchestrationService, DemoTaskSnapshot, UploadedFile
from kg_agent.services.review_commands import ResolveHumanReviewCommand
from tests.factories import NOW


class _AppendableFakeModelAdapter(FakeModelAdapter):
    """允许测试在每个人工审核后加入下一段确定性模型响应。"""

    def enqueue(self, *responses: object) -> None:
        """按后续实际 Artifact ID 追加结构化响应。"""

        self._responses.extend(responses)  # noqa: SLF001 - 测试专用可控适配器


def _decision(skill_name: str, input_ids: list[UUID]) -> ProfessionalAgentDecision:
    """构造只调用一个目标 Skill 的专业 Agent 决策。"""

    return ProfessionalAgentDecision(
        decision_summary=f"调用 {skill_name} 完成当前 AgentSubTask",
        skill_calls=[SkillCallPlan(skill_name=skill_name, input_artifact_ids=input_ids)],
    )


def _artifact_id(snapshot: DemoTaskSnapshot, artifact_type: str) -> UUID:
    """从最新快照读取指定类型的最后一个可用 Artifact ID。"""

    return next(
        item.ref.artifact_id
        for item in reversed(snapshot.artifacts)
        if item.ref.artifact_type == artifact_type
    )


def _docx_bytes() -> bytes:
    """构造包含一条制造业事实的最小真实 DOCX。"""

    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>机器人 A 安装于装配线 B。</w:t></w:r></w:p></w:body>
</w:document>"""
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return stream.getvalue()


def _llm_outputs(*items: tuple[str, dict[str, object]]) -> dict[str, object]:
    """构造通过正式 Artifact Schema 校验的 FakeModelAdapter Skill 响应。"""

    return {
        "outputs": [
            {
                "artifact_type": artifact_type,
                "content_summary": f"测试生成 {artifact_type}",
                "content": content,
            }
            for artifact_type, content in items
        ],
        "quality_signals": {"contract_valid": 1.0},
    }


@pytest.mark.asyncio
async def test_approved_ontology_and_graph_resume_to_published_graph(
    project_root: Path,
) -> None:
    """本体审核和图谱审核通过后应依次解锁后续任务并保留全部中间产物。"""

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    task_id, conversation_id, request_id = uuid4(), uuid4(), uuid4()
    filename = "manufacturing.docx"
    raw_id = uuid5(
        NAMESPACE_URL,
        f"kg-agent:upload:{request_id}:0:RawDocument:{filename}",
    )
    task_spec = TaskSpec(
        task_id=task_id,
        revision=1,
        task_intent=TaskIntent.START,
        business_goal="解析制造业文档，自建本体，抽取实体关系并审核入库",
        source_artifact_ids=[raw_id],
        target_output_types=["PublishedGraph"],
        ontology_strategy=OntologyStrategy(mode="BUILD_FROM_DOCUMENTS"),
        created_at=NOW,
    )

    def subtask(
        subtask_type: str,
        agent_type: str,
        outputs: list[str],
        *,
        dependencies: list[UUID] | None = None,
        inputs: list[UUID] | None = None,
        human_review_required: bool = False,
    ) -> AgentSubTask:
        """构造同一完整计划中的单 Agent 级子任务。"""

        deps = dependencies or []
        return AgentSubTask(
            subtask_id=uuid4(),
            master_task_id=task_id,
            decomposition_revision=1,
            name=subtask_type,
            description=f"由 {agent_type} 执行 {subtask_type}",
            goal=f"产生 {outputs}",
            subtask_type=subtask_type,
            assigned_agent_type=agent_type,
            dependency_subtask_ids=deps,
            input_artifact_ids=inputs or [],
            expected_output_types=outputs,
            completion_criteria=["产生声明的标准 Artifact"],
            human_review_required=human_review_required,
            status=SubTaskStatus.BLOCKED if deps else SubTaskStatus.READY,
            created_at=NOW,
        )

    parse = subtask(
        "document_parse",
        "document_parsing_agent",
        ["ParsedDocument", "SemanticDocument"],
        inputs=[raw_id],
    )
    ontology = subtask(
        "ontology_extract",
        "knowledge_extraction_agent",
        ["CandidateOntology"],
        dependencies=[parse.subtask_id],
        human_review_required=True,
    )
    ontology_publish = subtask(
        "ontology_publish",
        "graph_management_agent",
        ["PublishedOntology"],
        dependencies=[ontology.subtask_id],
    )
    knowledge = subtask(
        "knowledge_extract",
        "knowledge_extraction_agent",
        ["EntityDataset", "RelationDataset", "PropertyDataset", "EvidenceDataset"],
        dependencies=[parse.subtask_id, ontology_publish.subtask_id],
    )
    conflict = subtask(
        "conflict_analyze",
        "knowledge_fusion_agent",
        ["ConflictReport"],
        dependencies=[knowledge.subtask_id],
    )
    graph = subtask(
        "graph_build",
        "graph_management_agent",
        ["CandidateGraph"],
        dependencies=[conflict.subtask_id],
    )
    graph_quality = subtask(
        "graph_quality_evaluate",
        "quality_evaluation_agent",
        ["GraphQualityReport"],
        dependencies=[graph.subtask_id],
        human_review_required=True,
    )
    graph_publish = subtask(
        "graph_publish",
        "graph_management_agent",
        ["PublishedGraph"],
        dependencies=[graph_quality.subtask_id],
    )
    decomposition = TaskDecompositionResult(
        task_intent=TaskIntent.START,
        task_spec=task_spec,
        subtasks=[
            parse,
            ontology,
            ontology_publish,
            knowledge,
            conflict,
            graph,
            graph_quality,
            graph_publish,
        ],
        reused_artifact_ids=[raw_id],
    )
    model = _AppendableFakeModelAdapter([decomposition])
    service = DemoOrchestrationService(
        database=database,
        storage=InMemoryObjectStorage(),
        model_gateway=model,
        project_root=project_root,
        max_repairs=0,
        heartbeat_seconds=1,
    )

    try:
        # 首次只运行正式 DOCX 解析，取得 SemanticDocument 后再提供后续模型响应。
        first = await service.submit_and_run(
            question="根据上传文档抽取实体关系并审核入库",
            files=[
                UploadedFile(
                    filename,
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    _docx_bytes(),
                )
            ],
            task_id=task_id,
            conversation_id=conversation_id,
            request_id=request_id,
            max_worker_runs=1,
        )
        semantic_id = _artifact_id(first, "SemanticDocument")
        candidate_ontology_content = {
            "schema_version": "1.0.0",
            "name": "制造业设备本体",
            "classes": [
                {
                    "class_id": "Equipment",
                    "name": "设备",
                    "description": "制造设备",
                    "parent_class_ids": [],
                }
            ],
            "relations": [
                {
                    "relation_id": "INSTALLED_IN",
                    "name": "安装于",
                    "domain_class_id": "Equipment",
                    "range_class_id": "Equipment",
                    "description": "设备安装关系",
                }
            ],
            "properties": [],
            "source_artifact_ids": [str(semantic_id)],
        }
        # 文档解析与 Skill 选择均由确定性契约处理，FakeModel 只提供实际 LLM Skill 的输出。
        model.enqueue(_llm_outputs(("CandidateOntology", candidate_ontology_content)))
        ontology_snapshot = await service.resume_ready_workers(
            task_id=task_id,
            max_worker_runs=1,
        )
        ontology_reviews = await service.reviews.list_reviews(task_id)
        assert ontology_reviews, {
            "actions": ontology_snapshot.coordination_actions,
            "subtasks": [
                (item.subtask_type, item.status.value) for item in ontology_snapshot.subtasks
            ],
            "runs": [
                (item.agent_name, item.status.value, item.blocking_issues)
                for item in ontology_snapshot.runs
            ],
        }
        ontology_review = ontology_reviews[0]
        assert ontology_review.status is HumanReviewStatus.PENDING
        approved_ontology = await service.reviews.resolve_review(
            ResolveHumanReviewCommand(
                review_id=ontology_review.review_id,
                action="APPROVE",
                reviewer_id="test-user",
            )
        )

        assert _artifact_id(ontology_snapshot, "CandidateOntology")
        assert approved_ontology.decision_artifact_id is not None
        # 发布 Skill 读取已批准候选并确定性生成版本，不消耗 FakeModel 响应。
        published_ontology_snapshot = await service.resume_ready_workers(
            task_id=task_id,
            max_worker_runs=1,
        )
        assert any(
            item.ref.artifact_type == "PublishedOntology"
            for item in published_ontology_snapshot.artifacts
        ), {
            "actions": published_ontology_snapshot.coordination_actions,
            "subtasks": [
                (item.subtask_type, item.status.value)
                for item in published_ontology_snapshot.subtasks
            ],
            "runs": [
                (
                    item.agent_name,
                    item.status.value,
                    item.skills_used,
                    item.blocking_issues,
                    [str(value) for value in item.input_artifact_ids],
                )
                for item in published_ontology_snapshot.runs
            ],
        }
        pointer = {
            "source_artifact_id": str(semantic_id),
            "locator": "word/paragraph:1",
            "quote": "机器人 A 安装于装配线 B。",
        }
        entity_content = {
            "schema_version": "1.0.0",
            "entities": [
                {
                    "entity_id": "robot-a",
                    "name": "机器人 A",
                    "class_id": "Equipment",
                    "aliases": [],
                    "evidence": [pointer],
                },
                {
                    "entity_id": "line-b",
                    "name": "装配线 B",
                    "class_id": "Equipment",
                    "aliases": [],
                    "evidence": [pointer],
                },
            ],
        }
        relation_content = {
            "schema_version": "1.0.0",
            "relations": [
                {
                    "relation_id": "installed-1",
                    "source_entity_id": "robot-a",
                    "relation_type_id": "INSTALLED_IN",
                    "target_entity_id": "line-b",
                    "evidence": [pointer],
                }
            ],
        }
        model.enqueue(
            {
                "entities": entity_content["entities"],
                "relations": relation_content["relations"],
                "properties": [],
            }
        )
        knowledge_snapshot = await service.resume_ready_workers(task_id=task_id, max_worker_runs=1)
        assert _artifact_id(knowledge_snapshot, "EntityDataset")
        assert _artifact_id(knowledge_snapshot, "RelationDataset")
        assert _artifact_id(knowledge_snapshot, "EvidenceDataset")

        model.enqueue(
            _llm_outputs(("ConflictReport", {"schema_version": "1.0.0", "conflicts": []}))
        )
        conflict_snapshot = await service.resume_ready_workers(task_id=task_id, max_worker_runs=1)
        assert _artifact_id(conflict_snapshot, "ConflictReport")

        graph_snapshot = await service.resume_ready_workers(task_id=task_id, max_worker_runs=1)
        candidate_graph_id = _artifact_id(graph_snapshot, "CandidateGraph")

        model.enqueue(
            _llm_outputs(
                (
                    "GraphQualityReport",
                    {
                        "schema_version": "1.0.0",
                        "target_artifact_id": str(candidate_graph_id),
                        "scores": {"integrity": 1.0},
                        "issues": [],
                        "recommendations": [],
                        "passed": True,
                    },
                )
            ),
        )
        quality_snapshot = await service.resume_ready_workers(task_id=task_id, max_worker_runs=1)
        graph_reviews = await service.reviews.list_reviews(task_id)
        graph_review = next(item for item in graph_reviews if item.target_type.value == "GRAPH")
        assert graph_review.status is HumanReviewStatus.PENDING
        approved_graph = await service.reviews.resolve_review(
            ResolveHumanReviewCommand(
                review_id=graph_review.review_id,
                action="APPROVE",
                reviewer_id="test-user",
            )
        )
        assert approved_graph.decision_artifact_id is not None

        model.enqueue(
            TaskDecompositionResult(
                task_intent=TaskIntent.CONTINUE,
                task_spec=task_spec,
                subtasks=[],
            ),
        )
        completed = await service.resume_ready_workers(
            task_id=task_id,
            max_worker_runs=1,
        )

        assert completed.coordination_actions == ["TASK_COMPLETED"]
        assert _artifact_id(completed, "PublishedGraph")
        produced_types = {item.ref.artifact_type for item in completed.artifacts}
        assert {
            "RawDocument",
            "ParsedDocument",
            "SemanticDocument",
            "CandidateOntology",
            "OntologyApproval",
            "PublishedOntology",
            "EntityDataset",
            "RelationDataset",
            "EvidenceDataset",
            "ConflictReport",
            "CandidateGraph",
            "GraphQualityReport",
            "GraphApproval",
            "PublishedGraph",
        } <= produced_types
        assert quality_snapshot.coordination_actions == ["REVIEW_CREATED"]
    finally:
        await database.dispose()
