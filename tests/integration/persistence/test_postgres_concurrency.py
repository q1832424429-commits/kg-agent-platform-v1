"""在显式测试 PostgreSQL 上验证 SKIP LOCKED 原子认领语义。"""

import asyncio
import os
from uuid import uuid4

import pytest

from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.database.session import Database
from kg_agent.services.assignment_service import AssignmentService
from kg_agent.services.postgres_context_service import PostgresContextService
from tests.factories import make_decomposition, make_subtask


@pytest.mark.asyncio
async def test_two_workers_cannot_claim_same_assignment() -> None:
    """两个独立事务并发认领时必须只有一个 Worker 获得 Assignment。"""

    database_url = os.getenv("KG_AGENT_TEST_DATABASE_URL")
    if not database_url:
        pytest.skip("未配置 KG_AGENT_TEST_DATABASE_URL，跳过 PostgreSQL 锁语义测试")
    if "postgresql+asyncpg" not in database_url or "test" not in database_url.lower():
        pytest.fail("KG_AGENT_TEST_DATABASE_URL 必须指向名称包含 test 的 PostgreSQL 测试库")

    database = Database(database_url)
    await database.create_schema()
    try:
        task_id = uuid4()
        await PostgresContextService(database.session_factory).publish_decomposition(
            conversation_id=uuid4(),
            result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
            source_artifacts=[],
            expected_version=None,
        )

        async def claim(worker_id: str):
            async with database.session_factory() as session, session.begin():
                service = AssignmentService(
                    task_repository=SqlAlchemyTaskRepository(session),
                    assignment_repository=SqlAlchemyAssignmentRepository(session),
                    context_repository=SqlAlchemyContextRepository(session),
                    event_repository=SqlAlchemyEventRepository(session),
                )
                return await service.claim_next(
                    agent_type="document_parsing_agent", worker_id=worker_id
                )

        results = await asyncio.gather(claim("worker-a"), claim("worker-b"))
        assert sum(item is not None for item in results) == 1
    finally:
        await database.drop_schema()
        await database.dispose()
