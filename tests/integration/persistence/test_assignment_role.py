"""验证 Worker 只能认领匹配专业 Agent 类型的 Assignment。"""

from uuid import uuid4

import pytest

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
async def test_wrong_agent_type_cannot_claim_assignment(database) -> None:
    """知识抽取 Worker 不能认领数据源解析 Agent 的 Assignment。"""

    task_id = uuid4()
    await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
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
            agent_type="knowledge_extraction_agent", worker_id="extractor-1"
        )
        assert claimed is None
