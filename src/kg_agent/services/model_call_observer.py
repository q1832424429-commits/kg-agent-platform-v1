"""把模型调用指标和 Skill 原始回答引用写入现有任务级日志。

Prompt、文档输入、Agent 回答和隐藏推理均不落盘。仅 Skill 的结构化原始回答单独保存，
JSONL 记录其路径与校验和，供核对“模型原始输出→Pydantic→Artifact”的数据流。
"""

from __future__ import annotations

from kg_agent.model_gateway.base import ModelCallTelemetry
from kg_agent.services.run_log_service import RunLogService


class RunLogModelCallObserver:
    """将有 task_id 的模型调用遥测追加到现有 RunLogService。

    没有任务归属的启动期探测或单元调用只会被适配器写入标准日志，避免创建无法归属的
    JSONL 文件。RunLog 写入失败由适配器捕获，不会反向中断业务任务。
    """

    def __init__(self, run_logs: RunLogService) -> None:
        """保存任务日志服务，不创建新的日志存储机制。"""

        self._run_logs = run_logs

    async def record(self, telemetry: ModelCallTelemetry) -> None:
        """把脱敏指标转换为 JSON 可序列化载荷并按任务追加。"""

        context = telemetry.context
        if context is None or context.task_id is None:
            return
        task_id = context.task_id
        raw_output_ref: str | None = None
        raw_output_checksum: str | None = None
        if context.component == "SKILL" and telemetry.raw_output is not None:
            stored = await self._run_logs.save_model_output(
                task_id=task_id,
                call_id=telemetry.call_id,
                content=telemetry.raw_output,
            )
            if stored is not None:
                raw_output_ref, raw_output_checksum = stored
        payload = {
            "call_id": telemetry.call_id,
            "component": context.component,
            "actor_name": context.actor_name,
            "subtask_id": str(context.subtask_id) if context.subtask_id else None,
            "assignment_id": str(context.assignment_id) if context.assignment_id else None,
            "skill_name": context.skill_name,
            "attempt": context.attempt,
            "model_name": telemetry.model_name,
            "endpoint_host": telemetry.endpoint_host,
            "output_model_name": telemetry.output_model_name,
            "system_prompt_chars": telemetry.system_prompt_chars,
            "user_prompt_chars": telemetry.user_prompt_chars,
            "prompt_chars": telemetry.system_prompt_chars + telemetry.user_prompt_chars,
            "started_at": telemetry.started_at.isoformat(),
            "finished_at": telemetry.finished_at.isoformat(),
            "duration_ms": telemetry.duration_ms,
            "status": telemetry.status,
            "provider_request_id": telemetry.provider_request_id,
            "prompt_tokens": telemetry.prompt_tokens,
            "completion_tokens": telemetry.completion_tokens,
            "total_tokens": telemetry.total_tokens,
            "finish_reason": telemetry.finish_reason,
            "error_type": telemetry.error_type,
            "http_status": telemetry.http_status,
            "raw_output_ref": raw_output_ref,
            "raw_output_checksum": raw_output_checksum,
            "raw_output_chars": len(telemetry.raw_output)
            if telemetry.raw_output is not None
            else None,
        }
        await self._run_logs.append(
            task_id=task_id,
            record_type="MODEL_CALL",
            actor=context.actor_name,
            payload=payload,
        )
