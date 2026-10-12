"""验证 Assignment 租约、fencing token、完成和过期恢复。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from kg_agent.domain.enums import AssignmentStatus, SubTaskStatus
from kg_agent.exceptions import AssignmentConflictError
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.services.assignment_service import AssignmentService
from kg_agent.services.postgres_context_service import PostgresContextService
from tests.factories import make_decomposition, make_subtask


class MutableClock:
    """允许测试显式推进时间而不执行真实等待。"""

    def __init__(self) -> None:
        """从固定 UTC 时间开始。"""

        self.current = datetime(2026, 9, 3, 10, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """返回当前测试时间。"""

        return self.current


def assignment_service(session, clock: MutableClock) -> AssignmentService:
    """使用同一个事务 Session 创建 AssignmentService。"""

    return AssignmentService(
        task_repository=SqlAlchemyTaskRepository(session),
        assignment_repository=SqlAlchemyAssignmentRepository(session),
        context_repository=SqlAlchemyContextRepository(session),
        event_repository=SqlAlchemyEventRepository(session),
        lease_seconds=60,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_claim_start_and_fencing_token(database) -> None:
    """只有匹配角色和当前 fencing token 可以启动并完成 Assignment。"""

    task_id = uuid4()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    clock = MutableClock()
    async with database.session_factory() as session, session.begin():
        claimed = await assignment_service(session, clock).claim_next(
            agent_type="document_parsing_agent", worker_id="worker-1"
        )
        assert claimed is not None
        assert claimed.status is AssignmentStatus.CLAIMED
        assert claimed.fencing_token == 1

    async with database.session_factory() as session, session.begin():
        with pytest.raises(AssignmentConflictError):
            await assignment_service(session, clock).start(
                assignment_id=claimed.assignment_id,
                worker_id="worker-1",
                fencing_token=0,
            )

    async with database.session_factory() as session, session.begin():
        running = await assignment_service(session, clock).start(
            assignment_id=claimed.assignment_id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
        )
        assert running.status is AssignmentStatus.RUNNING

    async with database.session_factory() as session, session.begin():
        completed = await assignment_service(session, clock).complete(
            assignment_id=claimed.assignment_id,
            worker_id="worker-1",
            fencing_token=claimed.fencing_token,
            succeeded=True,
        )
        assert completed.status is AssignmentStatus.SUCCEEDED
        subtask = await SqlAlchemyTaskRepository(session).get_subtask(completed.subtask_id)
        assert subtask is not None
        assert subtask.status is SubTaskStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_expired_lease_creates_new_attempt_and_rejects_old_holder(database) -> None:
    """租约过期后创建下一尝试，并阻止旧持有者继续回写。"""

    task_id = uuid4()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    clock = MutableClock()
    async with database.session_factory() as session, session.begin():
        claimed = await assignment_service(session, clock).claim_next(
            agent_type="document_parsing_agent", worker_id="worker-old"
        )
        assert claimed is not None
    clock.current += timedelta(seconds=61)

    async with database.session_factory() as session, session.begin():
        retries = await assignment_service(session, clock).recover_expired()
        assert len(retries) == 1
        assert retries[0].attempt == 2
        assert retries[0].fencing_token == claimed.fencing_token

    async with database.session_factory() as session, session.begin():
        with pytest.raises(AssignmentConflictError):
            await assignment_service(session, clock).start(
                assignment_id=claimed.assignment_id,
                worker_id="worker-old",
                fencing_token=claimed.fencing_token,
            )
        assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
        assert [item.status for item in assignments] == [
            AssignmentStatus.EXPIRED,
            AssignmentStatus.PENDING,
        ]


@pytest.mark.asyncio
async def test_reviewed_ontology_publish_unlocks_knowledge_extraction(database) -> None:
    """本体发布已成功后，审核标记不能再次阻断实例知识抽取。"""

    task_id = uuid4()
    publish = make_subtask(
        task_id,
        subtask_type="ontology_publish",
        agent_type="graph_management_agent",
        outputs=["PublishedOntology"],
        human_review_required=True,
    )
    knowledge = make_subtask(
        task_id,
        subtask_type="knowledge_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["EntityDataset"],
        dependencies=[publish.subtask_id],
    )
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[publish, knowledge]),
        source_artifacts=[],
        expected_version=None,
    )
    clock = MutableClock()
    async with database.session_factory() as session, session.begin():
        service = assignment_service(session, clock)
        claimed = await service.claim_next(
            agent_type="graph_management_agent", worker_id="publish-worker"
        )
        assert claimed is not None
    async with database.session_factory() as session, session.begin():
        await assignment_service(session, clock).start(
            assignment_id=claimed.assignment_id,
            worker_id="publish-worker",
            fencing_token=claimed.fencing_token,
        )
    async with database.session_factory() as session, session.begin():
        await assignment_service(session, clock).complete(
            assignment_id=claimed.assignment_id,
            worker_id="publish-worker",
            fencing_token=claimed.fencing_token,
            succeeded=True,
        )
        unlocked = await SqlAlchemyTaskRepository(session).get_subtask(knowledge.subtask_id)
        assert unlocked is not None
        assert unlocked.status is SubTaskStatus.ASSIGNED
        pending = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
        assert any(
            item.subtask_id == knowledge.subtask_id
            and item.status is AssignmentStatus.PENDING
            for item in pending
        )
