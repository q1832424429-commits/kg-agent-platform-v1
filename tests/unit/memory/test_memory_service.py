"""验证长期记忆服务的授权写入、质量门禁、去重、过滤和环境隔离。"""

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest

from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.memory_models import (
    CompletedTaskExperience,
    LongTermMemoryRecord,
    MemoryMessage,
    MemoryRef,
    MemoryWriteReceipt,
)
from kg_agent.memory.memory_service import LongTermMemoryService
from kg_agent.memory.stores import DisabledLongTermMemoryStore, InMemoryLongTermMemoryStore


@pytest.mark.asyncio
async def test_authorized_memory_can_be_recalled_as_shared_context_ref() -> None:
    """显式写入的记忆应能按语义检索并转成 Mem0 来源引用。"""

    service = LongTermMemoryService(
        InMemoryLongTermMemoryStore(),
        user_id="single-user",
        default_threshold=0.1,
    )
    await service.create_authorized_memory(
        text="用户偏好使用设备故障本体",
        source_refs=["conversation:1"],
        infer=True,
    )

    refs = await service.recall_refs("设备故障抽取")

    assert len(refs) == 1
    assert refs[0].memory_type == "LONG_TERM"
    assert "设备故障本体" in refs[0].summary
    assert refs[0].source_refs[0].startswith("mem0://memory/")


@pytest.mark.asyncio
async def test_disabled_store_does_not_create_local_long_term_fallback() -> None:
    """未配置 Mem0 时应明确返回空结果，不能偷偷把正文留在 PostgreSQL。"""

    service = LongTermMemoryService(DisabledLongTermMemoryStore(), user_id=f"user-{uuid4()}")
    receipt = await service.create_authorized_memory(
        text="不会持久化的测试记忆",
        source_refs=[],
        infer=True,
    )

    assert receipt.status == "DISABLED"
    assert await service.search("测试") == []


@pytest.mark.asyncio
async def test_memory_service_isolates_records_by_app_id() -> None:
    """同一用户在测试端写入的记忆不得被正式端命名空间召回。"""

    store = InMemoryLongTermMemoryStore()
    simulation = LongTermMemoryService(
        store,
        user_id="single-user",
        app_id="kg-agent-platform-simulation",
        default_threshold=0,
    )
    production = LongTermMemoryService(
        store,
        user_id="single-user",
        app_id="kg-agent-platform-production",
        default_threshold=0,
    )
    await simulation.create_authorized_memory(
        text="测试端偏好抽取设备关系",
        source_refs=["simulation"],
        infer=False,
    )

    assert await simulation.search("设备")
    assert await production.search("设备") == []


@pytest.mark.asyncio
async def test_authorized_memory_skips_semantic_duplicate_write() -> None:
    """相同授权记忆第二次写入时应返回去重回执且只保留一条正文。"""

    service = LongTermMemoryService(
        InMemoryLongTermMemoryStore(),
        user_id="single-user",
        default_threshold=0,
    )
    first = await service.create_authorized_memory(
        text="用户偏好使用设备故障本体",
        source_refs=["conversation:1"],
        infer=True,
    )
    second = await service.create_authorized_memory(
        text="用户偏好使用设备故障本体",
        source_refs=["conversation:2"],
        infer=True,
    )

    assert first.status == "SUCCEEDED"
    assert second.status == "SKIPPED_DUPLICATE"
    assert len(await service.search("设备故障本体", threshold=0)) == 1


class _RecallFixtureStore(InMemoryLongTermMemoryStore):
    """返回预置候选，便于验证服务层过滤、排序和最终数量。"""

    def __init__(self, records: list[LongTermMemoryRecord]) -> None:
        """保存预置记录和最后一次搜索参数。"""

        super().__init__()
        self.records = records
        self.last_top_k: int | None = None
        self.last_rerank: bool | None = None

    async def search(  # type: ignore[override]
        self,
        *,
        user_id: str,
        query: str,
        top_k: int,
        threshold: float | None,
        rerank: bool,
        app_id: str | None = None,
    ) -> list[LongTermMemoryRecord]:
        """原样返回候选，并记录服务是否按候选池和重排配置调用。"""

        self.last_top_k = top_k
        self.last_rerank = rerank
        return self.records[:top_k]


