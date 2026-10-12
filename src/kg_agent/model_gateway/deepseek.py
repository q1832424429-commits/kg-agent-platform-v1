"""通过 OpenAI 兼容接口调用 DeepSeek 并校验结构化输出。"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime
from time import perf_counter
from urllib.parse import urlparse
from uuid import uuid4

from openai import AsyncOpenAI
from pydantic import ValidationError

from kg_agent.config import Settings
from kg_agent.exceptions import ModelGatewayError
from kg_agent.model_gateway.base import (
    ModelCallContext,
    ModelCallObserver,
    ModelCallTelemetry,
    StructuredModelT,
)
from kg_agent.model_gateway.structured_output import (
    format_validation_error,
    validate_structured_text,
)

_LOGGER = logging.getLogger("kg_agent.model_call")


def _format_validation_error(exc: ValidationError | ValueError) -> str:
    """生成可安全写入日志和修复提示的结构化校验摘要。

    摘要只保留字段路径、错误类型和校验消息，不包含模型原始输出，避免把文档内容或
    其他敏感上下文带入日志。总控 Agent 的修复轮次也可以据此准确修正错误字段。
    """

    return format_validation_error(exc)


class DeepSeekModelAdapter:
    """封装 DeepSeek 网络调用，避免 Agent 依赖具体 SDK。

    本适配器只返回经 Pydantic 校验的结构化结果。它不会持久化会话，也不会记录或
    暴露模型隐藏思维过程。
    """

    def __init__(self, settings: Settings, *, observer: ModelCallObserver | None = None) -> None:
        """使用已经校验的 DeepSeek 配置初始化异步客户端。"""

        api_key, base_url, model = settings.require_deepseek()
        # Skill 与 Agent 层都有明确、可观测的重试和超时边界。关闭 SDK 隐式重试，
        # 避免一次公开显示的模型调用在底层悄悄重复请求，造成耗时和 Token 难以解释。
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url, max_retries=0)
        self._model = model
        self._endpoint_host = urlparse(base_url).netloc or base_url
        self._observer = observer
        # None 表示尚未探测；端点明确拒绝 JSON Schema 后，本进程后续调用直接使用
        # JSON Object，避免每个 Skill 都额外产生一次必然失败的协议请求。
        self._native_json_schema_supported: bool | None = None

    async def generate_structured(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        output_type: type[StructuredModelT],
        call_context: ModelCallContext | None = None,
    ) -> StructuredModelT:
        """调用 DeepSeek JSON Output 并转换为目标契约。

        Args:
            system_prompt: 总控 Agent 的稳定系统约束。
            user_prompt: 当前任务和上下文组成的提示。
            output_type: 期望返回的 Pydantic 模型类型。

        Returns:
            通过目标 Schema 校验的模型结果。

        Raises:
            ModelGatewayError: 网络调用失败、返回空内容或内容不符合契约时抛出。
        """

        call_id = str(uuid4())
        started_at = datetime.now(UTC)
        started = perf_counter()
        response = None
        try:
            use_native_schema = (
                call_context is not None
                and call_context.component == "SKILL"
                and self._native_json_schema_supported is not False
            )
            if not use_native_schema:
                response = await self._client.chat.completions.create(
                    model=self._model,
                    messages=[
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ],
                    response_format={"type": "json_object"},
                )
            else:
                try:
                    response = await self._client.chat.completions.create(
                        model=self._model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        # 优先使用供应商原生 JSON Schema。strict=False 兼容现有
                        # 带默认值的契约；本地仍执行完整 Pydantic 校验。
                        response_format={
                            "type": "json_schema",
                            "json_schema": {
                                "name": output_type.__name__[:64],
                                "strict": False,
                                "schema": output_type.model_json_schema(),
                            },
                        },
                    )
                    self._native_json_schema_supported = True
                except Exception as schema_exc:
                    status_code = getattr(schema_exc, "status_code", None)
                    if status_code not in {400, 404, 422}:
                        raise
                    # 部分兼容端点只实现 JSON Object。协议协商失败必须可观测，
                    # 且本进程只探测一次，不能让每个 Skill 重复失败。
                    self._native_json_schema_supported = False
                    await self._record_telemetry(
                        call_id=call_id,
                        call_context=call_context,
                        output_type=output_type,
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        started_at=started_at,
                        started=started,
                        status="FAILED",
                        error_type="JSON_SCHEMA_UNSUPPORTED",
                        http_status=status_code,
                    )
                    call_id = str(uuid4())
                    started_at = datetime.now(UTC)
                    started = perf_counter()
                    response = await self._client.chat.completions.create(
                        model=self._model,
                        messages=[
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        response_format={"type": "json_object"},
                    )
        except asyncio.CancelledError:
            # 上层 asyncio.timeout 以取消协程实现。必须先记录这次真实请求已经等待了
            # 多久，再继续抛出取消，让 Skill 将其转换为可恢复的超时结果。
            await self._record_telemetry(
                call_id=call_id,
                call_context=call_context,
                output_type=output_type,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                started_at=started_at,
                started=started,
                status="FAILED",
                error_type="TimeoutError",
            )
            raise
        except Exception as exc:
            status_code = getattr(exc, "status_code", None)
            await self._record_telemetry(
                call_id=call_id,
                call_context=call_context,
                output_type=output_type,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                started_at=started_at,
                started=started,
                status="FAILED",
                error_type=type(exc).__name__,
                http_status=status_code if isinstance(status_code, int) else None,
            )
            status = f"，HTTP {status_code}" if status_code is not None else ""
            raise ModelGatewayError(
                f"DeepSeek 模型调用失败（{type(exc).__name__}{status}）"
            ) from exc

        content = response.choices[0].message.content if response.choices else None
        if not content:
            await self._record_telemetry(
                call_id=call_id,
                call_context=call_context,
                output_type=output_type,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                started_at=started_at,
                started=started,
                status="FAILED",
                response=response,
                error_type="EMPTY_RESPONSE",
            )
            raise ModelGatewayError("DeepSeek 返回了空的结构化结果")

        try:
            result = validate_structured_text(content, output_type)
        except (ValidationError, ValueError) as exc:
            detail = _format_validation_error(exc)
            await self._record_telemetry(
                call_id=call_id,
                call_context=call_context,
                output_type=output_type,
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                started_at=started_at,
                started=started,
                status="FAILED",
                response=response,
                error_type=type(exc).__name__,
                raw_output=content if self._should_archive_raw_output(call_context) else None,
            )
            raise ModelGatewayError(f"DeepSeek 返回内容不符合目标数据契约：{detail}") from exc
        await self._record_telemetry(
            call_id=call_id,
            call_context=call_context,
            output_type=output_type,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            started_at=started_at,
            started=started,
            status="SUCCEEDED",
            response=response,
            raw_output=content if self._should_archive_raw_output(call_context) else None,
        )
        return result

    @staticmethod
    def _should_archive_raw_output(call_context: ModelCallContext | None) -> bool:
        """只允许归档业务 Skill 的结构化回答，不保存 Agent 对话或隐藏推理。"""

        return call_context is not None and call_context.component == "SKILL"

    async def _record_telemetry(
        self,
        *,
        call_id: str,
        call_context: ModelCallContext | None,
        output_type: type[StructuredModelT],
        system_prompt: str,
        user_prompt: str,
        started_at: datetime,
        started: float,
        status: str,
        response: object | None = None,
        error_type: str | None = None,
        http_status: int | None = None,
        raw_output: str | None = None,
    ) -> None:
        """记录已脱敏模型指标；观察失败绝不能改变业务调用结果。"""

        finished_at = datetime.now(UTC)
        usage = getattr(response, "usage", None)
        choices = getattr(response, "choices", []) or []
        first_choice = choices[0] if choices else None
        telemetry = ModelCallTelemetry(
            call_id=call_id,
            context=call_context,
            model_name=self._model,
            endpoint_host=self._endpoint_host,
            output_model_name=output_type.__name__,
            system_prompt_chars=len(system_prompt),
            user_prompt_chars=len(user_prompt),
            started_at=started_at,
            finished_at=finished_at,
            duration_ms=round((perf_counter() - started) * 1000, 3),
            status=status,  # type: ignore[arg-type]
            provider_request_id=getattr(response, "_request_id", None),
            prompt_tokens=getattr(usage, "prompt_tokens", None),
            completion_tokens=getattr(usage, "completion_tokens", None),
            total_tokens=getattr(usage, "total_tokens", None),
            finish_reason=getattr(first_choice, "finish_reason", None),
            error_type=error_type,
            http_status=http_status,
            raw_output=raw_output,
        )
        if self._observer is not None:
            try:
                await self._observer.record(telemetry)
            except Exception:  # pragma: no cover - 外部日志介质不可用时仅降级为标准日志
                _LOGGER.exception("模型调用遥测写入失败", extra={"call_id": call_id})
        _LOGGER.info(
            "model_call_completed",
            extra={
                "model_call": {
                    "call_id": telemetry.call_id,
                    "component": telemetry.context.component if telemetry.context else None,
                    "actor_name": telemetry.context.actor_name if telemetry.context else None,
                    "task_id": str(telemetry.context.task_id)
                    if telemetry.context and telemetry.context.task_id
                    else None,
                    "skill_name": telemetry.context.skill_name if telemetry.context else None,
                    "model_name": telemetry.model_name,
                    "endpoint_host": telemetry.endpoint_host,
                    "duration_ms": telemetry.duration_ms,
                    "status": telemetry.status,
                    "prompt_chars": telemetry.system_prompt_chars + telemetry.user_prompt_chars,
                    "prompt_tokens": telemetry.prompt_tokens,
                    "completion_tokens": telemetry.completion_tokens,
                    "provider_request_id": telemetry.provider_request_id,
                    "error_type": telemetry.error_type,
                    "http_status": telemetry.http_status,
                }
            },
        )
