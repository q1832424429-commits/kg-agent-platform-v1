"""验证人工审核、ChangeSet、Approval 与 PublishGuard 完整闭环。"""

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from kg_agent.agents.professional.factory import build_professional_agent_pool
from kg_agent.agents.professional.models import (
    ProfessionalAgentDecision,
    SkillCallPlan,
)
from kg_agent.agents.professional.prompts import ProfessionalPromptRepository
from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_content_models import (
    ApprovalContent,
    ChangeOperation,
)
from kg_agent.domain.artifact_models import ArtifactCreateCommand, ArtifactRecord
from kg_agent.domain.enums import (
    AgentRunStatus,
    AssignmentStatus,
    HumanReviewStatus,
    MasterTaskStatus,
    ReviewOption,
    ReviewTargetType,
    SubTaskStatus,
    TaskIntent,
)
from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.exceptions import (
    ArtifactValidationError,
    PublishGuardError,
    SkillPermissionError,
)
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyHumanReviewRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.assignment_service import AssignmentService
from kg_agent.services.postgres_artifact_service import PostgresArtifactService
from kg_agent.services.postgres_context_service import PostgresContextService
from kg_agent.services.postgres_human_review_service import PostgresHumanReviewService
from kg_agent.services.postgres_publish_guard import PostgresPublishGuard
from kg_agent.services.postgres_worker_services import PostgresWorkerServices
from kg_agent.services.review_commands import (
    CreateHumanReviewCommand,
    ResolveHumanReviewCommand,
)
from kg_agent.workers.assignment_worker import AssignmentWorker
from tests.factories import NOW, make_decomposition, make_subtask


async def create_graph_review_inputs(
    *,
    database: Database,
    storage: InMemoryObjectStorage,
    registry: ArtifactRegistry,
) -> tuple[PostgresArtifactService, ArtifactRecord, ArtifactRecord]:
    """创建一个任务及相互绑定的 CandidateGraph、GraphQualityReport。"""

    task_id = uuid4()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id),
        source_artifacts=[],
        expected_version=None,
    )
    service = PostgresArtifactService(
        database.session_factory,
        storage=storage,
        registry=registry,
    )
    candidate = await service.create(
        ArtifactCreateCommand(
            artifact_type="CandidateGraph",
            task_id=task_id,
            task_spec_revision=1,
            producer_agent="system",
            producer_skill="test_fixture",
            producer_run_id=uuid4(),
            content_summary="待用户审核的候选图谱",
            content={
                "schema_version": "1.0.0",
                "entities": [],
                "relations": [],
                "properties": [],
                "evidence": [],
            },
        )
    )
    report = await service.create(
        ArtifactCreateCommand(
            artifact_type="GraphQualityReport",
            task_id=task_id,
            task_spec_revision=1,
            producer_agent="system",
            producer_skill="test_fixture",
            producer_run_id=uuid4(),
            parent_artifact_ids=[candidate.ref.artifact_id],
            content_summary="候选图谱质量报告",
            content={
                "schema_version": "1.0.0",
                "target_artifact_id": str(candidate.ref.artifact_id),
                "scores": {"overall": 0.95},
                "issues": [],
                "recommendations": [],
                "passed": True,
            },
        )
    )
    return service, candidate, report


def review_service(
    *,
    database: Database,
    storage: InMemoryObjectStorage,
    registry: ArtifactRegistry,
) -> PostgresHumanReviewService:
    """构造使用真实持久化和内存对象存储的审核应用服务。"""

    return PostgresHumanReviewService(
        database.session_factory,
        storage=storage,
        registry=registry,
    )


async def request_graph_review(
    *,
    service: PostgresHumanReviewService,
    candidate: ArtifactRecord,
    report: ArtifactRecord,
):
    """为候选图谱创建默认三操作人工审核请求。"""

    return await service.request_review(
        CreateHumanReviewCommand(
            task_id=candidate.ref.task_id,
            target_type=ReviewTargetType.GRAPH,
            target_artifact_id=candidate.ref.artifact_id,
            quality_report_artifact_ids=[report.ref.artifact_id],
        )
    )