class _ExperiencePolicyStore(_RecallFixtureStore):
    """返回指定语义分数并记录完整经验最终写入内容。"""

    def __init__(self, *, score: float | None) -> None:
        """用一条已存在的完整任务经验初始化候选池。"""

        records = []
        if score is not None:
            records.append(
                LongTermMemoryRecord(
                    provider_memory_id="existing-experience",
                    memory="完整任务成功经验\n业务目标：抽取设备知识",
                    score=score,
                    metadata={"source": "completed_task_experience"},
                )
            )
        super().__init__(records)
        self.added_messages: list[MemoryMessage] = []
        self.added_metadata: dict[str, str | int | float | bool | None] = {}
        self.updated_memory_id: str | None = None
        self.updated_text: str | None = None

    async def add(  # type: ignore[override]
        self,
        *,
        user_id: str,
        messages: list[MemoryMessage],
        metadata: dict[str, str | int | float | bool | None],
        infer: bool,
        agent_id: str | None = None,
        run_id: str | None = None,
        app_id: str | None = None,
    ) -> MemoryWriteReceipt:
        """记录服务层提交的标准消息与合并元数据。"""

        del user_id, infer, agent_id, run_id, app_id
        self.added_messages = messages
        self.added_metadata = metadata
        return MemoryWriteReceipt(status="SUCCEEDED", message="已写入")

    async def update(  # type: ignore[override]
        self,
        *,
        provider_memory_id: str,
        text: str,
        metadata: dict[str, str | int | float | bool | None],
    ) -> MemoryWriteReceipt:
        """记录服务层是否对命中经验执行了原位合并。"""

        self.updated_memory_id = provider_memory_id
        self.updated_text = text
        self.added_metadata = metadata
        return MemoryWriteReceipt(status="SUCCEEDED", message="已更新")


def _completed_experience() -> CompletedTaskExperience:
    """生成三档相似度策略共用的完整成功经验。"""

    task_id = uuid4()
    return CompletedTaskExperience(
        experience_key=f"task:{task_id}:task-spec:1:decomposition:1",
        task_id=task_id,
        task_spec_revision=1,
        decomposition_revision=1,
        summary=(
            "完整任务成功经验\n业务目标：抽取设备知识\n"
            "执行路径：解析文档 → 知识抽取"
        ),
        source_refs=["agent-run://run-1"],
    )


@pytest.mark.asyncio
async def test_completed_experience_above_092_is_not_written() -> None:
    """语义相似度大于等于 0.92 时必须直接跳过，不调用写入 API。"""

    store = _ExperiencePolicyStore(score=0.93)
    service = LongTermMemoryService(store, user_id="single-user")

    result = await service.remember_completed_task_experience(_completed_experience())

    assert result.action == "SKIPPED_DUPLICATE"
    assert result.similarity == 0.93
    assert store.added_messages == []


@pytest.mark.asyncio
async def test_completed_experience_between_080_and_092_is_merged() -> None:
    """0.80 到 0.92 区间应合并事实后写入，并记录被合并的供应商记忆。"""

    store = _ExperiencePolicyStore(score=0.85)
    service = LongTermMemoryService(store, user_id="single-user")

    result = await service.remember_completed_task_experience(_completed_experience())

    assert result.action == "MERGED"
    assert result.similarity == 0.85
    assert result.matched_provider_memory_id == "existing-experience"
    assert store.added_messages == []
    assert store.updated_memory_id == "existing-experience"
    assert store.updated_text is not None
    assert "执行路径：解析文档 → 知识抽取" in store.updated_text
    assert store.added_metadata["consolidation_action"] == "MERGED"


