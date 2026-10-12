"""验证 AssignmentWorker 只负责确定性运行，不承担专业决策。"""

from io import BytesIO
from pathlib import Path
from uuid import uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from kg_agent.agents.professional.factory import build_professional_agent_pool
from kg_agent.agents.professional.models import (
    ProfessionalAgentDecision,
    SkillCallPlan,
)
from kg_agent.agents.professional.prompts import ProfessionalPromptRepository
from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.artifact_models import ArtifactCreateCommand, ArtifactRecord
from kg_agent.domain.enums import AgentRunStatus, AssignmentStatus, MasterTaskStatus
from kg_agent.infrastructure.database.artifact_repository import (
    SqlAlchemyArtifactRepository,
)
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.memory.memory_service import LongTermMemoryService
from kg_agent.memory.stores import DisabledLongTermMemoryStore
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.postgres_artifact_service import PostgresArtifactService
from kg_agent.services.postgres_context_service import PostgresContextService
from kg_agent.services.postgres_worker_services import PostgresWorkerServices
from kg_agent.workers.assignment_worker import AssignmentWorker
from tests.factories import make_decomposition, make_subtask


class RejectingArtifactWriter:
    """确保等待用户分支不会尝试创建任何 Artifact。"""

    async def create(self, command: ArtifactCreateCommand) -> ArtifactRecord:
        """若 Worker 或 Agent 错误写入 Artifact，则立即使测试失败。"""

        raise AssertionError(f"等待用户时不应创建 Artifact：{command.artifact_type}")


class RecordingMemoryService(LongTermMemoryService):
    """记录 Worker 是否错误地尝试写入单个 Agent 的碎片经验。"""

    def __init__(self) -> None:
        """使用禁用存储初始化，仅覆写旧 Agent 经验写入入口。"""

        super().__init__(DisabledLongTermMemoryStore(), user_id="worker-order-test")
        self.called = False

    async def safe_remember_agent_result(self, result: AgentRunResult) -> None:
        """若新 Worker 仍调用旧入口，则记录一次架构回归。"""

        self.called = True


def _docx_bytes() -> bytes:
    """构造供 Worker 成功路径使用的最小真实 DOCX。"""

    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body><w:p><w:r><w:t>机器人属于装配设备。</w:t></w:r></w:p></w:body>
