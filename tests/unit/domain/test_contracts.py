"""验证核心 Pydantic 契约的字段约束和跨字段规则。"""

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from pydantic import ValidationError

from kg_agent.domain.agent_models import AgentRunResult, TaskDecompositionResult
from kg_agent.domain.enums import AgentRunStatus, TaskIntent
from kg_agent.domain.schema_export import export_all_contract_schemas, get_contract_schema
from tests.factories import make_task_spec


def test_task_spec_rejects_zero_revision() -> None:
    """TaskSpec revision 必须从 1 开始。"""

    task_id = uuid4()
    payload = make_task_spec(task_id).model_dump()
    payload["revision"] = 0

    with pytest.raises(ValidationError):
        make_task_spec(task_id).model_validate(payload)


def test_task_intent_rejects_unknown_value() -> None:
    """顶层任务意图不能超出 START、CONTINUE、MODIFY。"""

    with pytest.raises(ValueError):
        TaskIntent("QUERY")


def test_clarification_requires_question() -> None:
    """请求澄清时必须提供可展示给用户的问题。"""

    task_id = uuid4()
    with pytest.raises(ValidationError):
        TaskDecompositionResult(
            task_intent=TaskIntent.CONTINUE,
            task_spec=make_task_spec(task_id, TaskIntent.CONTINUE),
            subtasks=[],
            requires_clarification=True,
        )


def test_successful_agent_result_requires_output_artifact() -> None:
    """专业 Agent 报告 SUCCESS 时至少应引用一个输出 Artifact。"""

    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        AgentRunResult(
            task_id=uuid4(),
            subtask_id=uuid4(),
            assignment_id=uuid4(),
            agent_name="knowledge_extraction_agent",
            run_id=uuid4(),
            status=AgentRunStatus.SUCCESS,
            execution_trace_id=uuid4(),
            started_at=now,
            finished_at=now,
        )


def test_needs_user_input_requires_question() -> None:
    """专业 Agent 请求用户输入时必须说明问题。"""

    now = datetime.now(UTC)
    with pytest.raises(ValidationError):
        AgentRunResult(
            task_id=uuid4(),
            subtask_id=uuid4(),
            assignment_id=uuid4(),
            agent_name="knowledge_extraction_agent",
            run_id=uuid4(),
            status=AgentRunStatus.NEEDS_USER_INPUT,
            execution_trace_id=uuid4(),
            started_at=now,
            finished_at=now,
        )


def test_all_contracts_can_export_json_schema() -> None:
    """全部核心契约都必须能够稳定生成 JSON Schema。"""

    schemas = export_all_contract_schemas()

    assert "TaskSpec" in schemas
    assert "AgentSubTask" in schemas
    assert "HumanReviewRequest" in schemas
    assert get_contract_schema("TaskSpec")["title"] == "TaskSpec"