@pytest.mark.asyncio
async def test_completed_experience_below_080_is_created() -> None:
    """语义相似度低于 0.80 时应创建独立完整经验，不拼接旧正文。"""

    store = _ExperiencePolicyStore(score=0.79)
    service = LongTermMemoryService(store, user_id="single-user")
    experience = _completed_experience()

    result = await service.remember_completed_task_experience(experience)

    assert result.action == "CREATED"
    assert result.similarity == 0.79
    assert store.added_messages[0].content == experience.summary
    assert store.added_metadata["consolidation_action"] == "CREATED"


@pytest.mark.asyncio
async def test_recall_filters_inactive_and_collapses_near_duplicates() -> None:
    """失效项和近似重复项不得进入 Agent，只返回配置的最终条数。"""

    now = datetime.now(UTC)
    store = _RecallFixtureStore(
        [
            LongTermMemoryRecord(
                provider_memory_id="best",
                memory="用户偏好使用设备故障本体。",
                score=0.98,
                updated_at=now,
            ),
            LongTermMemoryRecord(
                provider_memory_id="duplicate",
                memory=" 用户偏好使用设备故障本体 ",
                score=0.97,
                updated_at=now - timedelta(days=1),
            ),
            LongTermMemoryRecord(
                provider_memory_id="inactive",
                memory="已经废弃的旧本体",
                score=0.99,
                metadata={"status": "SUPERSEDED"},
            ),
            LongTermMemoryRecord(
                provider_memory_id="second",
                memory="关系抽取必须保留证据",
                score=0.91,
            ),
            LongTermMemoryRecord(
                provider_memory_id="third",
                memory="图谱发布必须人工审核",
                score=0.90,
            ),
        ]
    )
    service = LongTermMemoryService(
        store,
        user_id="single-user",
        default_top_k=2,
        candidate_top_k=5,
        default_threshold=0.5,
        default_rerank=True,
    )

    refs = await service.recall_refs("设备故障知识抽取")

    assert store.last_top_k == 5
    assert store.last_rerank is True
    assert [item.summary for item in refs] == [
        "用户偏好使用设备故障本体。",
        "关系抽取必须保留证据",
    ]


@pytest.mark.asyncio
async def test_recall_excludes_legacy_agent_and_task_turn_fragments() -> None:
    """历史单 Agent 与任务提交碎片应保留在 Mem0，但不得再注入 Agent 上下文。"""

    store = _RecallFixtureStore(
        [
            LongTermMemoryRecord(
                provider_memory_id="legacy-agent",
                memory="旧专业 Agent 碎片",
                score=0.99,
                metadata={"source": "agent_result"},
            ),
            LongTermMemoryRecord(
                provider_memory_id="legacy-turn",
                memory="旧任务提交摘要",
                score=0.98,
                metadata={"source": "task_turn"},
            ),
            LongTermMemoryRecord(
                provider_memory_id="completed",
                memory="完整任务成功经验",
                score=0.91,
                metadata={"source": "completed_task_experience"},
            ),
        ]
    )
    service = LongTermMemoryService(
        store,
        user_id="single-user",
        default_top_k=3,
        candidate_top_k=3,
    )

    refs = await service.recall_refs("成功经验")

    assert [item.summary for item in refs] == ["完整任务成功经验"]


@pytest.mark.asyncio
async def test_failed_agent_result_is_not_written_to_long_term_memory() -> None:
    """失败运行产生的经验摘要不得进入 Mem0。"""

    service = LongTermMemoryService(
        InMemoryLongTermMemoryStore(),
        user_id="single-user",
        default_threshold=0,
    )
    result = AgentRunResult.model_construct(
        status=AgentRunStatus.FAILED,
        output_artifact_ids=[],
        blocking_issues=["抽取失败"],
        memory_entries=[
            MemoryRef(
                memory_id=uuid4(),
                memory_type="EXPERIENCE",
                summary="失败但不应被记住的经验",
            )
        ],
    )

    assert await service.remember_agent_result(result) == []
    assert await service.search("失败", threshold=0) == []