</w:document>"""
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_worker_runs_real_agent_and_submits_user_waiting_result(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """Worker 只做认领、调用与提交，Skill 决策来自真实专业 Agent。"""

    task_id = uuid4()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    artifacts = ArtifactRegistry.v1()
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )
    decision = ProfessionalAgentDecision(
        decision_summary="输入文档缺少访问凭证",
        requires_user_input=True,
        user_question="请提供文档访问凭证。",
    )
    selection_model = FakeModelAdapter([decision])
    pool = build_professional_agent_pool(
        model_gateway=selection_model,
        agent_registry=agent_registry,
        skill_registry=skills,
        artifact_registry=artifacts,
        artifact_writer=RejectingArtifactWriter(),
        prompt_repository=ProfessionalPromptRepository(
            project_root / "configs" / "prompts" / "professional"
        ),
    )
    worker = AssignmentWorker(
        worker_id="worker-document-1",
        services=PostgresWorkerServices(
            database.session_factory,
            agents=agent_registry,
            skills=skills,
        ),
        agents=pool,
        heartbeat_seconds=1,
    )

    result = await worker.run_once("document_parsing_agent")

    assert result is not None
    assert result.status is AgentRunStatus.NEEDS_USER_INPUT
    assert result.user_question == "请提供文档访问凭证。"
    async with database.session_factory() as session:
        master = await SqlAlchemyTaskRepository(session).get_master(task_id)
    assert master is not None and master.status is MasterTaskStatus.WAITING_USER


@pytest.mark.asyncio
async def test_worker_does_not_claim_assignment_for_another_role(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """数据源解析 Worker 不能认领知识抽取 Agent 的 Assignment。"""

    task_id = uuid4()
    extraction = make_subtask(
        task_id,
        subtask_type="ontology_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["CandidateOntology"],
    )
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[extraction]),
        source_artifacts=[],
        expected_version=None,
    )
    artifacts = ArtifactRegistry.v1()
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )
    pool = build_professional_agent_pool(
        model_gateway=FakeModelAdapter([]),
        agent_registry=agent_registry,
        skill_registry=skills,
        artifact_registry=artifacts,
        artifact_writer=RejectingArtifactWriter(),
        prompt_repository=ProfessionalPromptRepository(
            project_root / "configs" / "prompts" / "professional"
        ),
    )
    worker = AssignmentWorker(
        worker_id="worker-document-1",
        services=PostgresWorkerServices(
            database.session_factory,
            agents=agent_registry,
            skills=skills,
        ),
        agents=pool,
    )

    result = await worker.run_once("document_parsing_agent")

    assert result is None


@pytest.mark.asyncio
async def test_worker_success_path_persists_artifacts_result_and_assignment(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """成功路径应贯通真实专业 Agent、正式 DOCX Skill、Artifact 和结果提交。"""

    task_id = uuid4()
    raw_id = uuid4()
    subtask = make_subtask(
        task_id,
        inputs=[raw_id],
        outputs=["ParsedDocument", "SemanticDocument"],
    )
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[subtask]),
        source_artifacts=[],
        expected_version=None,
    )
    artifacts = ArtifactRegistry.v1()
    storage = InMemoryObjectStorage()
    artifact_service = PostgresArtifactService(
        database.session_factory,
        storage=storage,
        registry=artifacts,
    )
    await artifact_service.create(
        ArtifactCreateCommand(
            artifact_id=raw_id,
            artifact_type="RawDocument",
            schema_version="1.0.0",
            task_id=task_id,
            task_spec_revision=1,
            producer_agent="user",
            producer_skill="upload_document",
            producer_run_id=uuid4(),
            content_summary="用户上传文件：worker-success.docx",
            media_type=("application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
            content=_docx_bytes(),
        )
    )
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
        model_gateway=FakeModelAdapter([]),
        artifact_reader=artifact_service,
    )
    decision = ProfessionalAgentDecision(
        decision_summary="解析已分配的原始文档",
        skill_calls=[
            SkillCallPlan(
                skill_name="parse_document",
                input_artifact_ids=[raw_id],
            )
        ],
    )
    parse_selection_model = FakeModelAdapter([decision])
    pool = build_professional_agent_pool(
        model_gateway=parse_selection_model,
        agent_registry=agent_registry,
        skill_registry=skills,
        artifact_registry=artifacts,
        artifact_writer=artifact_service,
        prompt_repository=ProfessionalPromptRepository(
            project_root / "configs" / "prompts" / "professional"
        ),
    )
    memory = RecordingMemoryService()
    worker = AssignmentWorker(
        worker_id="worker-document-success",
        services=PostgresWorkerServices(
            database.session_factory,
            agents=agent_registry,
            skills=skills,
            memory_service=memory,
        ),
        agents=pool,
        heartbeat_seconds=1,
    )

    result = await worker.run_once("document_parsing_agent")

    assert result is not None and result.status is AgentRunStatus.SUCCESS
    async with database.session_factory() as session:
        records = await SqlAlchemyArtifactRepository(session).list_for_task(task_id)
        assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
    assert {record.ref.artifact_type for record in records} == {
        "RawDocument",
        "ParsedDocument",
        "SemanticDocument",
    }
    assert len(result.skill_artifact_bindings) == 1
    binding = result.skill_artifact_bindings[0]
    assert binding.skill_name == "parse_document"
    assert binding.input_artifact_ids == [raw_id]
    assert binding.output_artifact_ids == result.output_artifact_ids
    assert binding.output_artifact_types == ["ParsedDocument", "SemanticDocument"]
    assert result.metrics["skill.parse_document.duration_ms"] >= 0
    assert result.metrics["skill_selection_mode"] == "DETERMINISTIC_CONTRACT"
    assert parse_selection_model.calls == []
    assert assignments[-1].status.value == "SUCCEEDED"
    assert memory.called is False


@pytest.mark.asyncio
async def test_worker_task_filter_does_not_claim_another_tasks_assignment(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """演示运行指定 task_id 后，不得误领其他任务中同角色的 Assignment。"""

    first_task_id = uuid4()
    target_task_id = uuid4()
    for task_id in (first_task_id, target_task_id):
        await PostgresContextService(database.session_factory).publish_decomposition(
            conversation_id=uuid4(),
            result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
            source_artifacts=[],
            expected_version=None,
        )

    artifacts = ArtifactRegistry.v1()
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )
    pool = build_professional_agent_pool(
        model_gateway=FakeModelAdapter(
            [
                ProfessionalAgentDecision(
                    decision_summary="仅处理目标任务并等待用户补充",
                    requires_user_input=True,
                    user_question="请补充目标任务的文档信息。",
                )
            ]
        ),
        agent_registry=agent_registry,
        skill_registry=skills,
        artifact_registry=artifacts,
        artifact_writer=RejectingArtifactWriter(),
        prompt_repository=ProfessionalPromptRepository(
            project_root / "configs" / "prompts" / "professional"
        ),
    )
    worker = AssignmentWorker(
        worker_id="demo-scoped-worker",
        services=PostgresWorkerServices(
            database.session_factory,
            agents=agent_registry,
            skills=skills,
        ),
        agents=pool,
        heartbeat_seconds=1,
    )

    result = await worker.run_once("document_parsing_agent", task_id=target_task_id)

    assert result is not None and result.task_id == target_task_id
    async with database.session_factory() as session:
        repository = SqlAlchemyAssignmentRepository(session)
        first_assignments = await repository.list_for_task(first_task_id)
        target_assignments = await repository.list_for_task(target_task_id)
    assert first_assignments[0].status is AssignmentStatus.PENDING
    assert first_assignments[0].claimed_by is None
    assert target_assignments[0].status is AssignmentStatus.FAILED
    assert target_assignments[0].claimed_by == "demo-scoped-worker"
