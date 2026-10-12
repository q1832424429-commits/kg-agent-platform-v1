"""提供禁用模式和测试模式的长期记忆存储实现。"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import uuid4

from kg_agent.domain.memory_models import (
    LongTermMemoryRecord,
    MemoryMessage,
    MemoryWriteReceipt,
)


class DisabledLongTermMemoryStore:
    """在未配置 Mem0 时提供显式禁用语义，不偷偷改存 PostgreSQL。"""

    @property
    def provider_name(self) -> str:
        """返回禁用状态名称。"""

        return "disabled"

    @property
    def enabled(self) -> bool:
        """表示当前不会持久化或召回长期记忆。"""

        return False

    async def add(
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
        """返回禁用回执，且不把长期记忆写入本地替代存储。"""

        return MemoryWriteReceipt(status="DISABLED", message="长期记忆未启用")

    async def search(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int,
        threshold: float | None,
        rerank: bool,
        app_id: str | None = None,
    ) -> list[LongTermMemoryRecord]:
        """禁用模式固定返回空结果。"""

        return []

    async def update(
        self,
        *,
        provider_memory_id: str,
        text: str,
        metadata: dict[str, str | int | float | bool | None],
    ) -> MemoryWriteReceipt:
        """禁用模式不更新任何正文，只返回明确禁用回执。"""

        del provider_memory_id, text, metadata
        return MemoryWriteReceipt(status="DISABLED", message="长期记忆未启用")

    async def delete(self, *, provider_memory_id: str) -> MemoryWriteReceipt:
        """禁用模式不执行删除，保持与真实存储相同的公开回执形状。"""

        del provider_memory_id
        return MemoryWriteReceipt(status="DISABLED", message="长期记忆未启用")


class InMemoryLongTermMemoryStore:
    """供单元测试和隔离模拟环境使用的确定性内存适配器。"""

    def __init__(self) -> None:
        """初始化按用户隔离的空记忆集合。"""

        self._records: dict[tuple[str, str | None], list[LongTermMemoryRecord]] = {}

    @property
    def provider_name(self) -> str:
        """返回测试供应商名称。"""

        return "in-memory"

    @property
    def enabled(self) -> bool:
        """内存实现可执行写入与搜索，因此视为启用。"""

        return True

    async def add(
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
        """把消息合并为一条测试记忆，保留元数据但不模拟模型推理。"""

        content = "\n".join(f"{item.role}: {item.content}" for item in messages)
        record = LongTermMemoryRecord(
            provider_memory_id=str(uuid4()),
            memory=content,
            metadata={**metadata, "infer": infer},
            created_at=datetime.now(UTC),
        )
        self._records.setdefault((user_id, app_id), []).append(record)
        return MemoryWriteReceipt(
            event_id=record.provider_memory_id, status="SUCCEEDED", message="测试记忆已写入"
        )

    async def search(
        self,
        *,
        user_id: str,
        query: str,
        top_k: int,
        threshold: float | None,
        rerank: bool,
        app_id: str | None = None,
    ) -> list[LongTermMemoryRecord]:
        """按简单词项重叠计算确定性分数，验证召回链路而非语义模型质量。"""

        query_terms = self._terms(query)
        scored: list[LongTermMemoryRecord] = []
        for record in self._records.get((user_id, app_id), []):
            memory_terms = self._terms(record.memory)
            overlap = len(query_terms & memory_terms)
            score = overlap / max(len(query_terms), 1)
            if threshold is not None and score < threshold:
                continue
            scored.append(record.model_copy(update={"score": score}))
        scored.sort(key=lambda item: item.score or 0, reverse=True)
        return scored[:top_k]

    async def update(
        self,
        *,
        provider_memory_id: str,
        text: str,
        metadata: dict[str, str | int | float | bool | None],
    ) -> MemoryWriteReceipt:
        """按供应商 ID 原位替换测试记忆，模拟 Mem0 Update API。"""

        for records in self._records.values():
            for index, record in enumerate(records):
                if record.provider_memory_id != provider_memory_id:
                    continue
                records[index] = record.model_copy(
                    update={
                        "memory": text,
                        "metadata": metadata,
                        "updated_at": datetime.now(UTC),
                    }
                )
                return MemoryWriteReceipt(
                    event_id=provider_memory_id,
                    status="SUCCEEDED",
                    message="测试记忆已更新",
                )
        return MemoryWriteReceipt(status="NOT_FOUND", message="测试记忆不存在")

    async def delete(self, *, provider_memory_id: str) -> MemoryWriteReceipt:
        """按供应商 ID 移除测试记忆，用于验证会话彻底删除边界。"""

        for records in self._records.values():
            for index, record in enumerate(records):
                if record.provider_memory_id == provider_memory_id:
                    records.pop(index)
                    return MemoryWriteReceipt(
                        event_id=provider_memory_id,
                        status="DELETED",
                        message="测试记忆已删除",
                    )
        return MemoryWriteReceipt(status="NOT_FOUND", message="测试记忆不存在")

    @staticmethod
    def _terms(value: str) -> set[str]:
        """把中英文文本切成适合测试排序的字符和单词集合。"""

        lowered = value.lower()
        words = set(re.findall(r"[a-z0-9_]+", lowered))
        chinese = {char for char in lowered if "\u4e00" <= char <= "\u9fff"}
        return words | chinese
