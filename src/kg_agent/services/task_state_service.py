"""实现任务发布和状态迁移的确定性领域服务。

调用方负责事务边界；本服务确保业务事实、事件和 SharedContext 快照在同一事务写入。
"""

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from uuid import UUID, uuid4

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import (
    AssignmentStatus,
    MasterTaskStatus,
    SubTaskStatus,
    TaskEventType,
    TaskIntent,
)
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.domain.state_transitions import (
    ensure_master_task_transition,
    ensure_subtask_transition,
)
from kg_agent.domain.task_models import AgentSubTask, MasterTask
from kg_agent.exceptions import ContextConflictError, PersistenceError, StateTransitionError
from kg_agent.repositories.interfaces import (
    AssignmentRepository,
    ContextRepository,
    EventRepository,
    TaskRepository,
)
from kg_agent.services.context_projection import build_context_projection

Clock = Callable[[], datetime]


class TaskStateService:
    """原子发布任务拆解并执行确定性子任务状态迁移。"""

    def __init__(
        self,
        *,
        task_repository: TaskRepository,
        assignment_repository: AssignmentRepository,
        context_repository: ContextRepository,
        event_repository: EventRepository,
        clock: Clock | None = None,
    ) -> None:
        """注入同一事务中的 Repository 和可测试时钟。"""

        self._tasks = task_repository
        self._assignments = assignment_repository
        self._contexts = context_repository
        self._events = event_repository
        self._clock = clock or (lambda: datetime.now(UTC))

    async def publish_decomposition(
        self,
        *,
        conversation_id: UUID,
        result: TaskDecompositionResult,
        source_artifacts: Sequence[ArtifactRef],
        expected_version: int | None,
        recalled_long_term_memories: Sequence[MemoryRef] = (),
        actor_id: str = "controller_agent",
    ) -> SharedTaskContext:
        """发布合法拆解，并在同一事务创建可执行 Assignment 和上下文快照。

        Raises:
            ContextConflictError: 上下文版本、任务或 revision 与当前事实冲突。
            PersistenceError: 同一 ID 被不同内容复用。
        """

        if result.requires_clarification:
            raise ContextConflictError("请求澄清时不能发布任务拆解")

        task_id = result.task_spec.task_id
        now = self._clock()
        # 更新现有任务时先锁定 MasterTask，再读取版本，避免两个写事务同时通过旧版本检查。
        master = await self._tasks.get_master(task_id, for_update=True)
        current_context = await self._contexts.get(task_id)
        current_version = current_context.version if current_context else None
        if current_version != expected_version:
            raise ContextConflictError("SharedContext 版本冲突")
        is_new_task = master is None
        if is_new_task:
            if result.task_intent is not TaskIntent.START:
                raise ContextConflictError("不存在当前任务时只能发布 START")
            decomposition_revision = self._decomposition_revision(result, default=1)
            master = MasterTask(
                task_id=task_id,
                conversation_id=conversation_id,
                title=result.task_spec.business_goal,
                current_task_spec_revision=result.task_spec.revision,
                current_decomposition_revision=decomposition_revision,
                status=MasterTaskStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            await self._tasks.add_master(master)
            await self._append_event(
                task_id=task_id,
                event_type=TaskEventType.MASTER_TASK_CREATED,
                actor_id=actor_id,
                occurred_at=now,
            )
        else:
            assert master is not None
            if result.task_intent is TaskIntent.START:
                raise ContextConflictError("已存在当前任务时不能再次发布 START")
            if master.conversation_id != conversation_id:
                raise ContextConflictError("conversation_id 与现有 MasterTask 不一致")
            decomposition_revision = self._decomposition_revision(
                result, default=master.current_decomposition_revision + 1
            )
            if decomposition_revision <= master.current_decomposition_revision:
                raise ContextConflictError("新的 decomposition revision 必须递增")

        stored_spec = await self._tasks.get_task_spec(task_id, result.task_spec.revision)
        if stored_spec is None:
            await self._tasks.add_task_spec(result.task_spec)
            await self._append_event(
                task_id=task_id,
                event_type=TaskEventType.TASK_SPEC_REVISED,
                actor_id=actor_id,
                occurred_at=now,
                payload={"revision": result.task_spec.revision},
            )
        elif stored_spec != result.task_spec:
            raise PersistenceError("同一 TaskSpec revision 不能保存不同内容")

        if await self._tasks.decomposition_exists(task_id, decomposition_revision):
            raise PersistenceError("decomposition revision 已存在")
        if any(item.decomposition_revision != decomposition_revision for item in result.subtasks):
            raise PersistenceError("同一次发布的子任务必须使用相同 decomposition revision")
        await self._tasks.add_decomposition(
            task_id=task_id,
            revision=decomposition_revision,
            task_spec_revision=result.task_spec.revision,
            task_intent=result.task_intent.value,
            created_at=now,
        )

        normalized_subtasks: list[AgentSubTask] = []
        for subtask in result.subtasks:
            existing = await self._tasks.get_subtask(subtask.subtask_id)
            if existing is not None:
                if existing != subtask:
                    raise PersistenceError("同一 subtask_id 不能保存不同内容")
                continue
            initial = subtask.model_copy(update={"status": SubTaskStatus.PENDING})
            await self._tasks.add_subtask(initial)
            normalized_subtasks.append(initial)

        for subtask in normalized_subtasks:
            await self._tasks.add_dependencies(subtask.subtask_id, subtask.dependency_subtask_ids)
            await self._append_event(
                task_id=task_id,
                subtask_id=subtask.subtask_id,
                event_type=TaskEventType.SUBTASK_PUBLISHED,
                actor_id=actor_id,
                occurred_at=now,
                payload={"assigned_agent_type": subtask.assigned_agent_type},
            )

        for subtask in normalized_subtasks:
            dependency_statuses = await self._tasks.list_dependency_statuses(subtask.subtask_id)
            target = (
                SubTaskStatus.READY
                if all(status is SubTaskStatus.SUCCEEDED for status in dependency_statuses)
                else SubTaskStatus.BLOCKED
            )
            ensure_subtask_transition(SubTaskStatus.PENDING, target)
            await self._tasks.update_subtask_status(subtask.subtask_id, target, now)
            if target is SubTaskStatus.READY:
                await self._create_assignment(subtask.model_copy(update={"status": target}), now)

        assert master is not None
        updated_master = master.model_copy(
            update={
                "title": result.task_spec.business_goal,
                "current_task_spec_revision": result.task_spec.revision,
                "current_decomposition_revision": decomposition_revision,
                "status": MasterTaskStatus.ACTIVE,
                "updated_at": now,
            }
        )
        await self._tasks.update_master(updated_master)
        await self._append_event(
            task_id=task_id,
            event_type=TaskEventType.DECOMPOSITION_PUBLISHED,
            actor_id=actor_id,
            occurred_at=now,
            payload={
                "revision": decomposition_revision,
                "task_spec_revision": result.task_spec.revision,
                "reused_artifact_ids": [str(item) for item in result.reused_artifact_ids],
            },
        )

        for artifact in source_artifacts:
            if artifact.task_id != task_id:
                raise PersistenceError("ArtifactRef.task_id 与当前任务不一致")
            existing_artifact = await self._tasks.get_artifact_ref(artifact.artifact_id)
            if existing_artifact is None:
                await self._tasks.add_artifact_ref(artifact)
            elif existing_artifact != artifact:
                raise PersistenceError("同一 artifact_id 不能保存不同 ArtifactRef")

        return await self._create_context_snapshot(
            task_id,
            now,
            actor_id,
            recalled_long_term_memories=recalled_long_term_memories,
        )

    async def transition_subtask(
        self,
        *,
        subtask_id: UUID,
        target: SubTaskStatus,
        actor_id: str,
        reason_code: str | None = None,
    ) -> AgentSubTask:
        """执行单个子任务状态迁移，并拒绝依赖未完成时进入 READY。"""

        subtask = await self._tasks.get_subtask(subtask_id, for_update=True)
        if subtask is None:
            raise StateTransitionError("AgentSubTask 不存在")
        master = await self._tasks.get_master(subtask.master_task_id, for_update=True)
        if master is None:
            raise StateTransitionError("AgentSubTask 对应 MasterTask 不存在")
        ensure_subtask_transition(subtask.status, target)
        if target is SubTaskStatus.READY:
            dependency_statuses = await self._tasks.list_dependency_statuses(subtask_id)
            if any(status is not SubTaskStatus.SUCCEEDED for status in dependency_statuses):
                raise StateTransitionError("子任务依赖未完成，不能进入 READY")
        now = self._clock()
        await self._tasks.update_subtask_status(subtask_id, target, now)
        final_target = target
        if target is SubTaskStatus.READY:
            await self._create_assignment(subtask.model_copy(update={"status": target}), now)
            final_target = SubTaskStatus.ASSIGNED
        await self._append_event(
            task_id=subtask.master_task_id,
            subtask_id=subtask_id,
            event_type=TaskEventType.SUBTASK_STATUS_CHANGED,
            actor_id=actor_id,
            occurred_at=now,
            reason_code=reason_code,
            payload={"from": subtask.status.value, "to": target.value},
        )
        await self._create_context_snapshot(subtask.master_task_id, now, actor_id)
        return subtask.model_copy(update={"status": final_target})

    async def transition_master_task(
        self,
        *,
        task_id: UUID,
        target: MasterTaskStatus,
        actor_id: str,
        reason_code: str | None = None,
    ) -> MasterTask:
        """执行 MasterTask 合法状态迁移并生成事件和新上下文快照。"""

        master = await self._tasks.get_master(task_id, for_update=True)
        if master is None:
            raise StateTransitionError("MasterTask 不存在")
        ensure_master_task_transition(master.status, target)
        now = self._clock()
        terminal = target in {
            MasterTaskStatus.SUCCEEDED,
            MasterTaskStatus.FAILED,
            MasterTaskStatus.CANCELLED,
        }
        updated = master.model_copy(
            update={
                "status": target,
                "updated_at": now,
                "completed_at": now if terminal else None,
            }
        )
        await self._tasks.update_master(updated)
        await self._append_event(
            task_id=task_id,
            event_type=TaskEventType.MASTER_TASK_STATUS_CHANGED,
            actor_id=actor_id,
            occurred_at=now,
            reason_code=reason_code,
            payload={"from": master.status.value, "to": target.value},
        )
        await self._create_context_snapshot(task_id, now, actor_id)
        return updated

    async def _create_assignment(self, subtask: AgentSubTask, now: datetime) -> AgentAssignment:
        """为 READY 子任务创建首次 PENDING Assignment 并把子任务标记为 ASSIGNED。"""

        existing_attempts = [
            item
            for item in await self._assignments.list_for_task(subtask.master_task_id)
            if item.subtask_id == subtask.subtask_id
        ]
        attempt = max((item.attempt for item in existing_attempts), default=0) + 1
        fencing_token = max((item.fencing_token for item in existing_attempts), default=0)
        assignment = AgentAssignment(
            assignment_id=uuid4(),
            subtask_id=subtask.subtask_id,
            assigned_agent_type=subtask.assigned_agent_type,
            status=AssignmentStatus.PENDING,
            fencing_token=fencing_token,
            attempt=attempt,
            created_at=now,
        )
        await self._assignments.add(assignment)
        ensure_subtask_transition(SubTaskStatus.READY, SubTaskStatus.ASSIGNED)
        await self._tasks.update_subtask_status(subtask.subtask_id, SubTaskStatus.ASSIGNED, now)
        await self._append_event(
            task_id=subtask.master_task_id,
            subtask_id=subtask.subtask_id,
            assignment_id=assignment.assignment_id,
            event_type=TaskEventType.ASSIGNMENT_CREATED,
            actor_id="task_state_service",
            occurred_at=now,
            payload={"attempt": assignment.attempt},
        )
        return assignment

    async def _create_context_snapshot(
        self,
        task_id: UUID,
        now: datetime,
        actor_id: str,
        recalled_long_term_memories: Sequence[MemoryRef] = (),
    ) -> SharedTaskContext:
        """创建下一个 SharedContext 版本并追加对应审计事件。"""

        current = await self._contexts.get(task_id)
        next_version = 1 if current is None else current.version + 1
        context = await build_context_projection(
            task_id=task_id,
            version=next_version,
            updated_at=now,
            task_repository=self._tasks,
            assignment_repository=self._assignments,
            context_repository=self._contexts,
        )
        if recalled_long_term_memories:
            memory_map = {item.memory_id: item for item in context.long_term_memory_refs}
            for memory in recalled_long_term_memories:
                if memory.memory_type != "LONG_TERM":
                    raise ContextConflictError("召回记忆必须使用 LONG_TERM 类型")
                # 已有记忆本轮再次命中时先移除再追加，使 Context View 的最后三条
                # 始终代表当前查询结果，而不是首次写入该 ID 时的历史位置。
                memory_map.pop(memory.memory_id, None)
                memory_map[memory.memory_id] = memory
            context = context.model_copy(
                update={"long_term_memory_refs": list(memory_map.values())},
                deep=True,
            )
        await self._contexts.add_snapshot(context)
        await self._append_event(
            task_id=task_id,
            event_type=TaskEventType.CONTEXT_VERSION_CREATED,
            actor_id=actor_id,
            occurred_at=now,
            payload={"version": next_version},
        )
        return context

    async def _append_event(
        self,
        *,
        task_id: UUID,
        event_type: TaskEventType,
        actor_id: str,
        occurred_at: datetime,
        subtask_id: UUID | None = None,
        assignment_id: UUID | None = None,
        reason_code: str | None = None,
        payload: dict[str, object] | None = None,
    ) -> None:
        """以 SERVICE 角色追加一个结构化任务事件。"""

        await self._events.append(
            task_id=task_id,
            subtask_id=subtask_id,
            assignment_id=assignment_id,
            event_type=event_type,
            actor_type="SERVICE",
            actor_id=actor_id,
            reason_code=reason_code,
            payload=payload,
            occurred_at=occurred_at,
        )

    @staticmethod
    def _decomposition_revision(result: TaskDecompositionResult, *, default: int) -> int:
        """从子任务取得统一拆解版本；空拆解使用调用方给出的版本。"""

        revisions = {item.decomposition_revision for item in result.subtasks}
        if len(revisions) > 1:
            raise PersistenceError("一次拆解不能包含多个 decomposition revision")
        return next(iter(revisions), default)
