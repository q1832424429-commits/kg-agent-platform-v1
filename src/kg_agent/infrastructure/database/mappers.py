"""在 SQLAlchemy ORM 行与公共 Pydantic 契约之间转换。"""

from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.task_models import AgentSubTask, MasterTask, TaskSpec
from kg_agent.infrastructure.database.models import (
    AgentAssignmentRow,
    AgentSubTaskRow,
    ArtifactRefRow,
    MasterTaskRow,
    TaskSpecRevisionRow,
)


def to_master_task(row: MasterTaskRow) -> MasterTask:
    """把 MasterTask ORM 行转换为领域契约。"""

    return MasterTask.model_validate(
        {
            "task_id": row.task_id,
            "conversation_id": row.conversation_id,
            "workspace_id": row.workspace_id,
            "user_id": row.user_id,
            "title": row.title,
            "current_task_spec_revision": row.current_task_spec_revision,
            "current_decomposition_revision": row.current_decomposition_revision,
            "status": row.status,
            "created_at": row.created_at,
            "updated_at": row.updated_at,
            "completed_at": row.completed_at,
        }
    )


def to_task_spec(row: TaskSpecRevisionRow) -> TaskSpec:
    """把不可变 TaskSpec JSON 快照恢复为领域契约。"""

    return TaskSpec.model_validate(row.payload)


def to_subtask(row: AgentSubTaskRow) -> AgentSubTask:
    """恢复子任务计划，并以关系列中的当前状态覆盖初始状态。"""

    payload = dict(row.payload)
    payload["status"] = row.status
    return AgentSubTask.model_validate(payload)


def to_assignment(row: AgentAssignmentRow) -> AgentAssignment:
    """把 Assignment ORM 行转换为领域契约。"""

    return AgentAssignment.model_validate(
        {
            "assignment_id": row.assignment_id,
            "subtask_id": row.subtask_id,
            "assigned_agent_type": row.assigned_agent_type,
            "claimed_by": row.claimed_by,
            "status": row.status,
            "lease_expires_at": row.lease_expires_at,
            "heartbeat_at": row.heartbeat_at,
            "fencing_token": row.fencing_token,
            "attempt": row.attempt,
            "started_at": row.started_at,
            "finished_at": row.finished_at,
            "created_at": row.created_at,
        }
    )


def to_artifact_ref(row: ArtifactRefRow) -> ArtifactRef:
    """把 ArtifactRef JSON 快照恢复为公共领域契约。"""

    payload = dict(row.payload)
    payload["status"] = row.status
    return ArtifactRef.model_validate(payload)
