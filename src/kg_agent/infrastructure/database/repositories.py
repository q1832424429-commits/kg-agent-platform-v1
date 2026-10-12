"""实现任务、Assignment、SharedContext 与 Agent 运行结果 Repository。

本模块只执行查询和行级持久化，不决定状态迁移、重试或角色权限。
"""

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Literal, cast
from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import (
    AssignmentStatus,
    HumanReviewStatus,
    MasterTaskStatus,
    SubTaskStatus,
    TaskEventType,
)
from kg_agent.domain.event_models import TaskEvent
from kg_agent.domain.review_models import HumanReviewRequest
from kg_agent.domain.task_models import AgentSubTask, MasterTask, TaskSpec
from kg_agent.domain.trace_models import ExecutionTrace
from kg_agent.exceptions import PersistenceError
from kg_agent.infrastructure.database.mappers import (
    to_artifact_ref,
    to_assignment,
    to_master_task,
    to_subtask,
    to_task_spec,
)
from kg_agent.infrastructure.database.models import (
    AgentAssignmentRow,
    AgentRunResultRow,
    AgentSubTaskRow,
    ArtifactRefRow,
    DecompositionRevisionRow,
    ExecutionTraceRow,
    HumanReviewRequestRow,
    MasterTaskRow,
    SharedContextSnapshotRow,
    SubTaskDependencyRow,
    TaskEventRow,
    TaskSpecRevisionRow,
)


