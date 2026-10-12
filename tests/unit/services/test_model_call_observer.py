"""验证模型调用遥测只写入既有任务日志且不包含 Prompt 正文。"""

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from kg_agent.model_gateway.base import ModelCallContext, ModelCallTelemetry
from kg_agent.services.model_call_observer import RunLogModelCallObserver
from kg_agent.services.run_log_service import RunLogService


@pytest.mark.asyncio
async def test_model_call_observer_writes_sanitized_task_metrics(tmp_path) -> None:
    """模型遥测应保存 Skill 原始回答引用，但不把正文塞入公开 JSONL。"""

    task_id = uuid4()
    now = datetime.now(UTC)
    observer = RunLogModelCallObserver(RunLogService(tmp_path))

    model_output = '{"outputs":[{"artifact_type":"EntityDataset"}]}'
    await observer.record(
        ModelCallTelemetry(
            call_id="call-1",
            context=ModelCallContext(
                component="SKILL",
                actor_name="knowledge_extraction_agent",
                task_id=task_id,
                skill_name="extract_knowledge",
            ),
            model_name="configured-model",
            endpoint_host="example.invalid",
            output_model_name="LLMSkillStructuredOutput",
            system_prompt_chars=120,
            user_prompt_chars=456,
            started_at=now,
            finished_at=now,
            duration_ms=12.5,
            status="SUCCEEDED",
            prompt_tokens=100,
            completion_tokens=20,
            total_tokens=120,
            raw_output=model_output,
        )
    )

    raw = await RunLogService(tmp_path).read(task_id)
    assert raw is not None
    record = json.loads(raw.decode("utf-8"))
    assert record["record_type"] == "MODEL_CALL"
    assert record["payload"]["skill_name"] == "extract_knowledge"
    assert record["payload"]["prompt_chars"] == 576
    assert "system_prompt" not in record["payload"]
    assert "user_prompt" not in record["payload"]
    assert "raw_output" not in record["payload"]
    assert record["payload"]["raw_output_chars"] == len(model_output)
    output_path = record["payload"]["raw_output_ref"]
    assert output_path is not None
    with open(output_path, encoding="utf-8") as output_file:
        assert output_file.read() == model_output
    assert record["payload"]["raw_output_checksum"].startswith("sha256:")


@pytest.mark.asyncio
async def test_model_call_observer_ignores_calls_without_task_context(tmp_path) -> None:
    """无法归属 MasterTask 的调用不得创建游离日志文件。"""

    observer = RunLogModelCallObserver(RunLogService(tmp_path))
    now = datetime.now(UTC)
    await observer.record(
        ModelCallTelemetry(
            call_id="call-no-task",
            context=ModelCallContext(component="CONTROLLER", actor_name="controller_agent"),
            model_name="configured-model",
            endpoint_host="example.invalid",
            output_model_name="TaskDecompositionResult",
            system_prompt_chars=1,
            user_prompt_chars=1,
            started_at=now,
            finished_at=now,
            duration_ms=1,
            status="FAILED",
            error_type="TimeoutError",
        )
    )
    assert list(tmp_path.iterdir()) == []
