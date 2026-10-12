"""验证上游成功后由确定性服务解锁下游 Assignment。"""

from uuid import uuid4

import pytest

from kg_agent.domain.enums import AssignmentStatus, SubTaskStatus
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.services.assignment_service import AssignmentService
from kg_agent.services.postgres_context_service import PostgresContextService
from tests.factories import make_decomposition, make_subtask


@pytest.mark.asyncio
async def test_success_unlocks_blocked_dependent(database) -> None:
    """上游完成后应创建下游首次 Assignment，不依赖 LLM 主动推进状态。"""

    task_id = uuid4()
    first = make_subtask(task_id)
    second = make_subtask(
        task_id,
        subtask_type="knowledge_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["EntityDataset", "RelationDataset", "PropertyDataset", "EvidenceDataset"],
        dependencies=[first.subtask_id],
    )
    context = await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[first, second]),
        source_artifacts=[],
        expected_version=None,
    )
    assert len(context.assignment_ids) == 1

    async with database.session_factory() as session, session.begin():
        service = AssignmentService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
        )
        claimed = await service.claim_next(
            agent_type="document_parsing_agent", worker_id="parser-1"
        )
        assert claimed is not None

    async with database.session_factory() as session, session.begin():
        service = AssignmentService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
        )
        running = await service.start(
            assignment_id=claimed.assignment_id,
            worker_id="parser-1",
            fencing_token=claimed.fencing_token,
        )

    async with database.session_factory() as session, session.begin():
        service = AssignmentService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
        )
        await service.complete(
            assignment_id=running.assignment_id,
            worker_id="parser-1",
            fencing_token=running.fencing_token,
            succeeded=True,
        )
        subtasks = await SqlAlchemyTaskRepository(session).list_subtasks(task_id)
        assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)

    statuses = {item.subtask_id: item.status for item in subtasks}
    assert statuses[first.subtask_id] is SubTaskStatus.SUCCEEDED
    assert statuses[second.subtask_id] is SubTaskStatus.ASSIGNED
    assert len(assignments) == 2
    assert assignments[1].status is AssignmentStatus.PENDING
    assert assignments[1].assigned_agent_type == "knowledge_extraction_agent"


@pytest.mark.asyncio
async def test_final_failure_cancels_blocked_descendants(database) -> None:
    """上游最终失败后，完整计划的后代应取消并交给总控做最终检查。"""

    task_id = uuid4()
    first = make_subtask(task_id)
    second = make_subtask(task_id, dependencies=[first.subtask_id])
    third = make_subtask(task_id, dependencies=[second.subtask_id])
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[first, second, third]),
        source_artifacts=[],
        expected_version=None,
    )

    async with database.session_factory() as session, session.begin():
        service = AssignmentService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
        )
        claimed = await service.claim_next(
            agent_type="document_parsing_agent", worker_id="parser-failed"
        )
        assert claimed is not None
        running = await service.start(
            assignment_id=claimed.assignment_id,
            worker_id="parser-failed",
            fencing_token=claimed.fencing_token,
        )
        await service.complete(
            assignment_id=running.assignment_id,
            worker_id="parser-failed",
            fencing_token=running.fencing_token,
            succeeded=False,
        )
        subtasks = await SqlAlchemyTaskRepository(session).list_subtasks(task_id)
        assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)

    statuses = {item.subtask_id: item.status for item in subtasks}
    assert statuses[first.subtask_id] is SubTaskStatus.FAILED
    assert statuses[second.subtask_id] is SubTaskStatus.CANCELLED
    assert statuses[third.subtask_id] is SubTaskStatus.CANCELLED
    assert len(assignments) == 1


@pytest.mark.asyncio
async def test_review_gate_does_not_unlock_publish_before_approval(database) -> None:
    """带审核标记的质量任务成功后，发布任务仍必须保持 BLOCKED。"""

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
        service = AssignmentService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
        )
        claimed = await service.claim_next(
            agent_type="quality_evaluation_agent", worker_id="quality-1"
        )
        assert claimed is not None
        running = await service.start(
            assignment_id=claimed.assignment_id,
            worker_id="quality-1",
            fencing_token=claimed.fencing_token,
        )
        await service.complete(
            assignment_id=running.assignment_id,
            worker_id="quality-1",
            fencing_token=running.fencing_token,
            succeeded=True,
        )
        stored_publish = await SqlAlchemyTaskRepository(session).get_subtask(
            publish.subtask_id
        )
        assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)

    assert stored_publish is not None
    assert stored_publish.status is SubTaskStatus.BLOCKED
    assert len(assignments) == 1