@pytest.mark.asyncio
async def test_graph_review_still_requires_quality_report(database: Database) -> None:
    """取消本体评估不能放宽候选图谱原有的质量报告门禁。"""

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    _, candidate, _ = await create_graph_review_inputs(
        database=database,
        storage=storage,
        registry=registry,
    )
    service = review_service(database=database, storage=storage, registry=registry)

    with pytest.raises(ArtifactValidationError, match="GraphQualityReport"):
        await service.request_review(
            CreateHumanReviewCommand(
                task_id=candidate.ref.task_id,
                target_type=ReviewTargetType.GRAPH,
                target_artifact_id=candidate.ref.artifact_id,
            )
        )


@pytest.mark.asyncio
async def test_approve_creates_bound_approval_and_audited_context(
    database: Database,
) -> None:
    """批准应生成绑定目标 checksum 的 GraphApproval，并更新状态和上下文。"""

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    artifact_service, candidate, report = await create_graph_review_inputs(
        database=database,
        storage=storage,
        registry=registry,
    )
    service = review_service(database=database, storage=storage, registry=registry)

    requested = await request_graph_review(
        service=service,
        candidate=candidate,
        report=report,
    )
    async with database.session_factory() as session:
        waiting = await SqlAlchemyTaskRepository(session).get_master(candidate.ref.task_id)
    assert waiting is not None and waiting.status is MasterTaskStatus.WAITING_HUMAN
    materials = await service.get_review_materials(requested.review_id)
    assert materials.target.artifact_id == candidate.ref.artifact_id
    assert [item.artifact_id for item in materials.quality_reports] == [report.ref.artifact_id]

    resolved = await service.resolve_review(
        ResolveHumanReviewCommand(
            review_id=requested.review_id,
            action=ReviewOption.APPROVE,
            reviewer_feedback="确认发布该候选图谱。",
            publication_title="制造设备故障图谱 V1",
        )
    )

    assert resolved.status is HumanReviewStatus.APPROVED
    assert resolved.decision_artifact_id is not None
    raw = await artifact_service.read_content(resolved.decision_artifact_id)
    approval = ApprovalContent.model_validate(json.loads(raw))
    assert approval.target_artifact_id == candidate.ref.artifact_id
    assert approval.target_checksum == candidate.ref.checksum
    assert approval.publication_title == "制造设备故障图谱 V1"
    async with database.session_factory() as session:
        active = await SqlAlchemyTaskRepository(session).get_master(candidate.ref.task_id)
        context = await SqlAlchemyContextRepository(session).get(candidate.ref.task_id)
        stored = await SqlAlchemyHumanReviewRepository(session).get(requested.review_id)
    assert active is not None and active.status is MasterTaskStatus.ACTIVE
    assert stored == resolved
    assert context is not None
    assert requested.review_id in context.human_review_request_ids
    assert len(context.execution_trace_ids) >= 2


