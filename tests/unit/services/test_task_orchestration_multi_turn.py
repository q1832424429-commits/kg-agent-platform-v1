"""验证正式与测试端共用的多轮消息持久化和总控历史注入。"""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.enums import TaskIntent
from kg_agent.domain.task_models import OntologyStrategy, TaskSpec
from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.services.task_orchestration_service import TaskOrchestrationService
from tests.factories import NOW


@pytest.mark.asyncio
async def test_second_turn_receives_persisted_first_user_message(project_root: Path) -> None:
    """同一 task_id 第二轮应读取首轮公开消息，并保持 USER_TURN 语义。"""

    task_id, conversation_id = uuid4(), uuid4()
    task_spec = TaskSpec(
        task_id=task_id,
        revision=1,
        task_intent=TaskIntent.START,
        business_goal="记录设备资料任务",
        source_artifact_ids=[],
        target_output_types=[],
        ontology_strategy=OntologyStrategy(mode="BUILD_FROM_DOCUMENTS"),
        created_at=NOW,
    )
    model = FakeModelAdapter(
        [
            TaskDecompositionResult(
                task_intent=TaskIntent.START,
                task_spec=task_spec,
                subtasks=[],
            ),
            TaskDecompositionResult(
                task_intent=TaskIntent.CONTINUE,
                task_spec=task_spec,
                subtasks=[],
            ),
        ]
    )
    database = Database("sqlite+aiosqlite:///:memory:")
    service = TaskOrchestrationService(
        database=database,
        storage=InMemoryObjectStorage(),
        model_gateway=model,
        project_root=project_root,
    )
    await asyncio.wait_for(database.create_schema(), timeout=5)
    try:
        await asyncio.wait_for(
            service.submit_and_run(
                question="先记录设备资料任务。",
                files=[],
                task_id=task_id,
                conversation_id=conversation_id,
                request_id=uuid4(),
            ),
            timeout=5,
        )
        second = await asyncio.wait_for(
            service.submit_and_run(
                question="继续当前任务。",
                files=[],
                task_id=task_id,
                conversation_id=conversation_id,
                request_id=uuid4(),
            ),
            timeout=5,
        )
        history = await asyncio.wait_for(service._history(conversation_id), timeout=5)
    finally:
        await asyncio.wait_for(database.dispose(), timeout=5)

    assert second.controller_result.decomposition.task_intent is TaskIntent.CONTINUE
    assert "先记录设备资料任务。" in model.calls[1].user_prompt
    assert "已识别任务意图为 START" in model.calls[1].user_prompt
    assert '"operation": "USER_TURN"' in model.calls[1].user_prompt
    assert [item.role for item in history] == ["USER", "ASSISTANT", "USER", "ASSISTANT"]
