"""定义会话、SharedContext 记忆引用和 Mem0 长期记忆交换契约。

SharedContext 仍只保存摘要与来源引用；长期记忆正文由 Mem0 Cloud 保存。任何记忆
契约都不得包含模型隐藏思维过程、API 密钥或大型 Artifact 正文。
"""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field


class ConversationContextRef(BaseModel):
    """保存当前任务关联会话的标识和受控摘要。"""

    conversation_id: UUID
    latest_message_id: UUID | None = None
    summary: str = ""


class MemoryRef(BaseModel):
    """保存 SharedContext 中结构化记忆的轻量引用。"""

    memory_id: UUID
    memory_type: Literal["SHORT_TERM", "LONG_TERM", "EXPERIENCE"]
    summary: str
    source_refs: list[str] = Field(default_factory=list)


class MemoryMessage(BaseModel):
    """表示提交给长期记忆服务的一条公开对话消息。"""

    role: Literal["user", "assistant", "system"]
    content: str = Field(min_length=1)


class LongTermMemoryRecord(BaseModel):
    """表示从 Mem0 Cloud 召回的一条长期记忆及其相关度。"""

    provider_memory_id: str = Field(min_length=1)
    memory: str = Field(min_length=1)
    score: float | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    categories: list[str] = Field(default_factory=list)
    created_at: datetime | None = None
    updated_at: datetime | None = None


class MemoryWriteReceipt(BaseModel):
    """表示 Mem0 异步写入请求已被接受后的事件回执。"""

    event_id: str | None = None
    status: str
    message: str = ""


class CompletedTaskExperience(BaseModel):
    """表示一个用户目标完整成功后可写入长期记忆的聚合经验。

    该契约只承载公开摘要和业务引用，不包含文档正文、模型隐藏推理或 Mem0 凭据。
    ``experience_key`` 由任务及当前修订确定性生成，用于阻止协调器重试造成重复写入。
    """

    experience_key: str = Field(min_length=1, max_length=512)
    task_id: UUID
    task_spec_revision: int = Field(ge=1)
    decomposition_revision: int = Field(ge=1)
    summary: str = Field(min_length=1)
    source_refs: list[str] = Field(default_factory=list)


class MemoryConsolidationResult(BaseModel):
    """记录长期经验经过语义去重后的确定性处理结果。"""

    action: Literal["CREATED", "MERGED", "SKIPPED_DUPLICATE", "DISABLED"]
    receipt: MemoryWriteReceipt
    similarity: float | None = Field(default=None, ge=0, le=1)
    matched_provider_memory_id: str | None = None


class MemorySearchRequest(BaseModel):
    """表示平台内部或 API 发起的一次长期记忆语义检索。"""

    query: str = Field(min_length=1)
    top_k: int = Field(default=8, ge=1, le=100)
    threshold: float | None = Field(default=None, ge=0, le=1)
    rerank: bool = False


class MemoryCreateRequest(BaseModel):
    """表示用户显式授权写入 Mem0 Cloud 的一条长期记忆。"""

    text: str = Field(min_length=1)
    infer: bool = True
    authorized: bool = Field(
        default=False,
        description="必须由用户明确设为 true，平台才会写入长期记忆。",
    )
    source_refs: list[str] = Field(default_factory=list)
