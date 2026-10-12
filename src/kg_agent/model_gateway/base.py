"""定义与具体模型供应商无关的结构化输出网关和公开遥测契约。

本模块只描述一次模型调用的可观测事实，例如耗时、提示词大小、供应商请求标识和
Token 用量。Skill 的原始结构化回答可交给受控观察器单独留档，用于核对模型输出与
最终 Artifact；总控和专业 Agent 的回答以及模型隐藏思维过程仍不留档。
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel

StructuredModelT = TypeVar("StructuredModelT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class ModelCall:
    """记录一次模型调用的可测试输入，不包含密钥或隐藏思维过程。"""

    system_prompt: str
    user_prompt: str
    output_model_name: str


@dataclass(frozen=True, slots=True)
class ModelCallContext:
    """标识一次模型调用所属的公开业务阶段，便于按任务排障。

    该上下文不是 SharedContext 的替代品，只用于把模型级指标关联到现有任务、Agent
    或 Skill。字段全部是已有标识符，不能放入聊天正文或任何敏感业务内容。
    """

    component: Literal["CONTROLLER", "PROFESSIONAL_AGENT", "SKILL"]
    actor_name: str
    task_id: UUID | None = None
    subtask_id: UUID | None = None
    assignment_id: UUID | None = None
    skill_name: str | None = None
    attempt: int = 0


@dataclass(frozen=True, slots=True)
class ModelCallTelemetry:
    """一次模型调用结束后的公开诊断指标。

    非流式 OpenAI 兼容接口无法可靠提供首 token 到达时间，因此这里明确只记录端到端
    墙钟耗时；后续若切换流式协议，可在不改变 Agent 契约的前提下新增该指标。
    """

    call_id: str
    context: ModelCallContext | None
    model_name: str
    endpoint_host: str
    output_model_name: str
    system_prompt_chars: int
    user_prompt_chars: int
    started_at: datetime
    finished_at: datetime
    duration_ms: float
    status: Literal["SUCCEEDED", "FAILED"]
    provider_request_id: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    total_tokens: int | None = None
    finish_reason: str | None = None
    error_type: str | None = None
    http_status: int | None = None
    raw_output: str | None = field(default=None, repr=False)


class ModelCallObserver(Protocol):
    """接收模型调用指标的异步观察接口，不参与模型请求和业务决策。"""

    async def record(self, telemetry: ModelCallTelemetry) -> None:
        """持久化或输出一条已经脱敏的调用指标。"""
        ...


class ModelGateway(Protocol):
    """约束总控 Agent 获取 Pydantic 结构化输出的统一接口。"""

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_type: type[StructuredModelT],
        call_context: ModelCallContext | None = None,
    ) -> StructuredModelT:
        """调用模型并把结果校验为指定 Pydantic 类型及公开指标。"""
        ...
