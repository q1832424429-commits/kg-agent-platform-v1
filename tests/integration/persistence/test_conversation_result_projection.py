"""验证正式会话把已提交的目标 Artifact 投影为可点击的 Agent 回复。"""

from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

import pytest

from kg_agent.infrastructure.database.models import (
    ArtifactRefRow,
    ConversationRow,
    MasterTaskRow,
    TaskSpecRevisionRow,
)
from kg_agent.infrastructure.database.session import Database
from kg_agent.services.conversation_service import ConversationService
from tests.factories import NOW


@pytest.mark.anyio
async def test_completed_and_final_candidate_artifacts_appear_in_chat(tmp_path) -> None:
    """已完成任务和仅需候选物的任务均可在刷新后查看真实产物。"""

    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'conversation-result.db'}")
    await database.create_schema()
    conversation_id = uuid4()
    completed_id, candidate_id = uuid4(), uuid4()
    graph_id, ontology_id = uuid4(), uuid4()
    async with database.session_factory() as session, session.begin():
        session.add(
            ConversationRow(
                conversation_id=conversation_id,
                title="设备知识构建",
                created_at=NOW,
                updated_at=NOW,
            )
        )
        for task_id, status, target_type, artifact_id, offset in (
            (completed_id, "SUCCEEDED", "PublishedGraph", graph_id, 1),
            (candidate_id, "WAITING_HUMAN", "CandidateOntology", ontology_id, 2),
        ):
            created = NOW + timedelta(minutes=offset)
            session.add(
                MasterTaskRow(
                    task_id=task_id,
                    conversation_id=conversation_id,
                    workspace_id="default",
                    user_id="default-user",
                    title=target_type,
                    current_task_spec_revision=1,
                    current_decomposition_revision=1,
                    status=status,
                    created_at=created,
                    updated_at=created,
                    completed_at=created if status == "SUCCEEDED" else None,
                )
            )
            session.add(
                TaskSpecRevisionRow(
                    task_id=task_id,
                    revision=1,
                    task_intent="START",
                    payload={"target_output_types": [target_type]},
                    created_at=created,
                )
            )
            session.add(
                ArtifactRefRow(
                    artifact_id=artifact_id,
                    task_id=task_id,
                    artifact_type=target_type,
                    status="AVAILABLE",
                    payload={"content_summary": f"{target_type} 结果"},
                    created_at=created,
                )
            )
    service = ConversationService(
        database.session_factory,
        storage=SimpleNamespace(),  # type: ignore[arg-type]
        memory=SimpleNamespace(),  # type: ignore[arg-type]
        run_logs=SimpleNamespace(),  # type: ignore[arg-type]
    )
    first = await service.conversation_snapshot(conversation_id)
    second = await service.conversation_snapshot(conversation_id)
    assert first is not None and second is not None
    assert [item["message_id"] for item in first["messages"]] == [
        item["message_id"] for item in second["messages"]
    ]
    assert {ref["artifact_id"] for item in first["messages"] for ref in item["artifact_refs"]} == {
        graph_id,
        ontology_id,
    }
    assert "候选结果已生成" in first["messages"][1]["content"]
    await database.dispose()
