"""验证自动测试模型适配器的顺序响应和真实Schema校验。"""

import pytest

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.exceptions import ModelGatewayError
from kg_agent.model_gateway.fake import FakeModelAdapter
from tests.factories import make_decomposition


@pytest.mark.asyncio
async def test_fake_model_records_call_and_validates_response() -> None:
    """FakeModelAdapter 应记录调用且返回目标 Pydantic 类型。"""

    expected = make_decomposition(__import__("uuid").uuid4())
    adapter = FakeModelAdapter([expected])

    actual = await adapter.generate_structured(
        system_prompt="system",
        user_prompt="user",
        output_type=TaskDecompositionResult,
    )

    assert actual == expected
    assert adapter.calls[0].output_model_name == "TaskDecompositionResult"


@pytest.mark.asyncio
async def test_fake_model_rejects_invalid_payload() -> None:
    """不符合契约的测试响应必须与真实模型响应一样被拒绝。"""

    adapter = FakeModelAdapter([{"task_intent": "QUERY"}])

    with pytest.raises(ModelGatewayError):
        await adapter.generate_structured(
            system_prompt="system",
            user_prompt="user",
            output_type=TaskDecompositionResult,
        )


@pytest.mark.asyncio
async def test_fake_model_accepts_whitespace_and_json_code_fence() -> None:
    """结构化解析器应只清理外围空白和完整 JSON 代码围栏。"""

    expected = make_decomposition(__import__("uuid").uuid4())
    raw = expected.model_dump_json()
    adapter = FakeModelAdapter([f"  ```json\n{raw}\n```  "])

    actual = await adapter.generate_structured(
        system_prompt="system",
        user_prompt="user",
        output_type=TaskDecompositionResult,
    )

    assert actual == expected
