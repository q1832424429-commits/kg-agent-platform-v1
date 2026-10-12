"""验证任务发布失败时不会留下部分关系事实。"""

from uuid import uuid4

import pytest

from kg_agent.exceptions import PersistenceError
from kg_agent.services.postgres_context_service import PostgresContextService
from tests.factories import make_artifact, make_decomposition, make_subtask


@pytest.mark.asyncio
async def test_late_artifact_validation_failure_rolls_back_whole_publish(database) -> None:
    """Artifact 归属在事务后段失败时，任务、子任务和事件必须全部回滚。"""

    task_id = uuid4()
    invalid_artifact = make_artifact(uuid4())
    service = PostgresContextService(database.session_factory)

    with pytest.raises(PersistenceError):
        await service.publish_decomposition(
            conversation_id=uuid4(),
            result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
            source_artifacts=[invalid_artifact],
            expected_version=None,
        )

    assert await service.get(task_id) is None
    assert await service.list_subtasks(task_id) == []
