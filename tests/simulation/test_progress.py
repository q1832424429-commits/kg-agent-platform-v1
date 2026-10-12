"""验证测试观察台的进度展示必须以持久化 MasterTask 状态为最高事实来源。"""

from uuid import uuid4

from kg_agent.api.schemas import DemoRunResponse, DemoSubTaskView
from kg_agent.domain.enums import MasterTaskStatus
from simulation_lab.app import _calculate_progress


def _observed_completed_task() -> DemoRunResponse:
    """构造两个已完成子任务的公开快照，不伪造协调器临时动作。"""

    task_id = uuid4()
    return DemoRunResponse(
        task_id=task_id,
        conversation_id=uuid4(),
        task_intent="START",
        requires_clarification=False,
        clarification_question=None,
        task_spec={},
        subtasks=[
            DemoSubTaskView(
                subtask_id=uuid4(),
                decomposition_revision=1,
                name="解析资料",
                subtask_type="document_parse",
                assigned_agent_type="document_parsing_agent",
                status="SUCCEEDED",
            ),
            DemoSubTaskView(
                subtask_id=uuid4(),
                decomposition_revision=1,
                name="生成结果",
                subtask_type="custom_business_task",
                assigned_agent_type="custom_agent",
                status="SUCCEEDED",
            ),
        ],
    )


def test_persisted_succeeded_status_overrides_missing_coordination_action() -> None:
    """刷新后协调器动作列表为空时，已完成任务仍必须稳定显示 100%。"""

    progress = _calculate_progress(
        _observed_completed_task(),
        coordination_actions=[],
        master_task_status=MasterTaskStatus.SUCCEEDED,
    )

    assert progress.percent == 100
    assert progress.stage == "目标已完成"
    assert progress.completed_subtasks == 2
