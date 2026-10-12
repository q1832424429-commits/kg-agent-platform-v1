"""验证候选计划在正式发布前只进入隔离暂存区。"""

from typing import Any
from uuid import UUID, uuid4

import pytest

from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.services.demo_service import UploadedFile
from simulation_lab.app import _dependency_order_violations
from simulation_lab.plan_draft_store import SimulationPlanDraftStore
from tests.factories import make_decomposition, make_subtask


class _MemoryRunLogs:
    """避免单元测试依赖线程文件 IO 的最小追加日志替身。"""

    def __init__(self) -> None:
        self.records: dict[UUID, list[dict[str, Any]]] = {}

    async def append(
        self,
        *,
        task_id: UUID,
        record_type: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        self.records.setdefault(task_id, []).append(
            {"record_type": record_type, "actor": actor, "payload": payload}
        )

    async def read_after(
        self, task_id: UUID, *, after: int = 0
    ) -> tuple[list[tuple[int, dict[str, Any]]], int]:
        records = self.records.get(task_id, [])
        return list(enumerate(records[after:], start=after + 1)), len(records)


@pytest.mark.asyncio
async def test_plan_draft_round_trip_and_publish_marker() -> None:
    """草稿应可跨请求恢复，标记发布后不再被当作待审核计划。"""

    task_id = uuid4()
    conversation_id = uuid4()
    request_id = uuid4()
    decomposition = make_decomposition(
        task_id,
        subtasks=[make_subtask(task_id)],
    )
    store = SimulationPlanDraftStore(
        InMemoryObjectStorage("simulation-plan-tests"),
        _MemoryRunLogs(),  # type: ignore[arg-type]
    )

    await store.save(
        task_id=task_id,
        conversation_id=conversation_id,
        request_id=request_id,
        question="解析上传文档并抽取实体",
        decomposition=decomposition,
        files=[
            UploadedFile(
                filename="设备.docx",
                media_type=(
                    "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
                ),
                content=b"real-document-bytes",
            )
        ],
    )

    loaded = await store.load_current(task_id)
    assert loaded is not None
    assert loaded.stored.decomposition == decomposition
    assert loaded.files[0].content == b"real-document-bytes"

    await store.mark(task_id, request_id, "PUBLISHED")
    assert await store.load_current(task_id) is None


@pytest.mark.asyncio
async def test_latest_unsuperseded_plan_is_returned() -> None:
    """用户要求重新规划后，应只恢复最新候选 revision。"""

    task_id = uuid4()
    conversation_id = uuid4()
    first_request = uuid4()
    second_request = uuid4()
    store = SimulationPlanDraftStore(
        InMemoryObjectStorage("simulation-plan-tests"),
        _MemoryRunLogs(),  # type: ignore[arg-type]
    )
    first = make_decomposition(task_id, subtasks=[make_subtask(task_id)])
    second = make_decomposition(
        task_id,
        revision=2,
        subtasks=[make_subtask(task_id, decomposition_revision=2, subtask_type="entity_extract")],
    )
    await store.save(
        task_id=task_id,
        conversation_id=conversation_id,
        request_id=first_request,
        question="原始任务",
        decomposition=first,
        files=[],
    )
    await store.save(
        task_id=task_id,
        conversation_id=conversation_id,
        request_id=second_request,
        question="修改后的任务",
        decomposition=second,
        files=[],
    )
    await store.mark(task_id, first_request, "SUPERSEDED")

    loaded = await store.load_current(task_id)
    assert loaded is not None
    assert loaded.stored.request_id == second_request


def test_plan_review_order_must_keep_dependencies_before_dependents() -> None:
    """用户审核排序不能把等待 SemanticDocument 的任务放在文档解析之前。"""

    task_id = uuid4()
    document_parse = make_subtask(task_id, subtask_type="document_parse")
    custom_detection = make_subtask(
        task_id,
        subtask_type="custom_fault_detection",
        dependencies=[document_parse.subtask_id],
    )

    assert _dependency_order_violations(
        [document_parse, custom_detection],
        [custom_detection.subtask_id, document_parse.subtask_id],
    ) == ["“执行 custom_fault_detection”必须排在前置任务“执行 document_parse”之后"]
    assert not _dependency_order_violations(
        [document_parse, custom_detection],
        [document_parse.subtask_id, custom_detection.subtask_id],
    )
