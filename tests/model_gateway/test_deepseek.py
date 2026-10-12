"""验证 DeepSeek 适配器的 JSON Schema 优先策略与安全校验摘要。"""

from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from kg_agent.model_gateway.base import ModelCallContext, ModelCallTelemetry
from kg_agent.model_gateway.deepseek import DeepSeekModelAdapter, _format_validation_error


class _ExpectedPayload(BaseModel):
    """定义测试用最小结构化输出契约。"""

    count: int


def test_validation_summary_contains_field_path_without_raw_input() -> None:
    """校验摘要应指明错误字段，但不得回显模型提供的原始敏感值。"""

    sensitive_value = "不得写入日志的原始模型内容"
    try:
        _ExpectedPayload.model_validate({"count": sensitive_value})
    except ValidationError as exc:
        summary = _format_validation_error(exc)
    else:  # pragma: no cover - Pydantic 契约变化时明确使测试失败
        raise AssertionError("无效整数必须触发 ValidationError")

    assert "count" in summary
    assert "int_parsing" in summary
    assert sensitive_value not in summary


class _FakeCompletions:
    """记录 response_format，并可模拟端点拒绝原生 JSON Schema。"""

    def __init__(self, *, reject_schema: bool = False) -> None:
        self.reject_schema = reject_schema
        self.formats: list[dict[str, object]] = []

    async def create(self, **kwargs):  # type: ignore[no-untyped-def]
        """按测试配置返回合法 JSON 或抛出带状态码的协议错误。"""

        response_format = kwargs["response_format"]
        self.formats.append(response_format)
        if self.reject_schema and response_format["type"] == "json_schema":
            error = RuntimeError("unsupported response format")
            error.status_code = 400  # type: ignore[attr-defined]
            raise error
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='  ```json\n{"count": 2}\n```  '),
                    finish_reason="stop",
                )
            ],
            usage=None,
            _request_id="request-1",
        )


class _TelemetryObserver:
    """收集模型遥测，验证只有 Skill 原始回答进入受控留档链路。"""

    def __init__(self) -> None:
        self.items: list[ModelCallTelemetry] = []

    async def record(self, telemetry: ModelCallTelemetry) -> None:
        """保存一次测试遥测。"""

        self.items.append(telemetry)


def _adapter_with(
    completions: _FakeCompletions, *, observer: _TelemetryObserver | None = None
) -> DeepSeekModelAdapter:
    """创建不访问网络的适配器实例。"""

    adapter = object.__new__(DeepSeekModelAdapter)
    adapter._client = SimpleNamespace(  # type: ignore[attr-defined]
        chat=SimpleNamespace(completions=completions)
    )
    adapter._model = "test-model"  # type: ignore[attr-defined]
    adapter._endpoint_host = "example.invalid"  # type: ignore[attr-defined]
    adapter._observer = observer  # type: ignore[attr-defined]
    adapter._native_json_schema_supported = None  # type: ignore[attr-defined]
    return adapter


@pytest.mark.asyncio
async def test_adapter_prefers_native_json_schema_and_validates_fenced_json() -> None:
    """端点支持时应直接发送目标 Pydantic JSON Schema。"""

    completions = _FakeCompletions()
    adapter = _adapter_with(completions)

    result = await adapter.generate_structured(
        system_prompt="system",
        user_prompt="user",
        output_type=_ExpectedPayload,
        call_context=ModelCallContext(component="SKILL", actor_name="test_skill"),
    )

    assert result.count == 2
    assert completions.formats[0]["type"] == "json_schema"
    schema = completions.formats[0]["json_schema"]
    assert isinstance(schema, dict)
    assert schema["schema"]["properties"]["count"]["type"] == "integer"


@pytest.mark.asyncio
async def test_adapter_exposes_full_raw_output_only_for_skill_observer() -> None:
    """Skill 保留供应商原始字符串，Agent 调用不得记录原始回答。"""

    observer = _TelemetryObserver()
    adapter = _adapter_with(_FakeCompletions(), observer=observer)
    await adapter.generate_structured(
        system_prompt="system",
        user_prompt="skill-user",
        output_type=_ExpectedPayload,
        call_context=ModelCallContext(component="SKILL", actor_name="test_skill"),
    )
    await adapter.generate_structured(
        system_prompt="system",
        user_prompt="agent-user",
        output_type=_ExpectedPayload,
        call_context=ModelCallContext(component="PROFESSIONAL_AGENT", actor_name="test_agent"),
    )

    assert observer.items[0].raw_output == '  ```json\n{"count": 2}\n```  '
    assert observer.items[1].raw_output is None


@pytest.mark.asyncio
async def test_adapter_falls_back_once_when_endpoint_rejects_json_schema() -> None:
    """兼容端点拒绝 Schema 后应缓存能力，后续直接使用 JSON Object。"""

    completions = _FakeCompletions(reject_schema=True)
    adapter = _adapter_with(completions)

    await adapter.generate_structured(
        system_prompt="system",
        user_prompt="first",
        output_type=_ExpectedPayload,
        call_context=ModelCallContext(component="SKILL", actor_name="test_skill"),
    )
    await adapter.generate_structured(
        system_prompt="system",
        user_prompt="second",
        output_type=_ExpectedPayload,
        call_context=ModelCallContext(component="SKILL", actor_name="test_skill"),
    )

    assert [item["type"] for item in completions.formats] == [
        "json_schema",
        "json_object",
        "json_object",
    ]
