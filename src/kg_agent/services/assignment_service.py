"""实现 Assignment 原子认领、租约、心跳、完成和过期恢复。

本服务只执行确定性并发控制，不运行专业 Agent 或业务 Skill。
"""

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import (
    AssignmentStatus,
    MasterTaskStatus,
    SubTaskStatus,
    TaskEventType,
)
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.domain.state_transitions import (
    ensure_assignment_transition,
    ensure_master_task_transition,
    ensure_subtask_lease_recovery,
    ensure_subtask_transition,
)
from kg_agent.domain.task_models import AgentSubTask
from kg_agent.exceptions import AssignmentConflictError, StateTransitionError
from kg_agent.repositories.interfaces import (
    AssignmentRepository,
    ContextRepository,
    EventRepository,
    TaskRepository,
)
from kg_agent.services.context_projection import build_context_projection

Clock = Callable[[], datetime]


class AssignmentService:
    """管理一个事务内的 Assignment 租约和安全结果提交。"""

    def __init__(
        self,
        *,
        task_repository: TaskRepository,
        assignment_repository: AssignmentRepository,
        context_repository: ContextRepository,
        event_repository: EventRepository,
        lease_seconds: int = 60,
        clock: Clock | None = None,
    ) -> None:
        """注入 Repository、租约长度和可测试时钟。"""

        if lease_seconds <= 0:
            raise ValueError("lease_seconds 必须大于 0")
        self._tasks = task_repository
        self._assignments = assignment_repository
        self._contexts = context_repository
        self._events = event_repository
        self._lease = timedelta(seconds=lease_seconds)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def claim_next(
        self,
        *,
        agent_type: str,
        worker_id: str,
        task_id: UUID | None = None,
    ) -> AgentAssignment | None:
        """原子认领一个匹配 Agent 类型的待处理 Assignment。

        PostgreSQL Repository 使用 ``FOR UPDATE SKIP LOCKED``，并在认领时增加 fencing
        token。没有匹配任务时返回 None。
        """

        assignment = await self._assignments.claim_next_pending(agent_type, task_id=task_id)
        if assignment is None:
            return None
        subtask = await self._tasks.get_subtask(assignment.subtask_id, for_update=True)
        if subtask is None or subtask.status is not SubTaskStatus.ASSIGNED:
            raise AssignmentConflictError("Assignment 对应子任务当前不可认领")
        if assignment.assigned_agent_type != agent_type:
            raise AssignmentConflictError("Agent 类型与 Assignment 不匹配")

        # 不同专业 Agent 可以同时选中各自的 Assignment，但它们在认领成功后
        # 都会追加同一 MasterTask 的 SharedContext 快照。如果不在读取最新版本前
        # 串行化这个很短的写入区间，两个事务可能同时由 v11 计算出 v12，
        # 最终触发 shared_context_snapshots 主键冲突。锁仅保护任务状态投影，
        # 不会把后续 Skill/大模型执行串行化。
        master = await self._tasks.get_master(subtask.master_task_id, for_update=True)
        if master is None:
            raise AssignmentConflictError("AgentAssignment 对应 MasterTask 不存在")
        if master.status is not MasterTaskStatus.ACTIVE:
            raise AssignmentConflictError("MasterTask 已暂停或终止，不能认领 Assignment")

        now = self._clock()
        ensure_assignment_transition(assignment.status, AssignmentStatus.CLAIMED)
        ensure_subtask_transition(subtask.status, SubTaskStatus.CLAIMED)
        claimed = assignment.model_copy(
            update={
                "claimed_by": worker_id,
                "status": AssignmentStatus.CLAIMED,
                "heartbeat_at": now,
                "lease_expires_at": now + self._lease,
                "fencing_token": assignment.fencing_token + 1,
            }
        )
        await self._assignments.update(claimed)
        await self._tasks.update_subtask_status(subtask.subtask_id, SubTaskStatus.CLAIMED, now)
        await self._append_event(
            task_id=subtask.master_task_id,
            subtask_id=subtask.subtask_id,
            assignment_id=assignment.assignment_id,
            event_type=TaskEventType.ASSIGNMENT_CLAIMED,
            worker_id=worker_id,
            now=now,
            payload={"fencing_token": claimed.fencing_token},
        )
        await self._create_snapshot(subtask.master_task_id, now)
        return claimed

    async def start(
        self, *, assignment_id: UUID, worker_id: str, fencing_token: int
    ) -> AgentAssignment:
        """把当前持有者的未过期 CLAIMED Assignment 转为 RUNNING。"""

        assignment, subtask, now = await self._get_owned_active(
            assignment_id, worker_id, fencing_token
        )
        ensure_assignment_transition(assignment.status, AssignmentStatus.RUNNING)
        ensure_subtask_transition(subtask.status, SubTaskStatus.RUNNING)
        running = assignment.model_copy(
            update={"status": AssignmentStatus.RUNNING, "started_at": now}
        )
        await self._assignments.update(running)
        await self._tasks.update_subtask_status(subtask.subtask_id, SubTaskStatus.RUNNING, now)
        await self._append_event(
            task_id=subtask.master_task_id,
            subtask_id=subtask.subtask_id,
            assignment_id=assignment_id,
            event_type=TaskEventType.ASSIGNMENT_STARTED,
            worker_id=worker_id,
            now=now,
        )
        await self._create_snapshot(subtask.master_task_id, now)
        return running

    async def heartbeat(
        self, *, assignment_id: UUID, worker_id: str, fencing_token: int
    ) -> AgentAssignment:
        """验证当前持有者和 fencing token 后延长 Assignment 租约。"""

        assignment, subtask, now = await self._get_owned_active(
            assignment_id, worker_id, fencing_token
        )
        renewed = assignment.model_copy(
            update={"heartbeat_at": now, "lease_expires_at": now + self._lease}
        )
        await self._assignments.update(renewed)
        await self._append_event(
            task_id=subtask.master_task_id,
            subtask_id=subtask.subtask_id,
            assignment_id=assignment_id,
            event_type=TaskEventType.ASSIGNMENT_HEARTBEAT,
            worker_id=worker_id,
            now=now,
        )
        return renewed

    async def validate_submission(
        self,
        *,
        assignment_id: UUID,
        subtask_id: UUID,
        agent_type: str,
        worker_id: str,
        fencing_token: int,
    ) -> None:
        """校验待提交结果与当前 Assignment、角色及有效租约完全一致。"""

        assignment, subtask, _ = await self._get_owned_active(
            assignment_id, worker_id, fencing_token
        )
        if assignment.status is not AssignmentStatus.RUNNING:
            raise StateTransitionError("只有 RUNNING Assignment 可以提交 AgentRunResult")
        if assignment.subtask_id != subtask_id or subtask.subtask_id != subtask_id:
            raise AssignmentConflictError("AgentRunResult 与 Assignment 子任务不一致")
        if (
            assignment.assigned_agent_type != agent_type
            or subtask.assigned_agent_type != agent_type
        ):
            raise AssignmentConflictError("AgentRunResult 与 Assignment 角色不一致")

    async def complete(
        self,
        *,
        assignment_id: UUID,
        worker_id: str,
        fencing_token: int,
        succeeded: bool,
        agent_run_result_id: UUID | None = None,
        execution_trace_id: UUID | None = None,
        result_status: str | None = None,
    ) -> AgentAssignment:
        """使用当前 fencing token 提交最终结果并解锁满足依赖的下游任务。"""

        assignment, subtask, now = await self._get_owned_active(
            assignment_id, worker_id, fencing_token
        )
        if assignment.status is not AssignmentStatus.RUNNING:
            raise StateTransitionError("只有 RUNNING Assignment 可以提交完成结果")
        assignment_target = AssignmentStatus.SUCCEEDED if succeeded else AssignmentStatus.FAILED
        subtask_target = SubTaskStatus.SUCCEEDED if succeeded else SubTaskStatus.FAILED
        ensure_assignment_transition(assignment.status, assignment_target)
        ensure_subtask_transition(subtask.status, subtask_target)
        completed = assignment.model_copy(
            update={
                "status": assignment_target,
                "finished_at": now,
                "lease_expires_at": None,
            }
        )
        await self._assignments.update(completed)
        await self._tasks.update_subtask_status(subtask.subtask_id, subtask_target, now)
        await self._append_event(
            task_id=subtask.master_task_id,
            subtask_id=subtask.subtask_id,
            assignment_id=assignment_id,
            event_type=TaskEventType.ASSIGNMENT_COMPLETED,
            worker_id=worker_id,
            now=now,
            payload={"succeeded": succeeded, "result_status": result_status},
        )
        if succeeded:
            await self._unlock_dependents(subtask.subtask_id, now)
        else:
            await self._cancel_unsatisfiable_descendants(subtask.subtask_id, now)
        await self._create_snapshot(
            subtask.master_task_id,
            now,
            agent_run_result_id=agent_run_result_id,
            execution_trace_id=execution_trace_id,
        )
        return completed

    async def retry(
        self,
        *,
        assignment_id: UUID,
        worker_id: str,
        fencing_token: int,
        agent_run_result_id: UUID,
        execution_trace_id: UUID,
        result_status: str,
    ) -> AgentAssignment | None:
        """结束可重试失败的当前尝试，并在额度内创建下一次 Assignment。

        返回新建的待认领 Assignment；若已经达到 max_attempts，则把子任务
        置为最终失败并返回 None。本方法不会把失败尝试伪装成租约过期。
        """

        assignment, subtask, now = await self._get_owned_active(
            assignment_id, worker_id, fencing_token
        )
        if assignment.status is not AssignmentStatus.RUNNING:
            raise StateTransitionError("只有 RUNNING Assignment 可以安排业务重试")
        ensure_assignment_transition(assignment.status, AssignmentStatus.FAILED)
        failed = assignment.model_copy(
            update={
                "status": AssignmentStatus.FAILED,
                "finished_at": now,
                "lease_expires_at": None,
            }
        )
        await self._assignments.update(failed)

        next_assignment: AgentAssignment | None = None
        if assignment.attempt < subtask.retry_policy.max_attempts:
            ensure_subtask_lease_recovery(subtask.status)
            next_assignment = AgentAssignment(
                assignment_id=uuid4(),
                subtask_id=subtask.subtask_id,
                assigned_agent_type=subtask.assigned_agent_type,
                status=AssignmentStatus.PENDING,
                fencing_token=assignment.fencing_token,
                attempt=assignment.attempt + 1,
                created_at=max(
                    now,
                    self._as_utc(assignment.created_at) + timedelta(microseconds=1),
                ),
            )
            await self._assignments.add(next_assignment)
            await self._tasks.update_subtask_status(subtask.subtask_id, SubTaskStatus.ASSIGNED, now)
        else:
            ensure_subtask_transition(subtask.status, SubTaskStatus.FAILED)
            await self._tasks.update_subtask_status(subtask.subtask_id, SubTaskStatus.FAILED, now)
            await self._cancel_unsatisfiable_descendants(subtask.subtask_id, now)

        await self._append_event(
            task_id=subtask.master_task_id,
            subtask_id=subtask.subtask_id,
            assignment_id=assignment_id,
            event_type=TaskEventType.ASSIGNMENT_COMPLETED,
            worker_id=worker_id,
            now=now,
            payload={
                "succeeded": False,
                "result_status": result_status,
                "retry_scheduled": next_assignment is not None,
                "attempt": assignment.attempt,
            },
        )
        await self._create_snapshot(
            subtask.master_task_id,
            now,
            agent_run_result_id=agent_run_result_id,
            execution_trace_id=execution_trace_id,
        )
        return next_assignment

    async def wait_for_user(
        self,
        *,
        assignment_id: UUID,
        worker_id: str,
        fencing_token: int,
        agent_run_result_id: UUID,
        execution_trace_id: UUID,
        result_status: str,
    ) -> AgentAssignment:
        """终止当前尝试并把 MasterTask 转为等待用户输入。

        SubTask 使用现有 FAILED 终态保存本次未完成事实。用户补充信息后，由
        总控 Agent 通过新的 TaskSpec/decomposition revision 发布后续正式子任务。
        """

        assignment, subtask, now = await self._get_owned_active(
            assignment_id, worker_id, fencing_token
        )
        if assignment.status is not AssignmentStatus.RUNNING:
            raise StateTransitionError("只有 RUNNING Assignment 可以请求用户输入")
        master = await self._tasks.get_master(subtask.master_task_id, for_update=True)
        if master is None:
            raise AssignmentConflictError("AgentAssignment 对应 MasterTask 不存在")

        ensure_assignment_transition(assignment.status, AssignmentStatus.FAILED)
        ensure_subtask_transition(subtask.status, SubTaskStatus.FAILED)
        ensure_master_task_transition(master.status, MasterTaskStatus.WAITING_USER)
        completed = assignment.model_copy(
            update={
                "status": AssignmentStatus.FAILED,
                "finished_at": now,
                "lease_expires_at": None,
            }
        )
        await self._assignments.update(completed)
        await self._tasks.update_subtask_status(subtask.subtask_id, SubTaskStatus.FAILED, now)
        await self._tasks.update_master(
            master.model_copy(update={"status": MasterTaskStatus.WAITING_USER, "updated_at": now})
        )
        await self._append_event(
            task_id=subtask.master_task_id,
            subtask_id=subtask.subtask_id,
            assignment_id=assignment_id,
            event_type=TaskEventType.ASSIGNMENT_COMPLETED,
            worker_id=worker_id,
            now=now,
            payload={"succeeded": False, "result_status": result_status},
        )
        await self._events.append(
            task_id=subtask.master_task_id,
            event_type=TaskEventType.MASTER_TASK_STATUS_CHANGED,
            actor_type="SERVICE",
            actor_id="assignment_service",
            reason_code="AGENT_NEEDS_USER_INPUT",
            payload={
                "from": master.status.value,
                "to": MasterTaskStatus.WAITING_USER.value,
            },
            occurred_at=now,
        )
        await self._create_snapshot(
            subtask.master_task_id,
            now,
            agent_run_result_id=agent_run_result_id,
            execution_trace_id=execution_trace_id,
        )
        return completed

    async def cancel(
        self,
        *,
        assignment_id: UUID,
        worker_id: str,
        fencing_token: int,
        agent_run_result_id: UUID,
        execution_trace_id: UUID,
        result_status: str,
    ) -> AgentAssignment:
        """按 AgentRunResult 的取消语义终止当前 Assignment 和子任务。"""

        assignment, subtask, now = await self._get_owned_active(
            assignment_id, worker_id, fencing_token
        )
        if assignment.status is not AssignmentStatus.RUNNING:
            raise StateTransitionError("只有 RUNNING Assignment 可以提交取消结果")
        ensure_assignment_transition(assignment.status, AssignmentStatus.CANCELLED)
        ensure_subtask_transition(subtask.status, SubTaskStatus.CANCELLED)
        cancelled = assignment.model_copy(
            update={
                "status": AssignmentStatus.CANCELLED,
                "finished_at": now,
                "lease_expires_at": None,
            }
        )
        await self._assignments.update(cancelled)
        await self._tasks.update_subtask_status(subtask.subtask_id, SubTaskStatus.CANCELLED, now)
        await self._append_event(
            task_id=subtask.master_task_id,
            subtask_id=subtask.subtask_id,
            assignment_id=assignment_id,
            event_type=TaskEventType.ASSIGNMENT_COMPLETED,
            worker_id=worker_id,
            now=now,
            payload={"succeeded": False, "result_status": result_status},
        )
        await self._create_snapshot(
            subtask.master_task_id,
            now,
            agent_run_result_id=agent_run_result_id,
            execution_trace_id=execution_trace_id,
        )
        return cancelled

    async def recover_expired(self, *, limit: int = 100) -> list[AgentAssignment]:
        """使过期执行尝试失效，并在重试额度内创建下一次 PENDING Assignment。"""

        now = self._clock()
        expired_assignments = await self._assignments.list_expired(now, limit)
        recovered: list[AgentAssignment] = []
        for assignment in expired_assignments:
            subtask = await self._tasks.get_subtask(assignment.subtask_id, for_update=True)
            if subtask is None:
                continue
            master = await self._tasks.get_master(subtask.master_task_id, for_update=True)
            if master is None:
                continue
            ensure_assignment_transition(assignment.status, AssignmentStatus.EXPIRED)
            ensure_subtask_lease_recovery(subtask.status)
            expired = assignment.model_copy(
                update={"status": AssignmentStatus.EXPIRED, "finished_at": now}
            )
            await self._assignments.update(expired)
            if assignment.attempt < subtask.retry_policy.max_attempts:
                next_assignment = AgentAssignment(
                    assignment_id=uuid4(),
                    subtask_id=subtask.subtask_id,
                    assigned_agent_type=subtask.assigned_agent_type,
                    status=AssignmentStatus.PENDING,
                    fencing_token=assignment.fencing_token,
                    attempt=assignment.attempt + 1,
                    # 即使注入测试时钟发生回拨，重试记录也必须晚于上一尝试。
                    created_at=max(
                        now,
                        self._as_utc(assignment.created_at) + timedelta(microseconds=1),
                    ),
                )
                await self._assignments.add(next_assignment)
                await self._tasks.update_subtask_status(
                    subtask.subtask_id, SubTaskStatus.ASSIGNED, now
                )
                recovered.append(next_assignment)
            else:
                await self._tasks.update_subtask_status(
                    subtask.subtask_id, SubTaskStatus.FAILED, now
                )
            await self._append_event(
                task_id=subtask.master_task_id,
                subtask_id=subtask.subtask_id,
                assignment_id=assignment.assignment_id,
                event_type=TaskEventType.ASSIGNMENT_EXPIRED,
                worker_id="assignment_recovery",
                now=now,
                payload={"attempt": assignment.attempt},
            )
            await self._create_snapshot(subtask.master_task_id, now)
        return recovered

    async def _get_owned_active(
        self, assignment_id: UUID, worker_id: str, fencing_token: int
    ) -> tuple[AgentAssignment, AgentSubTask, datetime]:
        """锁定并校验 Assignment 当前持有者、令牌、状态和租约。"""

        assignment = await self._assignments.get(assignment_id, for_update=True)
        if assignment is None:
            raise AssignmentConflictError("AgentAssignment 不存在")
        subtask = await self._tasks.get_subtask(assignment.subtask_id, for_update=True)
        if subtask is None:
            raise AssignmentConflictError("AgentAssignment 对应子任务不存在")
        # Assignment 粒度认领仍可并行；只在提交任务级快照前短暂锁定 MasterTask。
        master = await self._tasks.get_master(subtask.master_task_id, for_update=True)
        if master is None:
            raise AssignmentConflictError("AgentAssignment 对应 MasterTask 不存在")
        if master.status is not MasterTaskStatus.ACTIVE:
            raise AssignmentConflictError("MasterTask 已暂停或终止，不能提交 Assignment")
        now = self._clock()
        if assignment.claimed_by != worker_id:
            raise AssignmentConflictError("Worker 不是当前 Assignment 持有者")
        if assignment.fencing_token != fencing_token:
            raise AssignmentConflictError("fencing token 已过期")
        if assignment.status not in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}:
            raise AssignmentConflictError("Assignment 当前不在有效租约状态")
        if assignment.lease_expires_at is None or self._as_utc(
            assignment.lease_expires_at
        ) <= self._as_utc(now):
            raise AssignmentConflictError("Assignment 租约已经过期")
        return assignment, subtask, now

    async def _unlock_dependents(self, subtask_id: UUID, now: datetime) -> None:
        """把依赖全部成功的 BLOCKED 下游任务转为可认领 Assignment。"""

        completed_subtask = await self._tasks.get_subtask(subtask_id)
        # 带人工审核标记的质量任务成功，只表示审核材料已准备好。其下游发布任务必须
        # 等 HumanReviewService 生成 Approval 后再解锁，不能因任务状态成功而绕过门禁。
        if (
            completed_subtask is not None
            and completed_subtask.human_review_required
            and completed_subtask.subtask_type not in {"ontology_publish", "graph_publish"}
        ):
            return
        for dependent in await self._tasks.list_dependents(subtask_id):
            if dependent.status is not SubTaskStatus.BLOCKED:
                continue
            statuses = await self._tasks.list_dependency_statuses(dependent.subtask_id)
            if any(status is not SubTaskStatus.SUCCEEDED for status in statuses):
                continue
            ensure_subtask_transition(SubTaskStatus.BLOCKED, SubTaskStatus.READY)
            await self._tasks.update_subtask_status(dependent.subtask_id, SubTaskStatus.READY, now)
            assignment = AgentAssignment(
                assignment_id=uuid4(),
                subtask_id=dependent.subtask_id,
                assigned_agent_type=dependent.assigned_agent_type,
                status=AssignmentStatus.PENDING,
                created_at=now,
            )
            await self._assignments.add(assignment)
            ensure_subtask_transition(SubTaskStatus.READY, SubTaskStatus.ASSIGNED)
            await self._tasks.update_subtask_status(
                dependent.subtask_id, SubTaskStatus.ASSIGNED, now
            )
            await self._append_event(
                task_id=dependent.master_task_id,
                subtask_id=dependent.subtask_id,
                assignment_id=assignment.assignment_id,
                event_type=TaskEventType.ASSIGNMENT_CREATED,
                worker_id="assignment_service",
                now=now,
                payload={"attempt": 1},
            )

    async def _cancel_unsatisfiable_descendants(self, subtask_id: UUID, now: datetime) -> None:
        """上游最终失败后取消仍阻塞的全部后代，避免完整计划永久等待。

        这里只取消尚未获得 Assignment 的 BLOCKED/PENDING 后代。已经运行或完成的独立
        分支保持原状；全部计划进入终态后，再由总控 Agent 根据公开失败结果决定是否
        发布修复 revision。
        """

        pending = [subtask_id]
        visited: set[UUID] = set()
        while pending:
            failed_or_cancelled_id = pending.pop()
            if failed_or_cancelled_id in visited:
                continue
            visited.add(failed_or_cancelled_id)
            for dependent in await self._tasks.list_dependents(failed_or_cancelled_id):
                pending.append(dependent.subtask_id)
                if dependent.status not in {
                    SubTaskStatus.PENDING,
                    SubTaskStatus.BLOCKED,
                }:
                    continue
                ensure_subtask_transition(dependent.status, SubTaskStatus.CANCELLED)
                await self._tasks.update_subtask_status(
                    dependent.subtask_id,
                    SubTaskStatus.CANCELLED,
                    now,
                )
                await self._events.append(
                    task_id=dependent.master_task_id,
                    subtask_id=dependent.subtask_id,
                    event_type=TaskEventType.SUBTASK_STATUS_CHANGED,
                    actor_type="SERVICE",
                    actor_id="assignment_service",
                    reason_code="UPSTREAM_TERMINAL_FAILURE",
                    payload={
                        "from": dependent.status.value,
                        "to": SubTaskStatus.CANCELLED.value,
                        "failed_dependency_id": str(failed_or_cancelled_id),
                    },
                    occurred_at=now,
                )

    async def _append_event(
        self,
        *,
        task_id: UUID,
        subtask_id: UUID,
        assignment_id: UUID,
        event_type: TaskEventType,
        worker_id: str,
        now: datetime,
        payload: dict[str, object] | None = None,
    ) -> None:
        """以 WORKER 角色追加 Assignment 审计事件。"""

        await self._events.append(
            task_id=task_id,
            subtask_id=subtask_id,
            assignment_id=assignment_id,
            event_type=event_type,
            actor_type="WORKER",
            actor_id=worker_id,
            payload=payload,
            occurred_at=now,
        )

    async def append_memories(self, *, task_id: UUID, entries: list[MemoryRef]) -> None:
        """把专业 Agent 产生的记忆引用追加到任务级 SharedContext。"""

        if entries:
            await self._create_snapshot(task_id, self._clock(), memory_entries=entries)

    async def _create_snapshot(
        self,
        task_id: UUID,
        now: datetime,
        *,
        agent_run_result_id: UUID | None = None,
        execution_trace_id: UUID | None = None,
        memory_entries: list[MemoryRef] | None = None,
    ) -> SharedTaskContext:
        """构建快照，并追加运行结果、公开轨迹和 Agent 共享记忆引用。"""

        current = await self._contexts.get(task_id)
        context = await build_context_projection(
            task_id=task_id,
            version=1 if current is None else current.version + 1,
            updated_at=now,
            task_repository=self._tasks,
            assignment_repository=self._assignments,
            context_repository=self._contexts,
        )
        if agent_run_result_id is not None or execution_trace_id is not None:
            result_ids = list(context.agent_run_result_ids)
            trace_ids = list(context.execution_trace_ids)
            if agent_run_result_id is not None and agent_run_result_id not in result_ids:
                result_ids.append(agent_run_result_id)
            if execution_trace_id is not None and execution_trace_id not in trace_ids:
                trace_ids.append(execution_trace_id)
            context = context.model_copy(
                update={
                    "agent_run_result_ids": result_ids,
                    "execution_trace_ids": trace_ids,
                }
            )
        if memory_entries:
            short = list(context.short_term_memories)
            long_term = list(context.long_term_memory_refs)
            known = {item.memory_id for item in [*short, *long_term]}
            for entry in memory_entries:
                if entry.memory_id in known:
                    continue
                (short if entry.memory_type == "SHORT_TERM" else long_term).append(entry)
                known.add(entry.memory_id)
            context = context.model_copy(
                update={"short_term_memories": short, "long_term_memory_refs": long_term}
            )
        await self._contexts.add_snapshot(context)
        await self._events.append(
            task_id=task_id,
            event_type=TaskEventType.CONTEXT_VERSION_CREATED,
            actor_type="SERVICE",
            actor_id="assignment_service",
            occurred_at=now,
            payload={"version": context.version},
        )
        return context

    @staticmethod
    def _as_utc(value: datetime) -> datetime:
        """将 SQLite 返回的无时区时间统一解释为 UTC，生产 PostgreSQL 保留时区。"""

        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
