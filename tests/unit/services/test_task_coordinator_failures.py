"""验证协调器保留当前计划中的不可重试 Worker 原始失败。"""

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import AgentRunStatus, MasterTaskStatus, SubTaskStatus, TaskIntent
from kg_agent.domain.memory_models import ConversationContextRef
from kg_agent.domain.task_models import AgentSubTask, MasterTask
from kg_agent.services.task_coordinator import TaskCoordinator
from tests.factories import make_subtask, make_task_spec

NOW = datetime(2026, 9, 17, 14, 0, tzinfo=UTC)


class _SessionContext:
    """提供无需真实数据库的协调器只读 Session 上下文。"""

    async def __aenter__(self) -> object:
        """返回仓储构造器可接收的占位 Session。"""

        return object()

    async def __aexit__(self, *args: object) -> None:
        """结束只读测试上下文。"""


@pytest.mark.asyncio
async def test_non_retryable_current_worker_failure_skips_final_controller(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """当前 revision 已不可重试失败时应暂停，不能再调用总控掩盖原始错误。"""

    task_id, conversation_id = uuid4(), uuid4()
    subtask = make_subtask(task_id, subtask_type="knowledge_extract").model_copy(
        update={"status": SubTaskStatus.FAILED}
    )
    master = MasterTask(
        task_id=task_id,
        conversation_id=conversation_id,
        title="测试不可重试失败",
        current_task_spec_revision=1,
        current_decomposition_revision=1,
        status=MasterTaskStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    task_spec = make_task_spec(task_id, TaskIntent.START).model_copy(
        update={"target_output_types": ["CandidateGraph"]}
    )
    context = SharedTaskContext(
        task_id=task_id,
        version=1,
        conversation=ConversationContextRef(conversation_id=conversation_id),
        master_task=master,
        current_task_spec=task_spec,
        updated_at=NOW,
    )
    failed_run = AgentRunResult(
        task_id=task_id,
        subtask_id=subtask.subtask_id,
        assignment_id=uuid4(),
        agent_name="knowledge_extraction_agent",
        run_id=uuid4(),
        status=AgentRunStatus.FAILED,
        blocking_issues=["ValueError: EvidenceDataset 不完整"],
        retryable=False,
        execution_trace_id=uuid4(),
        started_at=NOW,
        finished_at=NOW,
    )

    class _TaskRepository:
        """返回当前 MasterTask 与唯一失败子任务。"""

        def __init__(self, session: object) -> None:
            del session

        async def get_master(self, requested_task_id: UUID) -> MasterTask:
            assert requested_task_id == task_id
            return master

        async def list_subtasks(self, requested_task_id: UUID) -> list[AgentSubTask]:
            assert requested_task_id == task_id
            return [subtask]

    class _ContextRepository:
        """返回当前 SharedContext。"""

        def __init__(self, session: object) -> None:
            del session

        async def get(self, requested_task_id: UUID) -> SharedTaskContext:
            assert requested_task_id == task_id
            return context

    class _EmptyRepository:
        """为 Assignment 与 Artifact 查询返回空集合。"""

        def __init__(self, session: object) -> None:
            del session

        async def list_for_task(self, requested_task_id: UUID) -> list[Any]:
            assert requested_task_id == task_id
            return []

    class _RunRepository(_EmptyRepository):
        """返回当前不可重试失败结果。"""

        async def list_results(self, requested_task_id: UUID) -> list[AgentRunResult]:
            assert requested_task_id == task_id
            return [failed_run]

    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyTaskRepository", _TaskRepository
    )
    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyContextRepository", _ContextRepository
    )
    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyAssignmentRepository", _EmptyRepository
    )
    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyArtifactRepository", _EmptyRepository
    )
    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyAgentRunRepository", _RunRepository
    )

    orchestration = SimpleNamespace(submit_and_run=AsyncMock())
    reviews = SimpleNamespace(list_reviews=AsyncMock(return_value=[]))
    runtime = cast(
        Any,
        SimpleNamespace(
            database=SimpleNamespace(session_factory=lambda: _SessionContext()),
            reviews=reviews,
            orchestration=orchestration,
        ),
    )
    coordinator = TaskCoordinator(runtime)
    coordinator._create_required_review = AsyncMock(return_value=False)  # type: ignore[method-assign]
    coordinator._pause_for_guard = AsyncMock()  # type: ignore[method-assign]

    action = await coordinator.advance(task_id)

    assert action == "WAITING_USER"
    coordinator._pause_for_guard.assert_awaited_once_with(
        task_id, "NON_RETRYABLE_AGENT_FAILURE"
    )
    orchestration.submit_and_run.assert_not_awaited()


@pytest.mark.asyncio
async def test_successful_task_triggers_one_completed_experience_write(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """目标成功收口后应触发整轮经验，不能等待或依赖单个 Worker 写入。"""

    task_id, conversation_id = uuid4(), uuid4()
    master = MasterTask(
        task_id=task_id,
        conversation_id=conversation_id,
        title="测试整轮经验",
        current_task_spec_revision=1,
        current_decomposition_revision=1,
        status=MasterTaskStatus.ACTIVE,
        created_at=NOW,
        updated_at=NOW,
    )
    context = SharedTaskContext(
        task_id=task_id,
        version=1,
        conversation=ConversationContextRef(conversation_id=conversation_id),
        master_task=master,
        current_task_spec=make_task_spec(task_id, TaskIntent.START).model_copy(
            update={"target_output_types": []}
        ),
        updated_at=NOW,
    )

    class _TaskRepository:
        """返回可直接成功收口的空拆解任务。"""

        def __init__(self, session: object) -> None:
            del session

        async def get_master(self, requested_task_id: UUID) -> MasterTask:
            assert requested_task_id == task_id
            return master

        async def list_subtasks(self, requested_task_id: UUID) -> list[AgentSubTask]:
            assert requested_task_id == task_id
            return []

    class _ContextRepository:
        """返回当前任务上下文。"""

        def __init__(self, session: object) -> None:
            del session

        async def get(self, requested_task_id: UUID) -> SharedTaskContext:
            assert requested_task_id == task_id
            return context

    class _EmptyRepository:
        """返回空状态集合。"""

        def __init__(self, session: object) -> None:
            del session

        async def list_for_task(self, requested_task_id: UUID) -> list[Any]:
            assert requested_task_id == task_id
            return []

        async def list_results(self, requested_task_id: UUID) -> list[AgentRunResult]:
            assert requested_task_id == task_id
            return []

    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyTaskRepository", _TaskRepository
    )
    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyContextRepository", _ContextRepository
    )
    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyAssignmentRepository", _EmptyRepository
    )
    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyArtifactRepository", _EmptyRepository
    )
    monkeypatch.setattr(
        "kg_agent.services.task_coordinator.SqlAlchemyAgentRunRepository", _EmptyRepository
    )

    remember = AsyncMock()
    orchestration = SimpleNamespace(
        submit_and_run=AsyncMock(),
        remember_completed_task_experience=remember,
    )
    runtime = cast(
        Any,
        SimpleNamespace(
            database=SimpleNamespace(session_factory=lambda: _SessionContext()),
            reviews=SimpleNamespace(list_reviews=AsyncMock(return_value=[])),
            orchestration=orchestration,
        ),
    )
    coordinator = TaskCoordinator(runtime)
    coordinator._complete = AsyncMock(return_value=True)  # type: ignore[method-assign]

    action = await coordinator.advance(task_id)

    assert action == "TASK_COMPLETED"
    remember.assert_awaited_once_with(task_id)
