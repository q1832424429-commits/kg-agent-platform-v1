"""验证第二阶段冻结的确定性状态迁移规则。"""

import pytest

from kg_agent.domain.enums import AssignmentStatus, MasterTaskStatus, SubTaskStatus
from kg_agent.domain.state_transitions import (
    ensure_assignment_transition,
    ensure_master_task_transition,
    ensure_subtask_transition,
)
from kg_agent.exceptions import StateTransitionError


def test_allows_declared_subtask_transition() -> None:
    """依赖任务从 BLOCKED 到 READY 的合法迁移应通过。"""

    ensure_subtask_transition(SubTaskStatus.BLOCKED, SubTaskStatus.READY)


def test_rejects_terminal_subtask_transition() -> None:
    """成功终态不能重新回到 RUNNING。"""

    with pytest.raises(StateTransitionError):
        ensure_subtask_transition(SubTaskStatus.SUCCEEDED, SubTaskStatus.RUNNING)


def test_assignment_must_be_claimed_before_running() -> None:
    """PENDING Assignment 不能跳过认领直接运行。"""

    with pytest.raises(StateTransitionError):
        ensure_assignment_transition(AssignmentStatus.PENDING, AssignmentStatus.RUNNING)


def test_master_task_can_wait_for_human() -> None:
    """活动任务可以进入等待人工审核状态。"""

    ensure_master_task_transition(MasterTaskStatus.ACTIVE, MasterTaskStatus.WAITING_HUMAN)
