"""定义服务层可依赖的数据库无关 Repository 协议。"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol
from uuid import UUID

from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.artifact_models import ArtifactRecord, ArtifactRef
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import ArtifactStatus, SubTaskStatus, TaskEventType
from kg_agent.domain.event_models import TaskEvent
from kg_agent.domain.review_models import HumanReviewRequest
from kg_agent.domain.task_models import AgentSubTask, MasterTask, TaskSpec
from kg_agent.domain.trace_models import ExecutionTrace


class TaskRepository(Protocol):
    """约束任务、revision、子任务和依赖的持久化操作。"""

    async def get_master(self, task_id: UUID, *, for_update: bool = False) -> MasterTask | None:
        """读取 MasterTask，可选择获取数据库行锁。"""
        ...

    async def add_master(self, master_task: MasterTask) -> None:
        """新增 MasterTask。"""
        ...

    async def update_master(self, master_task: MasterTask) -> None:
        """更新 MasterTask 当前状态指针。"""
        ...

    async def get_task_spec(self, task_id: UUID, revision: int) -> TaskSpec | None:
        """读取指定且不可变的 TaskSpec revision。"""
        ...

    async def add_task_spec(self, task_spec: TaskSpec) -> None:
        """新增 TaskSpec revision。"""
        ...

    async def list_task_spec_revisions(self, task_id: UUID) -> list[int]:
        """返回任务全部 TaskSpec revision。"""
        ...

    async def decomposition_exists(self, task_id: UUID, revision: int) -> bool:
        """判断任务拆解 revision 是否已经存在。"""
        ...

    async def add_decomposition(
        self,
        *,
        task_id: UUID,
        revision: int,
        task_spec_revision: int,
        task_intent: str,
        created_at: datetime,
    ) -> None:
        """新增不可变的拆解 revision。"""
        ...

    async def get_subtask(
        self, subtask_id: UUID, *, for_update: bool = False
    ) -> AgentSubTask | None:
        """读取子任务，可选择获取数据库行锁。"""
        ...

    async def list_subtasks(self, task_id: UUID) -> list[AgentSubTask]:
        """返回任务全部子任务。"""
        ...

    async def add_subtask(self, subtask: AgentSubTask) -> None:
        """新增子任务及其规范化依赖。"""
        ...

    async def add_dependencies(self, subtask_id: UUID, dependency_ids: Sequence[UUID]) -> None:
        """在同一拆解的所有子任务写入后追加规范化依赖。"""
        ...

    async def update_subtask_status(
        self, subtask_id: UUID, status: SubTaskStatus, updated_at: datetime
    ) -> None:
        """更新子任务当前状态，不覆盖原始计划内容。"""
        ...

    async def list_dependency_statuses(self, subtask_id: UUID) -> list[SubTaskStatus]:
        """返回直接依赖子任务的当前状态。"""
        ...

    async def list_dependents(self, subtask_id: UUID) -> list[AgentSubTask]:
        """返回直接依赖指定子任务的下游任务。"""
        ...

    async def add_artifact_ref(self, artifact: ArtifactRef) -> None:
        """保存 SharedContext 所需的轻量 ArtifactRef。"""
        ...

    async def get_artifact_ref(self, artifact_id: UUID) -> ArtifactRef | None:
        """读取一个 ArtifactRef。"""
        ...

    async def list_artifact_refs(self, task_id: UUID) -> list[ArtifactRef]:
        """返回任务当前引用的全部 ArtifactRef。"""
        ...


class AssignmentRepository(Protocol):
    """约束 Assignment 尝试、租约和原子认领持久化操作。"""

    async def add(self, assignment: AgentAssignment) -> None:
        """新增一次 Assignment 尝试。"""
        ...

    async def get(self, assignment_id: UUID, *, for_update: bool = False) -> AgentAssignment | None:
        """读取 Assignment，可选择获取数据库行锁。"""
        ...

    async def update(self, assignment: AgentAssignment) -> None:
        """保存 Assignment 当前租约和状态。"""
        ...

    async def claim_next_pending(
        self, agent_type: str, *, task_id: UUID | None = None
    ) -> AgentAssignment | None:
        """选择匹配角色及可选任务范围的待认领 Assignment。"""
        ...

    async def list_for_task(self, task_id: UUID) -> list[AgentAssignment]:
        """返回任务全部 Assignment 尝试。"""
        ...

    async def list_expired(self, now: datetime, limit: int) -> list[AgentAssignment]:
        """锁定并返回已经超过租约的执行中 Assignment。"""
        ...


class AgentRunRepository(Protocol):
    """约束 AgentRunResult 和对应 ExecutionTrace 的不可变持久化。"""

    async def add_result(self, result: AgentRunResult) -> None:
        """新增一次 AgentRunResult。"""
        ...

    async def get_result(self, run_id: UUID) -> AgentRunResult | None:
        """读取指定运行结果。"""
        ...

    async def list_results(self, task_id: UUID) -> list[AgentRunResult]:
        """返回任务全部专业 Agent 运行结果。"""
        ...

    async def add_trace(self, trace: ExecutionTrace) -> None:
        """新增与运行结果关联的公开执行轨迹。"""
        ...

    async def get_trace(self, trace_id: UUID) -> ExecutionTrace | None:
        """读取指定执行轨迹。"""
        ...


class HumanReviewRepository(Protocol):
    """约束人工审核请求的不可变目标与确定性状态更新。"""

    async def add(self, review: HumanReviewRequest) -> None:
        """新增一个待处理人工审核请求。"""
        ...

    async def get(self, review_id: UUID, *, for_update: bool = False) -> HumanReviewRequest | None:
        """读取审核请求，可选择锁定当前状态行。"""
        ...

    async def update(self, review: HumanReviewRequest) -> None:
        """只更新审核状态、决策 Artifact 和用户反馈。"""
        ...

    async def list_for_task(self, task_id: UUID) -> list[HumanReviewRequest]:
        """按请求时间返回任务全部审核记录。"""
        ...

    async def find_pending(
        self,
        *,
        task_id: UUID,
        target_artifact_id: UUID,
        target_artifact_checksum: str,
    ) -> HumanReviewRequest | None:
        """查找同一目标版本尚未处理的审核请求。"""
        ...

    async def get_by_decision_artifact(self, artifact_id: UUID) -> HumanReviewRequest | None:
        """读取产生指定 Approval 或 ChangeSet 的审核记录。"""
        ...


class EventRepository(Protocol):
    """约束只能追加的任务审计事件操作。"""

    async def append(
        self,
        *,
        task_id: UUID,
        event_type: TaskEventType,
        actor_type: str,
        actor_id: str,
        occurred_at: datetime,
        subtask_id: UUID | None = None,
        assignment_id: UUID | None = None,
        reason_code: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> TaskEvent:
        """追加一个事件并返回数据库生成的全局顺序号。"""
        ...

    async def list_for_task(self, task_id: UUID) -> Sequence[TaskEvent]:
        """按顺序返回任务事件。"""
        ...


class ContextRepository(Protocol):
    """约束 SharedContext 不可变快照的读写操作。"""

    async def get(self, task_id: UUID) -> SharedTaskContext | None:
        """读取任务最新快照。"""
        ...

    async def add_snapshot(self, context: SharedTaskContext) -> None:
        """追加一个新的 SharedContext 版本。"""
        ...


class ArtifactRepository(Protocol):
    """约束 Artifact 元数据、对象定位和规范化血缘持久化操作。"""

    async def add(self, record: ArtifactRecord) -> None:
        """新增不可变 Artifact 记录和直接父级血缘。"""
        ...

    async def get(self, artifact_id: UUID, *, for_update: bool = False) -> ArtifactRecord | None:
        """读取 Artifact 完整记录，可选择锁定元数据行。"""
        ...

    async def list_for_task(self, task_id: UUID) -> list[ArtifactRecord]:
        """返回任务全部 Artifact 记录。"""
        ...

    async def get_parent_refs(self, artifact_id: UUID) -> list[ArtifactRef]:
        """返回 Artifact 的直接父级引用。"""
        ...

    async def update_status(self, artifact_id: UUID, status: ArtifactStatus) -> None:
        """更新 Artifact 生命周期状态，不修改内容。"""
        ...

    async def task_usage(self, task_id: UUID, artifact_type: str | None = None) -> tuple[int, int]:
        """返回任务中指定类型或全部 Artifact 的数量和总字节数。"""
        ...