class SqlAlchemyTaskRepository:
    """使用一个调用方管理的 AsyncSession 保存任务事实。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前事务使用的异步 Session。"""

        self._session = session

    async def get_master(self, task_id: UUID, *, for_update: bool = False) -> MasterTask | None:
        """读取 MasterTask；写命令可请求数据库行锁。"""

        query = select(MasterTaskRow).where(MasterTaskRow.task_id == task_id)
        if for_update:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        return to_master_task(row) if row else None

    async def add_master(self, master_task: MasterTask) -> None:
        """插入新的 MasterTask。"""

        self._session.add(
            MasterTaskRow(
                task_id=master_task.task_id,
                conversation_id=master_task.conversation_id,
                workspace_id=master_task.workspace_id,
                user_id=master_task.user_id,
                title=master_task.title,
                current_task_spec_revision=master_task.current_task_spec_revision,
                current_decomposition_revision=master_task.current_decomposition_revision,
                status=master_task.status.value,
                created_at=master_task.created_at,
                updated_at=master_task.updated_at,
                completed_at=master_task.completed_at,
            )
        )
        await self._session.flush()

    async def update_master(self, master_task: MasterTask) -> None:
        """更新 MasterTask 当前指针和状态，不操作历史 revision。"""

        row = await self._session.get(MasterTaskRow, master_task.task_id)
        if row is None:
            raise PersistenceError("MasterTask 不存在")
        row.title = master_task.title
        row.current_task_spec_revision = master_task.current_task_spec_revision
        row.current_decomposition_revision = master_task.current_decomposition_revision
        row.status = master_task.status.value
        row.updated_at = master_task.updated_at
        row.completed_at = master_task.completed_at
        await self._session.flush()

    async def get_task_spec(self, task_id: UUID, revision: int) -> TaskSpec | None:
        """读取指定 TaskSpec revision。"""

        row = await self._session.get(TaskSpecRevisionRow, (task_id, revision))
        return to_task_spec(row) if row else None

    async def add_task_spec(self, task_spec: TaskSpec) -> None:
        """插入不可变 TaskSpec revision。"""

        self._session.add(
            TaskSpecRevisionRow(
                task_id=task_spec.task_id,
                revision=task_spec.revision,
                task_intent=task_spec.task_intent.value,
                payload=task_spec.model_dump(mode="json"),
                created_at=task_spec.created_at,
            )
        )
        await self._session.flush()

    async def list_task_spec_revisions(self, task_id: UUID) -> list[int]:
        """按升序返回 TaskSpec revision。"""

        values = await self._session.scalars(
            select(TaskSpecRevisionRow.revision)
            .where(TaskSpecRevisionRow.task_id == task_id)
            .order_by(TaskSpecRevisionRow.revision)
        )
        return list(values)

    async def decomposition_exists(self, task_id: UUID, revision: int) -> bool:
        """检查拆解 revision 是否已经存在。"""

        row = await self._session.get(DecompositionRevisionRow, (task_id, revision))
        return row is not None

    async def add_decomposition(
        self,
        *,
        task_id: UUID,
        revision: int,
        task_spec_revision: int,
        task_intent: str,
        created_at: datetime,
    ) -> None:
        """插入不可变拆解 revision。"""

        self._session.add(
            DecompositionRevisionRow(
                task_id=task_id,
                revision=revision,
                task_spec_revision=task_spec_revision,
                task_intent=task_intent,
                created_at=created_at,
            )
        )
        await self._session.flush()

    async def get_subtask(
        self, subtask_id: UUID, *, for_update: bool = False
    ) -> AgentSubTask | None:
        """读取子任务；状态命令可请求行锁。"""

        query = select(AgentSubTaskRow).where(AgentSubTaskRow.subtask_id == subtask_id)
        if for_update:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        return to_subtask(row) if row else None

    async def list_subtasks(self, task_id: UUID) -> list[AgentSubTask]:
        """按创建时间和 ID 稳定返回任务子任务。"""

        rows = await self._session.scalars(
            select(AgentSubTaskRow)
            .where(AgentSubTaskRow.master_task_id == task_id)
            .order_by(AgentSubTaskRow.created_at, AgentSubTaskRow.subtask_id)
        )
        return [to_subtask(row) for row in rows]

    async def add_subtask(self, subtask: AgentSubTask) -> None:
        """插入子任务计划；依赖必须在全部子任务写入后单独追加。"""

        self._session.add(
            AgentSubTaskRow(
                subtask_id=subtask.subtask_id,
                master_task_id=subtask.master_task_id,
                decomposition_revision=subtask.decomposition_revision,
                subtask_type=subtask.subtask_type,
                assigned_agent_type=subtask.assigned_agent_type,
                priority=subtask.priority,
                status=subtask.status.value,
                human_review_required=subtask.human_review_required,
                retry_max_attempts=subtask.retry_policy.max_attempts,
                payload=subtask.model_dump(mode="json"),
                created_at=subtask.created_at,
                updated_at=subtask.created_at,
            )
        )
        await self._session.flush()

    async def add_dependencies(self, subtask_id: UUID, dependency_ids: Sequence[UUID]) -> None:
        """追加规范化依赖边。"""

        self._session.add_all(
            [
                SubTaskDependencyRow(
                    subtask_id=subtask_id,
                    dependency_subtask_id=dependency_id,
                )
                for dependency_id in dependency_ids
            ]
        )
        await self._session.flush()

    async def update_subtask_status(
        self, subtask_id: UUID, status: SubTaskStatus, updated_at: datetime
    ) -> None:
        """只更新子任务当前状态列。"""

        row = await self._session.get(AgentSubTaskRow, subtask_id)
        if row is None:
            raise PersistenceError("AgentSubTask 不存在")
        row.status = status.value
        row.updated_at = updated_at
        await self._session.flush()

    async def list_dependency_statuses(self, subtask_id: UUID) -> list[SubTaskStatus]:
        """查询直接依赖的当前状态。"""

        values = await self._session.scalars(
            select(AgentSubTaskRow.status)
            .join(
                SubTaskDependencyRow,
                SubTaskDependencyRow.dependency_subtask_id == AgentSubTaskRow.subtask_id,
            )
            .where(SubTaskDependencyRow.subtask_id == subtask_id)
        )
        return [SubTaskStatus(value) for value in values]

    async def list_dependents(self, subtask_id: UUID) -> list[AgentSubTask]:
        """查询直接下游子任务。"""

        rows = await self._session.scalars(
            select(AgentSubTaskRow)
            .join(
                SubTaskDependencyRow,
                SubTaskDependencyRow.subtask_id == AgentSubTaskRow.subtask_id,
            )
            .where(SubTaskDependencyRow.dependency_subtask_id == subtask_id)
        )
        return [to_subtask(row) for row in rows]

    async def add_artifact_ref(self, artifact: ArtifactRef) -> None:
        """插入轻量 ArtifactRef；完整生命周期由第三阶段实现。"""

        self._session.add(
            ArtifactRefRow(
                artifact_id=artifact.artifact_id,
                task_id=artifact.task_id,
                artifact_type=artifact.artifact_type,
                status=artifact.status.value,
                payload=artifact.model_dump(mode="json"),
                created_at=artifact.created_at,
            )
        )
        await self._session.flush()

    async def get_artifact_ref(self, artifact_id: UUID) -> ArtifactRef | None:
        """读取轻量 ArtifactRef。"""

        row = await self._session.get(ArtifactRefRow, artifact_id)
        return to_artifact_ref(row) if row else None

    async def list_artifact_refs(self, task_id: UUID) -> list[ArtifactRef]:
        """返回任务 ArtifactRef。"""

        rows = await self._session.scalars(
            select(ArtifactRefRow)
            .where(ArtifactRefRow.task_id == task_id)
            .order_by(ArtifactRefRow.created_at, ArtifactRefRow.artifact_id)
        )
        return [to_artifact_ref(row) for row in rows]


