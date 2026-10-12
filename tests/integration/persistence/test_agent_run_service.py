"""验证专业 Agent 结果的持久化、安全提交和状态语义。"""

from datetime import timedelta
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.enums import (
    AgentRunStatus,
    AssignmentStatus,
    MasterTaskStatus,
    SubTaskStatus,
)
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAgentRunRepository,
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.database.session import Database
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.postgres_context_service import PostgresContextService
from kg_agent.services.postgres_worker_services import PostgresWorkerServices
from tests.factories import NOW, make_decomposition, make_subtask


def make_worker_services(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> PostgresWorkerServices:
    """使用真实 Registry 和短事务服务组装测试 Worker 入口。"""

    artifacts = ArtifactRegistry.v1()
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )
    return PostgresWorkerServices(
        database.session_factory,
        agents=agent_registry,
        skills=skills,
    )


async def publish_and_start(
    *,
    database: Database,
    services: PostgresWorkerServices,
    worker_id: str = "worker-1",
) -> tuple[UUID, AgentAssignment]:
    """发布无输入解析子任务，并认领、启动其第一次 Assignment。"""

    task_id = uuid4()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    claimed = await services.claim("document_parsing_agent", worker_id)
    assert claimed is not None
    running = await services.start(claimed, worker_id)
    return task_id, running


def make_result(
    *,
    task_id: UUID,
    assignment: AgentAssignment,
    status: AgentRunStatus,
) -> AgentRunResult:
    """构造不依赖业务输出的失败、重试或等待用户结果。"""

    needs_user = status is AgentRunStatus.NEEDS_USER_INPUT
    return AgentRunResult(
        task_id=task_id,
        subtask_id=assignment.subtask_id,
        assignment_id=assignment.assignment_id,
        agent_name=assignment.assigned_agent_type,
        run_id=uuid4(),
        status=status,
        retryable=status is AgentRunStatus.RETRYABLE_FAILED,
        requires_user_input=needs_user,
        user_question="请补充打开文档所需密码。" if needs_user else None,
        blocking_issues=["测试状态分支"],
        execution_trace_id=uuid4(),
        started_at=NOW,
        finished_at=NOW + timedelta(seconds=1),
    )


@pytest.mark.asyncio
async def test_retryable_result_creates_next_assignment_and_trace(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """可重试失败应保存结果和轨迹，并递增 Assignment attempt。"""

    services = make_worker_services(database, project_root, agent_registry)
    task_id, running = await publish_and_start(database=database, services=services)
    result = make_result(
        task_id=task_id,
        assignment=running,
        status=AgentRunStatus.RETRYABLE_FAILED,
    )

    await services.submit(result, "worker-1", running.fencing_token)

    async with database.session_factory() as session:
        assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
        subtask = await SqlAlchemyTaskRepository(session).get_subtask(running.subtask_id)
        stored = await SqlAlchemyAgentRunRepository(session).get_result(result.run_id)
        trace = await SqlAlchemyAgentRunRepository(session).get_trace(result.execution_trace_id)
        context = await SqlAlchemyContextRepository(session).get(task_id)

    assert [item.status for item in assignments] == [
        AssignmentStatus.FAILED,
        AssignmentStatus.PENDING,
    ]
    assert assignments[-1].attempt == 2
    assert subtask is not None and subtask.status is SubTaskStatus.ASSIGNED
    assert stored == result
    assert trace is not None and trace.status == AgentRunStatus.RETRYABLE_FAILED.value
    assert context is not None
    assert result.run_id in context.agent_run_result_ids
    assert result.execution_trace_id in context.execution_trace_ids


@pytest.mark.asyncio
async def test_needs_user_input_moves_master_task_to_waiting_user(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """专业 Agent 请求信息时，当前尝试结束且总任务进入等待用户状态。"""

    services = make_worker_services(database, project_root, agent_registry)
    task_id, running = await publish_and_start(database=database, services=services)
    result = make_result(
        task_id=task_id,
        assignment=running,
        status=AgentRunStatus.NEEDS_USER_INPUT,
    )

    await services.submit(result, "worker-1", running.fencing_token)

    async with database.session_factory() as session:
        assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
        tasks = SqlAlchemyTaskRepository(session)
        subtask = await tasks.get_subtask(running.subtask_id)
        master = await tasks.get_master(task_id)

    assert assignments[-1].status is AssignmentStatus.FAILED
    assert subtask is not None and subtask.status is SubTaskStatus.FAILED
    assert master is not None and master.status is MasterTaskStatus.WAITING_USER


@pytest.mark.asyncio
async def test_result_cannot_reference_another_running_assignment(
    database: Database,
    project_root: Path,
    agent_registry: AgentRegistry,
) -> None:
    """即使角色相同，也不能把一个子任务结果回写到另一条 Assignment。"""

    services = make_worker_services(database, project_root, agent_registry)
    first_task_id, first = await publish_and_start(database=database, services=services)
    _, second = await publish_and_start(
        database=database,
        services=services,
        worker_id="worker-2",
    )
    forged = make_result(
        task_id=first_task_id,
        assignment=first,
        status=AgentRunStatus.FAILED,
    ).model_copy(update={"assignment_id": second.assignment_id})

    from kg_agent.exceptions import AssignmentConflictError

    with pytest.raises(AssignmentConflictError):
        await services.submit(forged, "worker-2", second.fencing_token)

    async with database.session_factory() as session:
        assert await SqlAlchemyAgentRunRepository(session).get_result(forged.run_id) is None
