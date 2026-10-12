"""统一导出模型网关协议及第一阶段适配器。"""

from kg_agent.model_gateway.base import (
    ModelCall,
    ModelCallContext,
    ModelCallObserver,
    ModelCallTelemetry,
    ModelGateway,
)
from kg_agent.model_gateway.deepseek import DeepSeekModelAdapter
from kg_agent.model_gateway.fake import FakeModelAdapter

__all__ = [
    "DeepSeekModelAdapter",
    "FakeModelAdapter",
    "ModelCall",
    "ModelCallContext",
    "ModelCallObserver",
    "ModelCallTelemetry",
    "ModelGateway",
]
