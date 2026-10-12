"""根据 Agent 角色和 Assignment 构造最小只读 Context View。"""

from collections.abc import Sequence
from uuid import UUID

from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_view_models import (
    ArtifactContextItem,
    AssignmentContextItem,
    ControllerContextView,
    ProfessionalAgentContextView,
    SubTaskContextItem,
)
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.exceptions import AssignmentConflictError, ContextConflictError
from kg_agent.repositories.interfaces import (
    AssignmentRepository,
    ContextRepository,
    TaskRepository,
)

_MAX_CONVERSATION_MESSAGES_PER_AGENT_VIEW = 10
_MAX_LONG_TERM_MEMORY_REFS_PER_AGENT_VIEW = 3


class ContextViewService:
    """只向总控或当前专业 Agent 暴露完成工作所需字段。"""

    def __init__(
        self,
        *,
        task_repository: TaskRepository,
        assignment_repository: AssignmentRepository,
        context_repository: ContextRepository,
    ) -> None:
        """注入只读上下文所依赖的 Repository。"""

        self._tasks = task_repository
        self._assignments = assignment_repository
        self._contexts = context_repository

    async def build_controller_view(self, task_id: UUID) -> ControllerContextView:
        """构造总控 Agent 可见的任务摘要、全部子任务状态和 Artifact 摘要。"""

        context = await self._contexts.get(task_id)
        if context is None:
            raise ContextConflictError("MasterTask 不存在")
        subtasks = await self._tasks.list_subtasks(task_id)
        assignments = await self._assignments.list_for_task(task_id)
        return ControllerContextView(
            task_id=task_id,
            context_version=context.version,
            current_task_spec=context.current_task_spec,
            task_spec_revisions=context.task_spec_revision_refs,
            subtasks=[
                SubTaskContextItem(
                    subtask_id=item.subtask_id,
                    name=item.name,
                    subtask_type=item.subtask_type,
                    assigned_agent_type=item.assigned_agent_type,
                    status=item.status,
                    dependency_subtask_ids=item.dependency_subtask_ids,
                    expected_output_types=item.expected_output_types,
                )
                for item in subtasks
            ],
            assignments=[self._assignment_item(item) for item in assignments],
            artifacts=[self._artifact_item(item) for item in context.artifact_refs],
            human_review_request_ids=context.human_review_request_ids,
            short_term_memories=self._conversation_memories(context.short_term_memories),
            long_term_memory_refs=self._long_term_memories(context.long_term_memory_refs),
        )

    async def build_professional_view(
        self,
        *,
        assignment_id: UUID,
        requested_agent_type: str,
        resolved_input_artifact_ids: Sequence[UUID] | None = None,
    ) -> ProfessionalAgentContextView:
        """仅为 Assignment 指定的专业 Agent 构造工作视图。

        Raises:
            AssignmentConflictError: Assignment 不存在或请求角色与分配角色不一致。
            ContextConflictError: 子任务或 SharedContext 不存在。
        """

        assignment = await self._assignments.get(assignment_id)
        if assignment is None:
            raise AssignmentConflictError("AgentAssignment 不存在")
        if assignment.assigned_agent_type != requested_agent_type:
            raise AssignmentConflictError("请求 Agent 类型无权读取该 Assignment 上下文")
        subtask = await self._tasks.get_subtask(assignment.subtask_id)
        if subtask is None:
            raise ContextConflictError("Assignment 对应 AgentSubTask 不存在")
        context = await self._contexts.get(subtask.master_task_id)
        if context is None:
            raise ContextConflictError("AgentSubTask 对应 SharedContext 不存在")

        # 完整计划发布时，下游 Artifact ID 尚不存在。Worker 在依赖成功后解析真实
        # 血缘，并通过本参数把该次执行的冻结输入投影给专业 Agent。
        input_ids = set(
            subtask.input_artifact_ids
            if resolved_input_artifact_ids is None
            else resolved_input_artifact_ids
        )
        input_artifacts = [
            self._artifact_item(item)
            for item in context.artifact_refs
            if item.artifact_id in input_ids
        ]
        return ProfessionalAgentContextView(
            task_id=subtask.master_task_id,
            context_version=context.version,
            assignment=self._assignment_item(assignment),
            subtask_id=subtask.subtask_id,
            subtask_type=subtask.subtask_type,
            goal=subtask.goal,
            completion_criteria=subtask.completion_criteria,
            quality_requirements=subtask.quality_requirements,
            input_artifacts=input_artifacts,
            business_goal=context.current_task_spec.business_goal,
            domain=context.current_task_spec.domain,
            extraction_scope=context.current_task_spec.extraction_scope,
            constraints=context.current_task_spec.constraints,
            short_term_memories=self._conversation_memories(context.short_term_memories),
            long_term_memory_refs=self._long_term_memories(context.long_term_memory_refs),
        )

    @staticmethod
    def _conversation_memories(entries: Sequence[MemoryRef]) -> list[MemoryRef]:
        """只投影最近五轮公开对话，不让任务状态摘要占用十条对话额度。"""

        messages = [
            item
            for item in entries
            if any(ref.startswith("conversation-role:") for ref in item.source_refs)
        ]
        return messages[-_MAX_CONVERSATION_MESSAGES_PER_AGENT_VIEW:]

    @staticmethod
    def _long_term_memories(entries: Sequence[MemoryRef]) -> list[MemoryRef]:
        """只投影当前查询召回的 Mem0 长期记忆，不混入未召回的运行经验引用。"""

        memories = [item for item in entries if item.memory_type == "LONG_TERM"]
        return memories[-_MAX_LONG_TERM_MEMORY_REFS_PER_AGENT_VIEW:]

    @staticmethod
    def _assignment_item(assignment: AgentAssignment) -> AssignmentContextItem:
        """把完整 Assignment 契约裁剪为无 Worker 标识和 fencing token 的摘要。"""

        return AssignmentContextItem(
            assignment_id=assignment.assignment_id,
            subtask_id=assignment.subtask_id,
            assigned_agent_type=assignment.assigned_agent_type,
            status=assignment.status,
            attempt=assignment.attempt,
            lease_expires_at=assignment.lease_expires_at,
        )

    @staticmethod
    def _artifact_item(artifact: ArtifactRef) -> ArtifactContextItem:
        """把 ArtifactRef 裁剪为不包含 content_uri 和生产者内部信息的摘要。"""

        return ArtifactContextItem(
            artifact_id=artifact.artifact_id,
            artifact_type=artifact.artifact_type,
            schema_version=artifact.schema_version,
            content_summary=artifact.content_summary,
            checksum=artifact.checksum,
            ontology_id=artifact.ontology_id,
            ontology_version=artifact.ontology_version,
            parent_artifact_ids=artifact.parent_artifact_ids,
        )
