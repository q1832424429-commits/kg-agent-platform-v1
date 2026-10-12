"""验证总控和专业 Agent 只能读取其角色所需的上下文字段。"""

from uuid import uuid4

import pytest

from kg_agent.exceptions import AssignmentConflictError
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.services.context_view_service import ContextViewService
from kg_agent.services.postgres_context_service import PostgresContextService
from tests.factories import make_artifact, make_decomposition, make_subtask


@pytest.mark.asyncio
async def test_professional_view_filters_artifacts_and_content_uri(database) -> None:
    """专业 Agent 只能看到子任务输入 Artifact，且不能看到内容存储地址。"""

    task_id = uuid4()
    artifact = make_artifact(task_id)
    unrelated = make_artifact(task_id)
    subtask = make_subtask(task_id, inputs=[artifact.artifact_id])
    context = await PostgresContextService(database.session_factory).publish_decomposition(
        conversation_id=uuid4(),
        result=make_decomposition(task_id, subtasks=[subtask]),
        source_artifacts=[artifact, unrelated],
        expected_version=None,
    )
    assignment_id = context.assignment_ids[0]

    async with database.session_factory() as session:
        service = ContextViewService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
        )
        view = await service.build_professional_view(
            assignment_id=assignment_id,
            requested_agent_type="document_parsing_agent",
        )
        serialized = view.model_dump_json()
        assert [item.artifact_id for item in view.input_artifacts] == [artifact.artifact_id]
        assert artifact.content_uri not in serialized
        assert str(unrelated.artifact_id) not in serialized

        with pytest.raises(AssignmentConflictError):
            await service.build_professional_view(
                assignment_id=assignment_id,
                requested_agent_type="knowledge_extraction_agent",
            )
