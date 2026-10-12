"""提供自动测试使用的可编排模型适配器。

FakeModelAdapter 只替代外部模型响应，不替代总控 Agent、校验器或上下文发布逻辑。
"""

from collections import deque
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ValidationError

from kg_agent.exceptions import ModelGatewayError
from kg_agent.model_gateway.base import ModelCall, ModelCallContext, StructuredModelT
from kg_agent.model_gateway.structured_output import (
    format_validation_error,
    validate_structured_text,
)


class FakeModelAdapter:
    """按预设顺序返回结构化响应，并记录每次模型调用。"""

    def __init__(self, responses: Iterable[BaseModel | dict[str, Any] | str]) -> None:
        """保存测试响应队列。

        Args:
            responses: 每次调用依次消费的 Pydantic 对象、字典或 JSON 字符串。
        """

        self._responses = deque(responses)
        self.calls: list[ModelCall] = []

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_type: type[StructuredModelT],
        call_context: ModelCallContext | None = None,
    ) -> StructuredModelT:
        """返回下一个预设响应并使用真实 Pydantic Schema 校验。"""

        self.calls.append(
            ModelCall(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                output_model_name=output_type.__name__,
            )
        )
        if not self._responses:
            raise ModelGatewayError("FakeModelAdapter 没有可用的预设响应")

        response = self._responses.popleft()
        try:
            if isinstance(response, str):
                return validate_structured_text(response, output_type)
            if isinstance(response, BaseModel):
                return output_type.model_validate(response.model_dump(mode="python"))
            return output_type.model_validate(response)
        except (ValidationError, ValueError) as exc:
            detail = format_validation_error(exc)
            raise ModelGatewayError(f"FakeModelAdapter 响应不符合目标数据契约：{detail}") from exc
