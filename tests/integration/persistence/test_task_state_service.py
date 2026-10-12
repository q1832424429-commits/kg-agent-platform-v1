"""验证 TaskStateService 的 MasterTask 和手动解锁状态迁移。"""

from uuid import uuid4

import pytest

from kg_agent.domain.enums import AssignmentStatus, MasterTaskStatus, SubTaskStatus
from kg_agent.exceptions import StateTransitionError
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.services.postgres_context_service import PostgresContextService
from kg_agent.services.task_state_service import TaskStateService
from tests.factories import make_decomposition, make_subtask


def make_service(session) -> TaskStateService:
    """用同一事务的 Repository 创建 TaskStateService。"""

    return TaskStateService(
        task_repository=SqlAlchemyTaskRepository(session),
        assignment_repository=SqlAlchemyAssignmentRepository(session),
        context_repository=SqlAlchemyContextRepository(session),
        event_repository=SqlAlchemyEventRepository(session),
    )


@pytest.mark.asyncio
async def test_master_task_transition_updates_snapshot(database) -> None:
    """MasterTask 等待用户状态必须通过服务迁移并产生新快照。"""

    task_id = uuid4()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    async with database.session_factory() as session, session.begin():
        updated = await make_service(session).transition_master_task(
            task_id=task_id,
            target=MasterTaskStatus.WAITING_USER,
            actor_id="controller_agent",
        )
        assert updated.status is MasterTaskStatus.WAITING_USER

    context = await PostgresContextService(database.session_factory).get(task_id)
    assert context is not None
    assert context.version == 2
    assert context.master_task.status is MasterTaskStatus.WAITING_USER


@pytest.mark.asyncio
async def test_terminal_master_task_rejects_reactivation(database) -> None:
    """终态 MasterTask 不能重新变为 ACTIVE。"""

    task_id = uuid4()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    async with database.session_factory() as session, session.begin():
        await make_service(session).transition_master_task(
            task_id=task_id,
            target=MasterTaskStatus.CANCELLED,
            actor_id="user",
        )
    async with database.session_factory() as session, session.begin():
        with pytest.raises(StateTransitionError):
            await make_service(session).transition_master_task(
                task_id=task_id,
                target=MasterTaskStatus.ACTIVE,
                actor_id="controller_agent",
            )


@pytest.mark.asyncio
async def test_manual_ready_transition_creates_assignment(database) -> None:
    """人工审核恢复等场景进入 READY 后必须立即创建可认领 Assignment。"""

    task_id = uuid4()
    subtask = make_subtask(task_id)
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[subtask]),
        source_artifacts=[],
        expected_version=None,
    )
    async with database.session_factory() as session, session.begin():
        tasks = SqlAlchemyTaskRepository(session)
        assignments = SqlAlchemyAssignmentRepository(session)
        stored = await tasks.get_subtask(subtask.subtask_id)
        assert stored is not None
        # 构造第二阶段可验证的 WAITING_HUMAN 恢复场景；审核决策本身在第六阶段实现。
        await tasks.update_subtask_status(
            subtask.subtask_id, SubTaskStatus.WAITING_HUMAN, stored.created_at
        )
        current_assignments = await assignments.list_for_task(task_id)
        for assignment in current_assignments:
            cancelled = assignment.model_copy(update={"status": AssignmentStatus.CANCELLED})
            await assignments.update(cancelled)

    async with database.session_factory() as session, session.begin():
        resumed = await make_service(session).transition_subtask(
            subtask_id=subtask.subtask_id,
            target=SubTaskStatus.READY,
            actor_id="human_review_service",
        )
        assert resumed.status is SubTaskStatus.ASSIGNED
        assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
        assert len(assignments) == 2