@pytest.mark.asyncio
async def test_approval_unlocks_only_reviewed_publish_dependency(
    database: Database,
) -> None:
    """审核批准产生 Approval 后，才为完整计划中的图谱发布任务创建 Assignment。"""

    task_id = uuid4()
    quality = make_subtask(
        task_id,
        subtask_type="graph_quality_evaluate",
        agent_type="quality_evaluation_agent",
        outputs=["GraphQualityReport"],
        human_review_required=True,
    )
    publish = make_subtask(
        task_id,
        subtask_type="graph_publish",
        agent_type="graph_management_agent",
        outputs=["PublishedGraph"],
        dependencies=[quality.subtask_id],
    )
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[quality, publish]),
        source_artifacts=[],
        expected_version=None,
    )
    async with database.session_factory() as session, session.begin():
        assignments = SqlAlchemyAssignmentRepository(session)
        assignment_service = AssignmentService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=assignments,
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
        )
        claimed = await assignment_service.claim_next(
            agent_type="quality_evaluation_agent", worker_id="quality-review-worker"
        )
        assert claimed is not None
        running = await assignment_service.start(
            assignment_id=claimed.assignment_id,
            worker_id="quality-review-worker",
            fencing_token=claimed.fencing_token,
        )
        await assignment_service.complete(
            assignment_id=running.assignment_id,
            worker_id="quality-review-worker",
            fencing_token=running.fencing_token,
            succeeded=True,
        )

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    artifacts = PostgresArtifactService(
        database.session_factory,
        storage=storage,
        registry=registry,
    )
    candidate = await artifacts.create(
        ArtifactCreateCommand(
            artifact_type="CandidateGraph",
            task_id=task_id,
            task_spec_revision=1,
            producer_agent="system",
            producer_skill="test_fixture",
            producer_run_id=uuid4(),
            content_summary="待审核候选图谱",
            content={
                "schema_version": "1.0.0",
                "entities": [],
                "relations": [],
                "properties": [],
                "evidence": [],
            },
        )
    )
    report = await artifacts.create(
        ArtifactCreateCommand(
            artifact_type="GraphQualityReport",
            task_id=task_id,
            task_spec_revision=1,
            producer_agent="system",
            producer_skill="test_fixture",
            producer_run_id=uuid4(),
            parent_artifact_ids=[candidate.ref.artifact_id],
            content_summary="候选图谱质量报告",
            content={
                "schema_version": "1.0.0",
                "target_artifact_id": str(candidate.ref.artifact_id),
                "scores": {"overall": 1.0},
                "issues": [],
                "passed": True,
            },
        )
    )
    reviews = review_service(database=database, storage=storage, registry=registry)
    review = await reviews.request_review(
        CreateHumanReviewCommand(
            task_id=task_id,
            subtask_id=quality.subtask_id,
            target_type=ReviewTargetType.GRAPH,
            target_artifact_id=candidate.ref.artifact_id,
            quality_report_artifact_ids=[report.ref.artifact_id],
        )
    )
    await reviews.resolve_review(
        ResolveHumanReviewCommand(
            review_id=review.review_id,
            action=ReviewOption.APPROVE,
        )
    )

    async with database.session_factory() as session:
        stored_publish = await SqlAlchemyTaskRepository(session).get_subtask(
            publish.subtask_id
        )
        task_assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
    assert stored_publish is not None
    assert stored_publish.status is SubTaskStatus.ASSIGNED
    assert task_assignments[-1].status is AssignmentStatus.PENDING
    assert task_assignments[-1].subtask_id == publish.subtask_id


@pytest.mark.asyncio
async def test_changeset_requires_re_evaluation_and_cannot_publish(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """用户修改只产生 ChangeSet；没有新质量报告和 Approval 时不能发布。"""

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    artifact_service, candidate, report = await create_graph_review_inputs(
        database=database,
        storage=storage,
        registry=registry,
    )
    service = review_service(database=database, storage=storage, registry=registry)
    requested = await request_graph_review(
        service=service,
        candidate=candidate,
        report=report,
    )

    resolved = await service.resolve_review(
        ResolveHumanReviewCommand(
            review_id=requested.review_id,
            action=ReviewOption.SUBMIT_CHANGES,
            reviewer_feedback="删除错误实体。",
            operations=[
                ChangeOperation(
                    operation_id="change-1",
                    action="DELETE",
                    target_kind="ENTITY",
                    target_id="entity-wrong",
                )
            ],
        )
    )

    assert resolved.status is HumanReviewStatus.CHANGES_REQUESTED
    assert resolved.decision_artifact_id is not None
    async with database.session_factory() as session:
        changeset = await SqlAlchemyArtifactRepository(session).get(resolved.decision_artifact_id)
    assert changeset is not None
    assert changeset.ref.artifact_type == "GraphChangeSet"
    payload = json.loads(await artifact_service.read_content(changeset.ref.artifact_id))
    assert payload["target_checksum"] == candidate.ref.checksum

    guarded = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=registry,
        side_effect_authorizer=PostgresPublishGuard(
            database.session_factory,
            agents=agent_registry,
            storage=storage,
        ),
    )
    with pytest.raises(PublishGuardError, match="Approval"):
        await guarded.invoke(
            SkillInvocation(
                invocation_id=uuid4(),
                agent_type="graph_management_agent",
                skill_name="publish_graph",
                input_artifacts=[candidate.ref, changeset.ref],
            )
        )


