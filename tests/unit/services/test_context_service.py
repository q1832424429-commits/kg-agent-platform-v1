"""验证 SharedContext 发布、记忆引用和最近五轮对话投影。"""

from datetime import timedelta
from uuid import uuid4

import pytest

from kg_agent.domain.enums import TaskIntent
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.exceptions import ContextConflictError
from kg_agent.services.context_service import InMemoryContextService
from kg_agent.services.context_view_service import ContextViewService
from kg_agent.services.task_orchestration_service import TaskOrchestrationService
from tests.factories import NOW, make_artifact, make_decomposition, make_subtask


@pytest.mark.asyncio
async def test_publish_start_creates_task_scoped_context() -> None:
    """START 发布后应创建独立上下文和子任务引用。"""

    task_id = uuid4()
    conversation_id = uuid4()
    subtask = make_subtask(task_id)
    result = make_decomposition(task_id, subtasks=[subtask])
    service = InMemoryContextService()

    context = await service.publish_decomposition(
        conversation_id=conversation_id,
        result=result,
        source_artifacts=[],
        expected_version=None,
    )

    assert context.task_id == task_id
    assert context.version == 1
    assert context.subtask_ids == [subtask.subtask_id]
    assert (await service.list_subtasks(task_id))[0] == subtask


@pytest.mark.asyncio
async def test_publish_adds_recalled_memory_without_storing_provider_payload() -> None:
    """召回结果只以摘要和来源引用进入 SharedContext。"""

    task_id = uuid4()
    memory = MemoryRef(
        memory_id=uuid4(),
        memory_type="LONG_TERM",
        summary="用户偏好使用制造业设备本体",
        source_refs=["mem0://memory/example"],
    )
    service = InMemoryContextService()

    context = await service.publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
        recalled_long_term_memories=[memory],
    )

    assert context.long_term_memory_refs == [memory]


@pytest.mark.asyncio
async def test_modify_preserves_artifact_and_revision_history() -> None:
    """MODIFY 追加 revision 时不能删除历史 ArtifactRef。"""

    task_id = uuid4()
    conversation_id = uuid4()
    artifact = make_artifact(task_id)
    service = InMemoryContextService()
    initial = await service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[artifact],
        expected_version=None,
    )
    modified_subtask = make_subtask(task_id, decomposition_revision=2)
    modified = make_decomposition(
        task_id,
        intent=TaskIntent.MODIFY,
        revision=2,
        subtasks=[modified_subtask],
    )

    updated = await service.publish_decomposition(
        conversation_id=conversation_id,
        result=modified,
        source_artifacts=[],
        expected_version=initial.version,
    )

    assert updated.version == 2
    assert updated.task_spec_revision_refs == [1, 2]
    assert updated.artifact_refs == [artifact]
    assert set(updated.subtask_ids) == {
        initial.subtask_ids[0],
        modified_subtask.subtask_id,
    }


@pytest.mark.asyncio
async def test_context_rejects_stale_expected_version() -> None:
    """旧版本调用方不能覆盖已经更新的 SharedContext。"""

    task_id = uuid4()
    conversation_id = uuid4()
    service = InMemoryContextService()
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
                intent=TaskIntent.MODIFY,
                revision=2,
                subtasks=[make_subtask(task_id, decomposition_revision=2)],
            ),
            source_artifacts=[],
            expected_version=0,
        )


@pytest.mark.asyncio
async def test_short_term_context_keeps_five_complete_conversation_rounds() -> None:
    """六轮对话后 SharedContext 和 Agent View 都应只保留最后五轮十条消息。"""

    task_id = uuid4()
    conversation_id = uuid4()
    service = InMemoryContextService()
    context = await service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    task_note = MemoryRef(
        memory_id=uuid4(),
        memory_type="SHORT_TERM",
        summary="不占用对话额度的任务状态",
        source_refs=["agent-run:test"],
    )
    context = context.model_copy(update={"short_term_memories": [task_note]})

    for index in range(6):
        context, _, _ = TaskOrchestrationService._conversation_round(
            current=context,
            conversation_id=conversation_id,
            user_message=f"用户消息 {index}",
            assistant_message=f"Agent 回复 {index}",
            created_at=NOW + timedelta(seconds=index),
        )

    conversation = ContextViewService._conversation_memories(context.short_term_memories)

    assert len(conversation) == 10
    assert conversation[0].summary == "用户消息 1"
    assert conversation[-1].summary == "Agent 回复 5"
    assert [item.source_refs[1] for item in conversation] == [
        role for _ in range(5) for role in ("conversation-role:user", "conversation-role:assistant")
    ]
    assert task_note in context.short_term_memories


def test_agent_view_keeps_three_recalled_long_term_memories() -> None:
    """运行经验引用不能挤占 Mem0 召回的三条长期记忆额度。"""

    memories = [
        MemoryRef(
            memory_id=uuid4(),
            memory_type="LONG_TERM",
            summary=f"长期记忆 {index}",
            source_refs=[f"mem0://memory/{index}"],
        )
        for index in range(4)
    ]
    experience = MemoryRef(
        memory_id=uuid4(),
        memory_type="EXPERIENCE",
        summary="当前任务运行经验",
        source_refs=["agent-run:test"],
    )

    selected = ContextViewService._long_term_memories([*memories, experience])

    assert [item.summary for item in selected] == [
        "长期记忆 1",
        "长期记忆 2",
        "长期记忆 3",
    ]
