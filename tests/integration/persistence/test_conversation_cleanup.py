"""验证会话删除对独立和已合并长期经验采用不同清理策略。"""

from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest

from kg_agent.domain.memory_models import MemoryMessage
from kg_agent.infrastructure.database.models import ApiRequestRow, ConversationRow, MasterTaskRow
from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.memory.memory_service import LongTermMemoryService
from kg_agent.memory.stores import InMemoryLongTermMemoryStore
from kg_agent.services.conversation_service import ConversationService
from kg_agent.services.run_log_service import RunLogService


@pytest.mark.anyio
@pytest.mark.parametrize("merged_elsewhere", [False, True])
async def test_conversation_cleanup_preserves_only_shared_experience(
    tmp_path: Path, merged_elsewhere: bool
) -> None:
    """删除会话只删除其独立经验；其他任务合并过的经验必须继续可召回。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'conversation-cleanup.db'}")
    await database.create_schema()
    store = InMemoryLongTermMemoryStore()
    memory = LongTermMemoryService(store, user_id="single-user", app_id="test-app")
    task_id, conversation_id = uuid4(), uuid4()
    now = datetime.now(UTC)
    receipt = await store.add(
        user_id="single-user",
        messages=[MemoryMessage(role="assistant", content="设备故障知识图谱构建成功经验")],
        metadata={"source": "completed_task_experience", "task_id": str(task_id)},
        infer=False,
        app_id="test-app",
    )
    assert receipt.event_id is not None

    async with database.session_factory() as session, session.begin():
        session.add(
            ConversationRow(
                conversation_id=conversation_id,
                title="待删除会话",
                created_at=now,
                updated_at=now,
            )
        )
        session.add(
            MasterTaskRow(
                task_id=task_id,
                conversation_id=conversation_id,
                workspace_id="default-workspace",
                user_id="default-user",
                title="设备故障图谱",
                current_task_spec_revision=1,
                current_decomposition_revision=1,
                status="SUCCEEDED",
                created_at=now,
                updated_at=now,
                completed_at=now,
            )
        )
        session.add(
            ApiRequestRow(
                request_id=uuid4(),
                request_hash="created",
                operation="COMPLETED_TASK_MEMORY",
                status="COMPLETED",
                task_id=task_id,
                conversation_id=conversation_id,
                response_payload={
                    "action": "CREATED",
                    "receipt": {"event_id": receipt.event_id, "status": "SUCCEEDED"},
                },
                error_message=None,
                created_at=now,
                updated_at=now,
            )
        )
        if merged_elsewhere:
            session.add(
                ApiRequestRow(
                    request_id=uuid4(),
                    request_hash="merged",
                    operation="COMPLETED_TASK_MEMORY",
                    status="COMPLETED",
                    task_id=uuid4(),
                    conversation_id=uuid4(),
                    response_payload={
                        "action": "MERGED",
                        "matched_provider_memory_id": receipt.event_id,
                        "receipt": {"event_id": receipt.event_id, "status": "SUCCEEDED"},
                    },
                    error_message=None,
                    created_at=now,
                    updated_at=now,
                )
            )

    result = await ConversationService(
        database.session_factory,
        storage=InMemoryObjectStorage(),
        memory=memory,
        run_logs=RunLogService(None),
    ).delete(conversation_id, actor_user_id="default-user")
    records = await memory.search("设备故障知识图谱构建成功经验", threshold=0, top_k=10)
    await database.dispose()

    assert result.deleted_memory_count == (0 if merged_elsewhere else 1)
    assert len(records) == (1 if merged_elsewhere else 0)