@pytest.mark.asyncio
async def test_publish_guard_rejects_approval_for_another_candidate(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """旧 Approval 不能用于同任务中 ID/checksum 不同的新候选图谱。"""

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    artifact_service, candidate, report = await create_graph_review_inputs(
        database=database,
        storage=storage,
        registry=registry,
    )
    service = review_service(database=database, storage=storage, registry=registry)
    requested = await request_graph_review(
        service=service,
        candidate=candidate,
        report=report,
    )
    resolved = await service.resolve_review(
        ResolveHumanReviewCommand(
            review_id=requested.review_id,
            action=ReviewOption.APPROVE,
        )
    )
    assert resolved.decision_artifact_id is not None
    async with database.session_factory() as session:
        approval = await SqlAlchemyArtifactRepository(session).get(resolved.decision_artifact_id)
    assert approval is not None
    another = await artifact_service.create(
        ArtifactCreateCommand(
            artifact_type="CandidateGraph",
            task_id=candidate.ref.task_id,
            task_spec_revision=1,
            producer_agent="system",
            producer_skill="test_fixture",
            producer_run_id=uuid4(),
            content_summary="修改后的新候选图谱",
            content={
                "schema_version": "1.0.0",
                "entities": [],
                "relations": [],
                "properties": [],
                "evidence": [],
            },
        )
    )
    guarded = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=registry,
        side_effect_authorizer=PostgresPublishGuard(
            database.session_factory,
            agents=agent_registry,
            storage=storage,
        ),
    )

    with pytest.raises(PublishGuardError, match="血缘|当前候选"):
        await guarded.invoke(
            SkillInvocation(
                invocation_id=uuid4(),
                agent_type="graph_management_agent",
                skill_name="publish_graph",
                input_artifacts=[another.ref, approval.ref],
            )
        )


@pytest.mark.asyncio
async def test_forged_approval_checksum_is_rejected_by_artifact_service(
    database: Database,
) -> None:
    """即使伪装成审核服务，错误 checksum 的 Approval 也不能落库。"""

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    artifact_service, candidate, _ = await create_graph_review_inputs(
        database=database,
        storage=storage,
        registry=registry,
    )

    with pytest.raises(ArtifactValidationError, match="checksum"):
        await artifact_service.create(
            ArtifactCreateCommand(
                artifact_type="GraphApproval",
                task_id=candidate.ref.task_id,
                task_spec_revision=1,
                producer_agent="user",
                producer_skill="human_review_service",
                producer_run_id=uuid4(),
                parent_artifact_ids=[candidate.ref.artifact_id],
                content_summary="伪造批准",
                content={
                    "schema_version": "1.0.0",
                    "target_artifact_id": str(candidate.ref.artifact_id),
                    "target_checksum": "sha256:wrong",
                    "decision": "APPROVED",
                    "comment": "",
                },
            )
        )


@pytest.mark.asyncio
async def test_publish_guard_rejects_approval_without_human_review_record(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """仅伪造正确内容的 Approval 仍不能绕过真实 HumanReviewRequest。"""

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    artifact_service, candidate, _ = await create_graph_review_inputs(
        database=database,
        storage=storage,
        registry=registry,
    )
    forged = await artifact_service.create(
        ArtifactCreateCommand(
            artifact_type="GraphApproval",
            task_id=candidate.ref.task_id,
            task_spec_revision=1,
            producer_agent="user",
            producer_skill="human_review_service",
            producer_run_id=uuid4(),
            parent_artifact_ids=[candidate.ref.artifact_id],
            content_summary="没有审核记录的伪造批准",
            content={
                "schema_version": "1.0.0",
                "target_artifact_id": str(candidate.ref.artifact_id),
                "target_checksum": candidate.ref.checksum,
                "decision": "APPROVED",
                "comment": "",
            },
        )
    )
    guarded = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=registry,
        side_effect_authorizer=PostgresPublishGuard(
            database.session_factory,
            agents=agent_registry,
            storage=storage,
        ),
    )

    with pytest.raises(PublishGuardError, match="HumanReviewRequest"):
        await guarded.invoke(
            SkillInvocation(
                invocation_id=uuid4(),
                agent_type="graph_management_agent",
                skill_name="publish_graph",
                input_artifacts=[candidate.ref, forged.ref],
            )
        )


