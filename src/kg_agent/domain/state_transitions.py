"""定义不依赖 LLM 和数据库的确定性状态迁移规则。"""

from collections.abc import Mapping

from kg_agent.domain.enums import AssignmentStatus, MasterTaskStatus, SubTaskStatus
from kg_agent.exceptions import StateTransitionError

SUBTASK_TRANSITIONS: Mapping[SubTaskStatus, frozenset[SubTaskStatus]] = {
    SubTaskStatus.PENDING: frozenset(
        {
            SubTaskStatus.BLOCKED,
            SubTaskStatus.READY,
            SubTaskStatus.CANCELLED,
            SubTaskStatus.SUPERSEDED,
        }
    ),
    SubTaskStatus.BLOCKED: frozenset(
        {SubTaskStatus.READY, SubTaskStatus.CANCELLED, SubTaskStatus.SUPERSEDED}
    ),
    SubTaskStatus.READY: frozenset(
        {SubTaskStatus.ASSIGNED, SubTaskStatus.CANCELLED, SubTaskStatus.SUPERSEDED}
    ),
    SubTaskStatus.ASSIGNED: frozenset({SubTaskStatus.CLAIMED, SubTaskStatus.CANCELLED}),
    SubTaskStatus.CLAIMED: frozenset(
        {
            SubTaskStatus.RUNNING,
            SubTaskStatus.ASSIGNED,
            SubTaskStatus.FAILED,
            SubTaskStatus.CANCELLED,
        }
    ),
    SubTaskStatus.RUNNING: frozenset(
        {
            SubTaskStatus.WAITING_HUMAN,
            SubTaskStatus.SUCCEEDED,
            SubTaskStatus.FAILED,
            SubTaskStatus.CANCELLED,
        }
    ),
    SubTaskStatus.WAITING_HUMAN: frozenset(
        {
            SubTaskStatus.READY,
            SubTaskStatus.SUCCEEDED,
            SubTaskStatus.FAILED,
            SubTaskStatus.CANCELLED,
        }
    ),
    SubTaskStatus.SUCCEEDED: frozenset(),
    SubTaskStatus.FAILED: frozenset(),
    SubTaskStatus.CANCELLED: frozenset(),
    SubTaskStatus.SUPERSEDED: frozenset(),
}

ASSIGNMENT_TRANSITIONS: Mapping[AssignmentStatus, frozenset[AssignmentStatus]] = {
    AssignmentStatus.PENDING: frozenset({AssignmentStatus.CLAIMED, AssignmentStatus.CANCELLED}),
    AssignmentStatus.CLAIMED: frozenset(
        {AssignmentStatus.RUNNING, AssignmentStatus.EXPIRED, AssignmentStatus.CANCELLED}
    ),
    AssignmentStatus.RUNNING: frozenset(
        {
            AssignmentStatus.SUCCEEDED,
            AssignmentStatus.FAILED,
            AssignmentStatus.EXPIRED,
            AssignmentStatus.CANCELLED,
        }
    ),
    AssignmentStatus.SUCCEEDED: frozenset(),
    AssignmentStatus.FAILED: frozenset(),
    AssignmentStatus.EXPIRED: frozenset(),
    AssignmentStatus.CANCELLED: frozenset(),
}

MASTER_TASK_TRANSITIONS: Mapping[MasterTaskStatus, frozenset[MasterTaskStatus]] = {
    MasterTaskStatus.ACTIVE: frozenset(
        {
            MasterTaskStatus.WAITING_USER,
            MasterTaskStatus.WAITING_HUMAN,
            MasterTaskStatus.SUCCEEDED,
            MasterTaskStatus.FAILED,
            MasterTaskStatus.CANCELLED,
        }
    ),
    MasterTaskStatus.WAITING_USER: frozenset(
        {MasterTaskStatus.ACTIVE, MasterTaskStatus.FAILED, MasterTaskStatus.CANCELLED}
    ),
    MasterTaskStatus.WAITING_HUMAN: frozenset(
        {MasterTaskStatus.ACTIVE, MasterTaskStatus.FAILED, MasterTaskStatus.CANCELLED}
    ),
    MasterTaskStatus.SUCCEEDED: frozenset(),
    MasterTaskStatus.FAILED: frozenset(),
    MasterTaskStatus.CANCELLED: frozenset(),
}


def ensure_subtask_transition(current: SubTaskStatus, target: SubTaskStatus) -> None:
    """校验子任务状态迁移；非法跳转时抛出 StateTransitionError。"""

    if target not in SUBTASK_TRANSITIONS[current]:
        raise StateTransitionError(f"非法子任务状态迁移：{current.value} -> {target.value}")


def ensure_assignment_transition(current: AssignmentStatus, target: AssignmentStatus) -> None:
    """校验 Assignment 状态迁移；非法跳转时抛出 StateTransitionError。"""

    if target not in ASSIGNMENT_TRANSITIONS[current]:
        raise StateTransitionError(f"非法 Assignment 状态迁移：{current.value} -> {target.value}")


def ensure_master_task_transition(current: MasterTaskStatus, target: MasterTaskStatus) -> None:
    """校验 MasterTask 状态迁移；非法跳转时抛出 StateTransitionError。"""

    if target not in MASTER_TASK_TRANSITIONS[current]:
        raise StateTransitionError(f"非法 MasterTask 状态迁移：{current.value} -> {target.value}")


def ensure_subtask_lease_recovery(current: SubTaskStatus) -> None:
    """校验租约恢复只允许把 CLAIMED 或 RUNNING 子任务重新置为 ASSIGNED。"""

    if current not in {SubTaskStatus.CLAIMED, SubTaskStatus.RUNNING}:
        raise StateTransitionError(f"{current.value} 状态的子任务不能执行租约恢复")
