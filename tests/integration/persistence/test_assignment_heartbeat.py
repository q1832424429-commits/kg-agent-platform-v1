"""验证 Assignment 心跳只接受当前租约持有者和令牌。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

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


class HeartbeatClock:
    """为心跳测试提供可推进的 UTC 时间。"""

    def __init__(self) -> None:
        """设置固定起点。"""

        self.now = datetime(2026, 9, 3, 11, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        """返回当前时间。"""

        return self.now


def service(session, clock: HeartbeatClock) -> AssignmentService:
    """构建共享测试时钟的 AssignmentService。"""

    return AssignmentService(
        task_repository=SqlAlchemyTaskRepository(session),
        assignment_repository=SqlAlchemyAssignmentRepository(session),
        context_repository=SqlAlchemyContextRepository(session),
        event_repository=SqlAlchemyEventRepository(session),
        lease_seconds=60,
        clock=clock,
    )


@pytest.mark.asyncio
async def test_heartbeat_extends_lease_and_rejects_other_worker(database) -> None:
    """有效心跳延长租约，其他 Worker 即使知道 ID 也不能续租。"""

    task_id = uuid4()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    clock = HeartbeatClock()
    async with database.session_factory() as session, session.begin():
        claimed = await service(session, clock).claim_next(
            agent_type="document_parsing_agent", worker_id="worker-owner"
        )
        assert claimed is not None
    original_expiry = claimed.lease_expires_at
    clock.now += timedelta(seconds=10)

    async with database.session_factory() as session, session.begin():
        renewed = await service(session, clock).heartbeat(
            assignment_id=claimed.assignment_id,
            worker_id="worker-owner",
            fencing_token=claimed.fencing_token,
        )
        assert renewed.lease_expires_at is not None
        assert original_expiry is not None
        assert renewed.lease_expires_at > original_expiry

    async with database.session_factory() as session, session.begin():
        with pytest.raises(AssignmentConflictError):
            await service(session, clock).heartbeat(
                assignment_id=claimed.assignment_id,
                worker_id="worker-other",
                fencing_token=claimed.fencing_token,
            )