class SqlAlchemyAssignmentRepository:
    """实现 Assignment 状态和 PostgreSQL 跳过锁行认领查询。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前事务使用的异步 Session。"""

        self._session = session

    async def add(self, assignment: AgentAssignment) -> None:
        """插入一次新的 Assignment 尝试。"""

        self._session.add(AgentAssignmentRow(**assignment.model_dump(mode="python")))
        await self._session.flush()

    async def get(self, assignment_id: UUID, *, for_update: bool = False) -> AgentAssignment | None:
        """读取 Assignment；租约命令可请求行锁。"""

        query = select(AgentAssignmentRow).where(AgentAssignmentRow.assignment_id == assignment_id)
        if for_update:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        return to_assignment(row) if row else None

    async def update(self, assignment: AgentAssignment) -> None:
        """更新 Assignment 当前租约和状态字段。"""

        row = await self._session.get(AgentAssignmentRow, assignment.assignment_id)
        if row is None:
            raise PersistenceError("AgentAssignment 不存在")
        for field in (
            "claimed_by",
            "lease_expires_at",
            "heartbeat_at",
            "fencing_token",
            "started_at",
            "finished_at",
        ):
            setattr(row, field, getattr(assignment, field))
        row.status = assignment.status.value
        await self._session.flush()

    async def claim_next_pending(
        self, agent_type: str, *, task_id: UUID | None = None
    ) -> AgentAssignment | None:
        """锁定匹配 Agent 类型及可选任务范围的待认领 Assignment。"""

        query = (
            select(AgentAssignmentRow)
            .join(AgentSubTaskRow, AgentSubTaskRow.subtask_id == AgentAssignmentRow.subtask_id)
            .join(MasterTaskRow, MasterTaskRow.task_id == AgentSubTaskRow.master_task_id)
            .where(
                AgentAssignmentRow.assigned_agent_type == agent_type,
                AgentAssignmentRow.status == AssignmentStatus.PENDING.value,
                AgentSubTaskRow.status == SubTaskStatus.ASSIGNED.value,
                MasterTaskRow.status == MasterTaskStatus.ACTIVE.value,
            )
            .order_by(
                AgentSubTaskRow.priority.desc(),
                AgentAssignmentRow.created_at,
                AgentAssignmentRow.assignment_id,
            )
            .limit(1)
            .with_for_update(skip_locked=True)
        )
        if task_id is not None:
            query = query.where(AgentSubTaskRow.master_task_id == task_id)
        row = await self._session.scalar(query)
        return to_assignment(row) if row else None

    async def list_for_task(self, task_id: UUID) -> list[AgentAssignment]:
        """按尝试时间返回任务的 Assignment。"""

        rows = await self._session.scalars(
            select(AgentAssignmentRow)
            .join(AgentSubTaskRow, AgentSubTaskRow.subtask_id == AgentAssignmentRow.subtask_id)
            .where(AgentSubTaskRow.master_task_id == task_id)
            .order_by(
                AgentAssignmentRow.created_at,
                AgentAssignmentRow.assignment_id,
            )
        )
        return [to_assignment(row) for row in rows]

    async def list_expired(self, now: datetime, limit: int) -> list[AgentAssignment]:
        """锁定超过租约的 CLAIMED/RUNNING Assignment。"""

        rows = await self._session.scalars(
            select(AgentAssignmentRow)
            .where(
                AgentAssignmentRow.status.in_(
                    [AssignmentStatus.CLAIMED.value, AssignmentStatus.RUNNING.value]
                ),
                AgentAssignmentRow.lease_expires_at < now,
            )
            .order_by(AgentAssignmentRow.lease_expires_at)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return [to_assignment(row) for row in rows]


class SqlAlchemyAgentRunRepository:
    """不可变保存 AgentRunResult 和 ExecutionTrace JSON 契约。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定调用方事务 Session。"""

        self._session = session

    async def add_result(self, result: AgentRunResult) -> None:
        """插入运行结果；run_id 和 assignment_id 都具备唯一约束。"""

        self._session.add(
            AgentRunResultRow(
                run_id=result.run_id,
                task_id=result.task_id,
                subtask_id=result.subtask_id,
                assignment_id=result.assignment_id,
                agent_name=result.agent_name,
                status=result.status.value,
                payload=result.model_dump(mode="json"),
                started_at=result.started_at,
                finished_at=result.finished_at,
            )
        )
        await self._session.flush()

    async def get_result(self, run_id: UUID) -> AgentRunResult | None:
        """按 run_id 读取并恢复公共契约。"""

        row = await self._session.get(AgentRunResultRow, run_id)
        return AgentRunResult.model_validate(row.payload) if row else None

    async def list_results(self, task_id: UUID) -> list[AgentRunResult]:
        """按完成时间稳定返回任务运行结果。"""

        rows = await self._session.scalars(
            select(AgentRunResultRow)
            .where(AgentRunResultRow.task_id == task_id)
            .order_by(AgentRunResultRow.finished_at, AgentRunResultRow.run_id)
        )
        return [AgentRunResult.model_validate(row.payload) for row in rows]

    async def add_trace(self, trace: ExecutionTrace) -> None:
        """插入不含隐藏思维过程的公开执行轨迹。"""

        self._session.add(
            ExecutionTraceRow(
                trace_id=trace.trace_id,
                task_id=trace.task_id,
                subtask_id=trace.subtask_id,
                assignment_id=trace.assignment_id,
                agent_run_id=trace.agent_run_id,
                status=trace.status,
                payload=trace.model_dump(mode="json"),
                started_at=trace.started_at,
                finished_at=trace.finished_at,
            )
        )
        await self._session.flush()

    async def get_trace(self, trace_id: UUID) -> ExecutionTrace | None:
        """按 trace_id 读取执行轨迹。"""

        row = await self._session.get(ExecutionTraceRow, trace_id)
        return ExecutionTrace.model_validate(row.payload) if row else None


class SqlAlchemyHumanReviewRepository:
    """持久化审核请求，并保护目标 Artifact 与 checksum 不可被更新。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由调用方管理事务的异步 Session。"""

        self._session = session

    async def add(self, review: HumanReviewRequest) -> None:
        """插入待处理审核请求的完整契约快照。"""

        self._session.add(
            HumanReviewRequestRow(
                review_id=review.review_id,
                task_id=review.task_id,
                subtask_id=review.subtask_id,
                target_type=review.target_type.value,
                target_artifact_id=review.target_artifact_id,
                target_artifact_checksum=review.target_artifact_checksum,
                status=review.status.value,
                decision_artifact_id=review.decision_artifact_id,
                reviewer_feedback=review.reviewer_feedback,
                payload=review.model_dump(mode="json"),
                requested_at=review.requested_at,
                resolved_at=review.resolved_at,
            )
        )
        await self._session.flush()

    async def get(self, review_id: UUID, *, for_update: bool = False) -> HumanReviewRequest | None:
        """读取审核请求；处理用户决策时可以获取行锁。"""

        query = select(HumanReviewRequestRow).where(HumanReviewRequestRow.review_id == review_id)
        if for_update:
            query = query.with_for_update()
        row = await self._session.scalar(query)
        return HumanReviewRequest.model_validate(row.payload) if row else None

    async def update(self, review: HumanReviewRequest) -> None:
        """更新审核终态，同时拒绝目标、附件和允许操作被替换。"""

        row = await self._session.get(HumanReviewRequestRow, review.review_id)
        if row is None:
            raise PersistenceError("HumanReviewRequest 不存在")
        existing = HumanReviewRequest.model_validate(row.payload)
        immutable_fields_match = (
            existing.task_id == review.task_id
            and existing.subtask_id == review.subtask_id
            and existing.target_type == review.target_type
            and existing.target_artifact_id == review.target_artifact_id
            and existing.target_artifact_checksum == review.target_artifact_checksum
            and existing.quality_report_artifact_ids == review.quality_report_artifact_ids
            and existing.conflict_report_artifact_ids == review.conflict_report_artifact_ids
            and existing.evidence_artifact_ids == review.evidence_artifact_ids
            and existing.allowed_actions == review.allowed_actions
            and existing.requested_at == review.requested_at
        )
        if not immutable_fields_match:
            raise PersistenceError("HumanReviewRequest 审核目标和附件不可修改")
        row.status = review.status.value
        row.decision_artifact_id = review.decision_artifact_id
        row.reviewer_feedback = review.reviewer_feedback
        row.payload = review.model_dump(mode="json")
        row.resolved_at = review.resolved_at
        await self._session.flush()

    async def list_for_task(self, task_id: UUID) -> list[HumanReviewRequest]:
        """按请求时间和 ID 稳定返回任务全部审核记录。"""

        rows = await self._session.scalars(
            select(HumanReviewRequestRow)
            .where(HumanReviewRequestRow.task_id == task_id)
            .order_by(
                HumanReviewRequestRow.requested_at,
                HumanReviewRequestRow.review_id,
            )
        )
        return [HumanReviewRequest.model_validate(row.payload) for row in rows]

    async def find_pending(
        self,
        *,
        task_id: UUID,
        target_artifact_id: UUID,
        target_artifact_checksum: str,
    ) -> HumanReviewRequest | None:
        """查找同一目标版本的待处理请求，防止创建重复审核门禁。"""

        row = await self._session.scalar(
            select(HumanReviewRequestRow).where(
                HumanReviewRequestRow.task_id == task_id,
                HumanReviewRequestRow.target_artifact_id == target_artifact_id,
                HumanReviewRequestRow.target_artifact_checksum == target_artifact_checksum,
                HumanReviewRequestRow.status == HumanReviewStatus.PENDING.value,
            )
        )
        return HumanReviewRequest.model_validate(row.payload) if row else None

    async def get_by_decision_artifact(self, artifact_id: UUID) -> HumanReviewRequest | None:
        """按唯一决策 Artifact 找到对应审核记录。"""

        row = await self._session.scalar(
            select(HumanReviewRequestRow).where(
                HumanReviewRequestRow.decision_artifact_id == artifact_id
            )
        )
        return HumanReviewRequest.model_validate(row.payload) if row else None


class SqlAlchemyEventRepository:
    """实现数据库生成顺序号的只追加任务事件存储。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前事务使用的异步 Session。"""

        self._session = session

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
        """插入一个事件并在 flush 后取得全局 sequence。"""

        row = TaskEventRow(
            event_id=uuid4(),
            task_id=task_id,
            subtask_id=subtask_id,
            assignment_id=assignment_id,
            event_type=event_type.value,
            actor_type=actor_type,
            actor_id=actor_id,
            reason_code=reason_code,
            payload=payload or {},
            occurred_at=occurred_at,
        )
        self._session.add(row)
        await self._session.flush()
        valid_actor = cast(Literal["USER", "AGENT", "SERVICE", "WORKER"], actor_type)
        return TaskEvent(
            event_id=row.event_id,
            sequence=row.sequence,
            task_id=row.task_id,
            subtask_id=row.subtask_id,
            assignment_id=row.assignment_id,
            event_type=TaskEventType(row.event_type),
            actor_type=valid_actor,
            actor_id=row.actor_id,
            reason_code=row.reason_code,
            payload=row.payload,
            occurred_at=row.occurred_at,
        )

    async def list_for_task(self, task_id: UUID) -> Sequence[TaskEvent]:
        """按数据库 sequence 返回任务事件。"""

        rows = await self._session.scalars(
            select(TaskEventRow)
            .where(TaskEventRow.task_id == task_id)
            .order_by(TaskEventRow.sequence)
        )
        return [
            TaskEvent(
                event_id=row.event_id,
                sequence=row.sequence,
                task_id=row.task_id,
                subtask_id=row.subtask_id,
                assignment_id=row.assignment_id,
                event_type=TaskEventType(row.event_type),
                actor_type=cast(Literal["USER", "AGENT", "SERVICE", "WORKER"], row.actor_type),
                actor_id=row.actor_id,
                reason_code=row.reason_code,
                payload=row.payload,
                occurred_at=row.occurred_at,
            )
            for row in rows
        ]


class SqlAlchemyContextRepository:
    """实现 SharedTaskContext 不可变版本快照存储。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定当前事务使用的异步 Session。"""

        self._session = session

    async def get(self, task_id: UUID) -> SharedTaskContext | None:
        """读取指定任务最新版本快照。"""

        row = await self._session.scalar(
            select(SharedContextSnapshotRow)
            .where(SharedContextSnapshotRow.task_id == task_id)
            .order_by(SharedContextSnapshotRow.version.desc())
            .limit(1)
        )
        return SharedTaskContext.model_validate(row.payload) if row else None

    async def add_snapshot(self, context: SharedTaskContext) -> None:
        """追加不可变 SharedContext 快照。"""

        self._session.add(
            SharedContextSnapshotRow(
                task_id=context.task_id,
                version=context.version,
                payload=context.model_dump(mode="json"),
                created_at=context.updated_at,
            )
        )
        await self._session.flush()