@pytest.mark.asyncio
async def test_approved_graph_is_published_only_by_graph_management_agent(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """审核通过后由图谱管理 Agent 调用受保护 Skill 生成 PublishedGraph。"""

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    artifact_service, candidate, report = await create_graph_review_inputs(
        database=database,
        storage=storage,
        registry=registry,
    )
    service = review_service(database=database, storage=storage, registry=registry)
    requested = await request_graph_review(
        service=service,
        candidate=candidate,
        report=report,
    )
    resolved = await service.resolve_review(
        ResolveHumanReviewCommand(
            review_id=requested.review_id,
            action=ReviewOption.APPROVE,
        )
    )
    assert resolved.decision_artifact_id is not None
    async with database.session_factory() as session:
        approval = await SqlAlchemyArtifactRepository(session).get(resolved.decision_artifact_id)
    assert approval is not None

    contexts = PostgresContextService(database.session_factory)
    current = await contexts.get(candidate.ref.task_id)
    assert current is not None
    spec = current.current_task_spec.model_copy(
        update={
            "revision": 2,
            "task_intent": TaskIntent.CONTINUE,
            "source_artifact_ids": [
                candidate.ref.artifact_id,
                approval.ref.artifact_id,
            ],
            "target_output_types": ["PublishedGraph"],
            "created_at": NOW + timedelta(seconds=1),
        }
    )
    publish_subtask = make_subtask(
        candidate.ref.task_id,
        subtask_type="graph_publish",
        agent_type="graph_management_agent",
        inputs=[candidate.ref.artifact_id, approval.ref.artifact_id],
        outputs=["PublishedGraph"],
        decomposition_revision=2,
    )
    await contexts.publish_decomposition(
        conversation_id=current.conversation.conversation_id,
        result=TaskDecompositionResult(
            task_intent=TaskIntent.CONTINUE,
            task_spec=spec,
            subtasks=[publish_subtask],
            reused_artifact_ids=[
                candidate.ref.artifact_id,
                approval.ref.artifact_id,
            ],
        ),
        source_artifacts=[],
        expected_version=current.version,
    )

    guarded_skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=registry,
        side_effect_authorizer=PostgresPublishGuard(
            database.session_factory,
            agents=agent_registry,
            storage=storage,
        ),
        model_gateway=FakeModelAdapter([]),
        artifact_reader=artifact_service,
    )
    decision = ProfessionalAgentDecision(
        decision_summary="发布已经通过用户审核的候选图谱",
        skill_calls=[
            SkillCallPlan(
                skill_name="publish_graph",
                input_artifact_ids=[
                    candidate.ref.artifact_id,
                    approval.ref.artifact_id,
                ],
                parameters={"graph_version": "1.0.0"},
            )
        ],
    )
    pool = build_professional_agent_pool(
        model_gateway=FakeModelAdapter([decision]),
        agent_registry=agent_registry,
        skill_registry=guarded_skills,
        artifact_registry=registry,
        artifact_writer=artifact_service,
        prompt_repository=ProfessionalPromptRepository(
            project_root / "configs" / "prompts" / "professional"
        ),
    )
    worker = AssignmentWorker(
        worker_id="worker-graph-publish",
        services=PostgresWorkerServices(
            database.session_factory,
            agents=agent_registry,
            skills=guarded_skills,
        ),
        agents=pool,
        heartbeat_seconds=1,
    )

    result = await worker.run_once("graph_management_agent")

    assert result is not None and result.status is AgentRunStatus.SUCCESS
    assert len(result.output_artifact_ids) == 1
    async with database.session_factory() as session:
        published = await SqlAlchemyArtifactRepository(session).get(result.output_artifact_ids[0])
    assert published is not None
    assert published.ref.artifact_type == "PublishedGraph"
    content = json.loads(await artifact_service.read_content(published.ref.artifact_id))
    assert content["approval_artifact_id"] == str(approval.ref.artifact_id)


@pytest.mark.asyncio
async def test_non_graph_management_agent_cannot_publish_approved_graph(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """即使持有真实 Approval，其他专业 Agent 也不能调用发布 Skill。"""

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    _, candidate, report = await create_graph_review_inputs(
        database=database,
        storage=storage,
        registry=registry,
    )
    service = review_service(database=database, storage=storage, registry=registry)
    requested = await request_graph_review(
        service=service,
        candidate=candidate,
        report=report,
    )
    resolved = await service.resolve_review(
        ResolveHumanReviewCommand(
            review_id=requested.review_id,
            action=ReviewOption.APPROVE,
        )
    )
    assert resolved.decision_artifact_id is not None
    async with database.session_factory() as session:
        approval = await SqlAlchemyArtifactRepository(session).get(resolved.decision_artifact_id)
    assert approval is not None
    guarded = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=registry,
        side_effect_authorizer=PostgresPublishGuard(
            database.session_factory,
            agents=agent_registry,
            storage=storage,
        ),
    )

    with pytest.raises(SkillPermissionError, match="无权调用"):
        await guarded.invoke(
            SkillInvocation(
                invocation_id=uuid4(),
                agent_type="knowledge_fusion_agent",
                skill_name="publish_graph",
                input_artifacts=[candidate.ref, approval.ref],
            )
        )


