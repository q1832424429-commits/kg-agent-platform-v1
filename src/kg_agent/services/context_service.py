"""提供 SharedContext 服务协议和用于快速测试的追加式内存实现。

生产持久化由 PostgresContextService 实现；两种适配器遵循相同 ContextService 边界。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol
from uuid import UUID

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import MasterTaskStatus, TaskIntent
from kg_agent.domain.memory_models import ConversationContextRef, MemoryRef
from kg_agent.domain.task_models import AgentSubTask, MasterTask, TaskSpec
from kg_agent.exceptions import ContextConflictError


class ContextService(Protocol):
    """约束总控 Agent 可使用的 SharedContext 读写能力。"""

    async def get(self, task_id: UUID) -> SharedTaskContext | None:
        """返回任务上下文快照；任务不存在时返回 None。"""
        ...

    async def list_subtasks(self, task_id: UUID) -> list[AgentSubTask]:
        """返回任务已经发布的子任务。"""
        ...

    async def get_task_spec(self, task_id: UUID, revision: int) -> TaskSpec | None:
        """读取不可变历史 TaskSpec，供跨 revision 复用影响校验。"""
        ...

    async def publish_decomposition(
        self,
        *,
        conversation_id: UUID,
        result: TaskDecompositionResult,
        source_artifacts: Sequence[ArtifactRef],
        expected_version: int | None,
        recalled_long_term_memories: Sequence[MemoryRef] = (),
    ) -> SharedTaskContext:
        """原子追加 TaskSpec revision 和子任务，并返回新上下文快照。"""
        ...


class InMemoryContextService:
    """用于单元测试和无数据库演示的追加式 SharedContext 内存实现。"""

    def __init__(self) -> None:
        """初始化空的任务上下文和子任务索引。"""

        self._contexts: dict[UUID, SharedTaskContext] = {}
        self._subtasks: dict[UUID, dict[UUID, AgentSubTask]] = {}
        self._task_specs: dict[UUID, dict[int, TaskSpec]] = {}

    async def get(self, task_id: UUID) -> SharedTaskContext | None:
        """返回上下文副本，避免调用方直接修改内部状态。"""

        context = self._contexts.get(task_id)
        return context.model_copy(deep=True) if context else None

    async def get_task_spec(self, task_id: UUID, revision: int) -> TaskSpec | None:
        """返回历史 TaskSpec 副本，不允许调用方覆盖已发布 revision。"""

        spec = self._task_specs.get(task_id, {}).get(revision)
        return spec.model_copy(deep=True) if spec else None

    async def list_subtasks(self, task_id: UUID) -> list[AgentSubTask]:
        """按创建时间和 ID 稳定返回任务子任务副本。"""

        values = self._subtasks.get(task_id, {}).values()
        ordered = sorted(values, key=lambda item: (item.created_at, str(item.subtask_id)))
        return [item.model_copy(deep=True) for item in ordered]

    async def publish_decomposition(
        self,
        *,
        conversation_id: UUID,
        result: TaskDecompositionResult,
        source_artifacts: Sequence[ArtifactRef],
        expected_version: int | None,
        recalled_long_term_memories: Sequence[MemoryRef] = (),
    ) -> SharedTaskContext:
        """以追加方式发布已校验拆解结果。

        Raises:
            ContextConflictError: 请求澄清、版本冲突、任务标识冲突或子任务 ID
                被不同内容重复使用时抛出。
        """

        if result.requires_clarification:
            raise ContextConflictError("请求澄清时不能发布子任务")

        task_id = result.task_spec.task_id
        current = self._contexts.get(task_id)
        if current is None and expected_version is not None:
            raise ContextConflictError("任务不存在，不能使用 expected_version 更新")
        if current is not None and current.version != expected_version:
            raise ContextConflictError("SharedContext 版本冲突")

        if current is None and result.task_intent is not TaskIntent.START:
            raise ContextConflictError("不存在当前任务时只能发布 START")
        if current is not None and result.task_intent is TaskIntent.START:
            raise ContextConflictError("已存在当前任务时不能再次发布 START")
        if current is not None and current.master_task.conversation_id != conversation_id:
            raise ContextConflictError("conversation_id 与现有 MasterTask 不一致")
        task_specs = dict(self._task_specs.get(task_id, {}))
        stored_spec = task_specs.get(result.task_spec.revision)
        if stored_spec is not None and stored_spec != result.task_spec:
            raise ContextConflictError("同一 TaskSpec revision 不能保存不同内容")
        task_specs[result.task_spec.revision] = result.task_spec.model_copy(deep=True)

        incoming_revisions = {item.decomposition_revision for item in result.subtasks}
        if len(incoming_revisions) > 1:
            raise ContextConflictError("同一次发布的子任务必须使用相同 decomposition revision")
        expected_decomposition_revision = (
            1 if current is None else current.master_task.current_decomposition_revision + 1
        )
        decomposition_revision = next(iter(incoming_revisions), expected_decomposition_revision)
        if decomposition_revision != expected_decomposition_revision:
            raise ContextConflictError(
                f"新的 decomposition revision 必须为 {expected_decomposition_revision}"
            )

        now = datetime.now(UTC)
        # 在局部副本中完成全部检查，避免后续冲突导致前面的子任务部分写入。
        task_subtasks = dict(self._subtasks.get(task_id, {}))
        for subtask in result.subtasks:
            stored = task_subtasks.get(subtask.subtask_id)
            if stored is not None and stored != subtask:
                raise ContextConflictError(f"subtask_id 已被不同内容占用：{subtask.subtask_id}")
            task_subtasks[subtask.subtask_id] = subtask.model_copy(deep=True)

        artifact_map: dict[UUID, ArtifactRef] = {}
        if current is not None:
            artifact_map.update({item.artifact_id: item for item in current.artifact_refs})
        for artifact in source_artifacts:
            if artifact.task_id != task_id:
                raise ContextConflictError("ArtifactRef.task_id 与当前任务不一致")
            previous = artifact_map.get(artifact.artifact_id)
            if previous is not None and previous != artifact:
                raise ContextConflictError("同一 artifact_id 不能保存不同 ArtifactRef")
            artifact_map[artifact.artifact_id] = artifact.model_copy(deep=True)

        memory_map: dict[UUID, MemoryRef] = {}
        if current is not None:
            memory_map.update({item.memory_id: item for item in current.long_term_memory_refs})
        for memory in recalled_long_term_memories:
            if memory.memory_type != "LONG_TERM":
                raise ContextConflictError("召回记忆必须使用 LONG_TERM 类型")
            memory_map.pop(memory.memory_id, None)
            memory_map[memory.memory_id] = memory.model_copy(deep=True)

        if current is None:
            master_task = MasterTask(
                task_id=task_id,
                conversation_id=conversation_id,
                title=result.task_spec.business_goal,
                current_task_spec_revision=result.task_spec.revision,
                current_decomposition_revision=decomposition_revision,
                status=MasterTaskStatus.ACTIVE,
                created_at=now,
                updated_at=now,
            )
            context = SharedTaskContext(
                task_id=task_id,
                version=1,
                conversation=ConversationContextRef(
                    conversation_id=conversation_id,
                    summary=result.task_spec.business_goal,
                ),
                master_task=master_task,
                current_task_spec=result.task_spec,
                task_spec_revision_refs=[result.task_spec.revision],
                subtask_ids=list(task_subtasks),
                artifact_refs=list(artifact_map.values()),
                long_term_memory_refs=list(memory_map.values()),
                updated_at=now,
            )
        else:
            revision_refs = list(current.task_spec_revision_refs)
            if result.task_spec.revision not in revision_refs:
                revision_refs.append(result.task_spec.revision)
            master_task = current.master_task.model_copy(
                update={
                    "title": result.task_spec.business_goal,
                    "current_task_spec_revision": result.task_spec.revision,
                    "current_decomposition_revision": decomposition_revision,
                    "updated_at": now,
                }
            )
            context = current.model_copy(
                update={
                    "version": current.version + 1,
                    "master_task": master_task,
                    "current_task_spec": result.task_spec,
                    "task_spec_revision_refs": revision_refs,
                    "subtask_ids": list(task_subtasks),
                    "artifact_refs": list(artifact_map.values()),
                    "long_term_memory_refs": list(memory_map.values()),
                    "updated_at": now,
                },
                deep=True,
            )

        self._contexts[task_id] = context.model_copy(deep=True)
        self._subtasks[task_id] = task_subtasks
        self._task_specs[task_id] = task_specs
        return context.model_copy(deep=True)
