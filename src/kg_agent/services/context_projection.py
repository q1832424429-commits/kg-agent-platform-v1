"""从关系事实构建版本化 SharedTaskContext 投影。"""

from datetime import datetime
from uuid import UUID

from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.memory_models import ConversationContextRef
from kg_agent.exceptions import PersistenceError
from kg_agent.repositories.interfaces import (
    AssignmentRepository,
    ContextRepository,
    TaskRepository,
)


async def build_context_projection(
    *,
    task_id: UUID,
    version: int,
    updated_at: datetime,
    task_repository: TaskRepository,
    assignment_repository: AssignmentRepository,
    context_repository: ContextRepository,
) -> SharedTaskContext:
    """根据关系事实构建新快照，并保留后续阶段已经写入的引用字段。"""

    master = await task_repository.get_master(task_id)
    if master is None:
        raise PersistenceError("构建 SharedContext 时 MasterTask 不存在")
    task_spec = await task_repository.get_task_spec(task_id, master.current_task_spec_revision)
    if task_spec is None:
        raise PersistenceError("构建 SharedContext 时当前 TaskSpec 不存在")

    previous = await context_repository.get(task_id)
    subtasks = await task_repository.list_subtasks(task_id)
    assignments = await assignment_repository.list_for_task(task_id)
    artifacts = await task_repository.list_artifact_refs(task_id)
    return SharedTaskContext(
        task_id=task_id,
        version=version,
        conversation=(
            previous.conversation
            if previous is not None
            else ConversationContextRef(
                conversation_id=master.conversation_id,
                summary=task_spec.business_goal,
            )
        ),
        master_task=master,
        current_task_spec=task_spec,
        task_spec_revision_refs=await task_repository.list_task_spec_revisions(task_id),
        subtask_ids=[item.subtask_id for item in subtasks],
        assignment_ids=[item.assignment_id for item in assignments],
        artifact_refs=artifacts,
        agent_run_result_ids=previous.agent_run_result_ids if previous else [],
        human_review_request_ids=previous.human_review_request_ids if previous else [],
        short_term_memories=previous.short_term_memories if previous else [],
        long_term_memory_refs=previous.long_term_memory_refs if previous else [],
        execution_trace_ids=previous.execution_trace_ids if previous else [],
        updated_at=updated_at,
    )
