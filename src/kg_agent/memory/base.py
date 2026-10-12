"""定义长期记忆供应商适配器必须满足的最小协议。"""

from typing import Protocol

from kg_agent.domain.memory_models import (
    LongTermMemoryRecord,
    MemoryMessage,
    MemoryWriteReceipt,
)


class LongTermMemoryStore(Protocol):
    """隔离 Mem0 Cloud 与业务层，使召回和写入可以独立测试。"""

    @property
    def provider_name(self) -> str:
        """返回用于观测的供应商名称，不能包含连接凭据。"""
        ...

    @property
    def enabled(self) -> bool:
        """返回当前适配器是否会访问真实长期记忆存储。"""
        ...

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
        """把公开消息提交给长期记忆存储并返回写入回执。"""
        ...

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
        """在指定用户命名空间内执行语义检索。"""
        ...

    async def update(
        self,
        *,
        provider_memory_id: str,
        text: str,
        metadata: dict[str, str | int | float | bool | None],
    ) -> MemoryWriteReceipt:
        """原位更新一条已存在的长期记忆，用于真正合并而不是追加重复副本。"""
        ...

    async def delete(self, *, provider_memory_id: str) -> MemoryWriteReceipt:
        """物理删除一条已精确定位且不再允许召回的长期记忆。"""
        ...
