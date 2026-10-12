"""验证专业 Agent 提交使用与 Worker 加载一致的有效 Artifact 集合。"""

from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.enums import AgentRunStatus
from kg_agent.services.agent_run_service import AgentRunService
from tests.factories import NOW, make_artifact, make_subtask


@pytest.mark.asyncio
async def test_submit_accepts_replacement_and_all_untouched_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """实体新版本替代旧版后，提交仍应接受关系和证据等未覆盖产物。"""

    task_id = uuid4()
    subtask = make_subtask(
        task_id,
        subtask_type="conflict_analyze",
        agent_type="knowledge_fusion_agent",
        outputs=["ConflictReport"],
    )
    old = make_artifact(task_id, artifact_type="EntityDataset")
    new = make_artifact(task_id, artifact_type="EntityDataset").model_copy(
        update={"parent_artifact_ids": [old.artifact_id]}
    )
    relation = make_artifact(task_id, artifact_type="RelationDataset")
    evidence = make_artifact(task_id, artifact_type="EvidenceDataset")
    candidates = [old, new, relation, evidence]
    by_id = {item.artifact_id: item for item in candidates}

    async def candidate_ids(**_kwargs: object) -> list:
        """模拟已成功祖先任务解析出的全部候选产物 ID。"""

        return [item.artifact_id for item in candidates]

    monkeypatch.setattr(
        "kg_agent.services.agent_run_service.resolve_candidate_input_ids", candidate_ids
    )
    tasks = SimpleNamespace(
        get_subtask=AsyncMock(return_value=subtask),
        get_artifact_ref=AsyncMock(side_effect=lambda artifact_id: by_id.get(artifact_id)),
    )
    runs = SimpleNamespace(
        get_result=AsyncMock(return_value=None),
        add_result=AsyncMock(),
        add_trace=AsyncMock(),
    )
    assignments = SimpleNamespace(
        validate_submission=AsyncMock(),
        complete=AsyncMock(),
        append_memories=AsyncMock(),
    )
    registration = SimpleNamespace(
        accepted_input_types={
            "EntityDataset",
            "RelationDataset",
            "EvidenceDataset",
            "ConflictReport",
        },
        allowed_skills=[],
    )
    agents = SimpleNamespace(require=lambda _name: registration)
    result = AgentRunResult(
        task_id=task_id,
        subtask_id=subtask.subtask_id,
        assignment_id=uuid4(),
        agent_name="knowledge_fusion_agent",
        run_id=uuid4(),
        status=AgentRunStatus.FAILED,
        input_artifact_ids=[new.artifact_id, relation.artifact_id, evidence.artifact_id],
        execution_trace_id=uuid4(),
        started_at=NOW,
        finished_at=NOW,
    )
    service = AgentRunService(
        runs=cast(Any, runs),
        tasks=cast(Any, tasks),
        assignments=cast(Any, assignments),
        agents=cast(Any, agents),
        skills=cast(Any, SimpleNamespace()),
    )

    await service.submit(result=result, worker_id="worker-1", fencing_token=1)

    runs.add_result.assert_awaited_once_with(result)
    assignments.complete.assert_awaited_once()