@pytest.mark.asyncio
async def test_fused_ontology_can_be_approved_and_authorized_for_publish(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """融合候选本体无需质量报告即可由用户批准并通过发布门禁。"""

    task_id = uuid4()
    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id),
        source_artifacts=[],
        expected_version=None,
    )
    artifacts = PostgresArtifactService(
        database.session_factory,
        storage=storage,
        registry=registry,
    )
    candidate = await artifacts.create(
        ArtifactCreateCommand(
            artifact_type="FusedCandidateOntology",
            task_id=task_id,
            task_spec_revision=1,
            producer_agent="system",
            producer_skill="test_fixture",
            producer_run_id=uuid4(),
            content_summary="融合后的候选本体",
            content={
                "schema_version": "1.0.0",
                "name": "制造业本体",
                "classes": [],
                "relations": [],
                "properties": [],
                "source_artifact_ids": [],
                "alignment_notes": ["已完成候选本体融合"],
            },
        )
    )
    service = review_service(database=database, storage=storage, registry=registry)
    requested = await service.request_review(
        CreateHumanReviewCommand(
            task_id=task_id,
            target_type=ReviewTargetType.ONTOLOGY,
            target_artifact_id=candidate.ref.artifact_id,
        )
    )
    materials = await service.get_review_materials(requested.review_id)
    assert materials.quality_reports == []
    resolved = await service.resolve_review(
        ResolveHumanReviewCommand(
            review_id=requested.review_id,
            action=ReviewOption.APPROVE,
        )
    )
    assert resolved.decision_artifact_id is not None
    async with database.session_factory() as session:
        approval = await SqlAlchemyArtifactRepository(session).get(resolved.decision_artifact_id)
    assert approval is not None and approval.ref.artifact_type == "OntologyApproval"

    guarded = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=registry,
        side_effect_authorizer=PostgresPublishGuard(
            database.session_factory,
            agents=agent_registry,
            storage=storage,
        ),
        model_gateway=FakeModelAdapter([]),
        artifact_reader=artifacts,
    )
    result = await guarded.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="graph_management_agent",
            skill_name="publish_ontology",
            input_artifacts=[candidate.ref, approval.ref],
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    assert [item.artifact_type for item in result.outputs] == ["PublishedOntology"]


@pytest.mark.asyncio
async def test_reject_records_feedback_without_creating_approval(
    database: Database,
) -> None:
    """拒绝审核应保留用户反馈并返回 ACTIVE，不能生成 Approval。"""

    storage = InMemoryObjectStorage()
    registry = ArtifactRegistry.v1()
    _, candidate, report = await create_graph_review_inputs(
        database=database,
        storage=storage,
        registry=registry,
    )
    service = review_service(database=database, storage=storage, registry=registry)
    requested = await request_graph_review(
        service=service,
        candidate=candidate,
        report=report,
    )

    resolved = await service.resolve_review(
        ResolveHumanReviewCommand(
            review_id=requested.review_id,
            action=ReviewOption.REJECT,
            reviewer_feedback="实体分类错误，需要重新融合。",
        )
    )

    assert resolved.status is HumanReviewStatus.REJECTED
    assert resolved.decision_artifact_id is None
    assert resolved.reviewer_feedback == "实体分类错误，需要重新融合。"
    async with database.session_factory() as session:
        master = await SqlAlchemyTaskRepository(session).get_master(candidate.ref.task_id)
        records = await SqlAlchemyArtifactRepository(session).list_for_task(candidate.ref.task_id)
    assert master is not None and master.status is MasterTaskStatus.ACTIVE
    assert not any(record.ref.artifact_type == "GraphApproval" for record in records)
