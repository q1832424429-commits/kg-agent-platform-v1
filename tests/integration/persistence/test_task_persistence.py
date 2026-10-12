"""验证任务 revision、事件和 SharedContext 的事务持久化。"""

from uuid import uuid4

import pytest

from kg_agent.domain.enums import TaskIntent
from kg_agent.exceptions import ContextConflictError
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.services.postgres_context_service import PostgresContextService
from tests.factories import make_decomposition, make_subtask


@pytest.mark.asyncio
async def test_modify_appends_revisions_and_preserves_history(database) -> None:
    """MODIFY 必须产生新 TaskSpec/拆解版本并保留历史子任务。"""

    task_id = uuid4()
    conversation_id = uuid4()
    service = PostgresContextService(database.session_factory)
    first_subtask = make_subtask(task_id)
    first = await service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[first_subtask]),
        source_artifacts=[],
        expected_version=None,
    )
    second_subtask = make_subtask(
        task_id,
        subtask_type="knowledge_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["EntityDataset", "RelationDataset", "PropertyDataset", "EvidenceDataset"],
        decomposition_revision=2,
    )
    second = await service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(
            task_id,
            intent=TaskIntent.MODIFY,
            revision=2,
            subtasks=[second_subtask],
        ),
        source_artifacts=[],
        expected_version=first.version,
    )

    assert second.version == 2
    assert second.current_task_spec.revision == 2
    assert set(second.subtask_ids) == {first_subtask.subtask_id, second_subtask.subtask_id}
    async with database.session_factory() as session:
        task_repository = SqlAlchemyTaskRepository(session)
        assert await task_repository.list_task_spec_revisions(task_id) == [1, 2]
        events = await SqlAlchemyEventRepository(session).list_for_task(task_id)
        assert [item.sequence for item in events] == sorted(item.sequence for item in events)
        assert len(events) >= 8


@pytest.mark.asyncio
async def test_rejects_stale_context_version(database) -> None:
    """过期 expected_version 不能覆盖较新的 SharedContext。"""

    task_id = uuid4()
    conversation_id = uuid4()
    service = PostgresContextService(database.session_factory)
    await service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )

    with pytest.raises(ContextConflictError):
        await service.publish_decomposition(
            conversation_id=conversation_id,
            result=make_decomposition(
                task_id,
                intent=TaskIntent.CONTINUE,
                subtasks=[make_subtask(task_id, decomposition_revision=2)],
            ),
            source_artifacts=[],
            expected_version=None,
        )

    current = await service.get(task_id)
    assert current is not None
    assert current.version == 1
