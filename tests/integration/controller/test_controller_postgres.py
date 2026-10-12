"""验证第四阶段总控 Agent 与 PostgreSQL 适配层的真实发布闭环。"""

from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from kg_agent.agents.controller.agent import ControllerAgent
from kg_agent.agents.controller.prompts import PromptRepository
from kg_agent.agents.controller.state import ControllerRequest
from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRecord
from kg_agent.domain.enums import AssignmentStatus, TaskEventType, TaskIntent
from kg_agent.exceptions import SubTaskValidationError
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.database.session import Database
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.services.postgres_artifact_reuse_service import PostgresArtifactReuseService
from kg_agent.services.postgres_context_service import PostgresContextService
from tests.factories import make_artifact, make_decomposition, make_subtask


@pytest_asyncio.fixture
async def controller_database():
    """为每个总控持久化测试创建完整临时 Schema。"""

    database = Database("sqlite+aiosqlite:///:memory:")
    await database.create_schema()
    try:
        yield database
    finally:
        await database.dispose()


def _controller(project_root: Path, agent_registry, database: Database, responses):
    """使用真实持久化/复用服务和可重复模型响应组装总控 Agent。"""

    return ControllerAgent(
        model_gateway=FakeModelAdapter(responses),
        agent_registry=agent_registry,
        context_service=PostgresContextService(database.session_factory),
        prompt_repository=PromptRepository(project_root / "configs" / "prompts" / "controller"),
        artifact_reuse_evaluator=PostgresArtifactReuseService(database.session_factory),
    )


@pytest.mark.asyncio
async def test_start_atomically_creates_subtask_and_assignment(
    project_root: Path, agent_registry, controller_database: Database
) -> None:
    """合法 START 应在一次发布中落下任务、子任务、Assignment 和 SharedContext。"""

    task_id = uuid4()
    conversation_id = uuid4()
    subtask = make_subtask(task_id)
    controller = _controller(
        project_root,
        agent_registry,
        controller_database,
        [make_decomposition(task_id, subtasks=[subtask])],
    )
    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=conversation_id,
            user_message="解析制造业设备手册。",
        )
    )

    assert output.shared_context is not None
    async with controller_database.session_factory() as session:
        assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
        assert len(assignments) == 1
        assert assignments[0].status is AssignmentStatus.PENDING


@pytest.mark.asyncio
async def test_continue_reuses_persisted_artifact_and_audits_decision(
    project_root: Path, agent_registry, controller_database: Database
) -> None:
    """CONTINUE 可复用正式 Artifact，并把复用 ID 写入拆解审计事件。"""

    task_id = uuid4()
    conversation_id = uuid4()
    context_service = PostgresContextService(controller_database.session_factory)
    initial = await context_service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    artifact = make_artifact(task_id)
    async with controller_database.session_factory() as session:
        async with session.begin():
            await SqlAlchemyArtifactRepository(session).add(
                ArtifactRecord(
                    ref=artifact,
                    object_key="tasks/test/semantic.json",
                    media_type="application/json",
                    size_bytes=1,
                )
            )
    extraction = make_subtask(
        task_id,
        subtask_type="ontology_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["CandidateOntology"],
        inputs=[artifact.artifact_id],
        decomposition_revision=2,
    )
    continued = TaskDecompositionResult(
        task_intent=TaskIntent.CONTINUE,
        task_spec=initial.current_task_spec,
        subtasks=[extraction],
        reused_artifact_ids=[artifact.artifact_id],
    )
    controller = _controller(project_root, agent_registry, controller_database, [continued])
    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            current_task_id=task_id,
            conversation_id=conversation_id,
            user_message="继续从已解析文档抽取候选本体。",
            existing_artifacts=[artifact],
        )
    )

    assert output.shared_context is not None
    assert artifact.artifact_id in output.decomposition.reused_artifact_ids
    async with controller_database.session_factory() as session:
        events = await SqlAlchemyEventRepository(session).list_for_task(task_id)
    decomposition_events = [
        item for item in events if item.event_type is TaskEventType.DECOMPOSITION_PUBLISHED
    ]
    assert str(artifact.artifact_id) in decomposition_events[-1].payload["reused_artifact_ids"]


@pytest.mark.asyncio
async def test_invalid_model_plan_never_creates_master_task(
    project_root: Path, agent_registry, controller_database: Database
) -> None:
    """模型候选未通过 Registry 校验时，PostgreSQL 中不能出现部分任务事实。"""

    task_id = uuid4()
    invalid = make_decomposition(
        task_id,
        subtasks=[make_subtask(task_id, agent_type="unknown_agent")],
    )
    controller = ControllerAgent(
        model_gateway=FakeModelAdapter([invalid]),
        agent_registry=agent_registry,
        context_service=PostgresContextService(controller_database.session_factory),
        prompt_repository=PromptRepository(project_root / "configs" / "prompts" / "controller"),
        max_repairs=0,
        artifact_reuse_evaluator=PostgresArtifactReuseService(controller_database.session_factory),
    )

    with pytest.raises(SubTaskValidationError):
        await controller.run(
            ControllerRequest(
                task_id=task_id,
                conversation_id=uuid4(),
                user_message="执行未知专业任务。",
            )
        )

    async with controller_database.session_factory() as session:
        assert await SqlAlchemyTaskRepository(session).get_master(task_id) is None
