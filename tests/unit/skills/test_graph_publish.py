"""验证 publish_graph 原样发布已批准的候选图谱。"""

import json
from uuid import uuid4

import pytest

from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.skills.graph_publish import GraphPublishSkill
from tests.factories import make_artifact


class FakeArtifactReader:
    """按 Artifact ID 返回候选图谱和 Approval JSON。"""

    def __init__(self, payloads: dict[object, dict[str, object]]) -> None:
        """保存本用例固定输入。"""

        self._payloads = payloads

    async def read_content(self, artifact_id: object) -> bytes:
        """返回指定 Artifact 的 UTF-8 JSON。"""

        return json.dumps(self._payloads[artifact_id], ensure_ascii=False).encode("utf-8")


@pytest.mark.asyncio
async def test_publish_graph_preserves_approved_candidate_content() -> None:
    """发布不得让任何生成式实现重写用户已经批准的实体关系。"""

    task_id = uuid4()
    candidate = make_artifact(task_id, artifact_type="CandidateGraph")
    approval = make_artifact(task_id, artifact_type="GraphApproval")
    registration = SkillRegistration(
        name="publish_graph",
        version="1.0.0",
        description="test",
        owner_agent="graph_management_agent",
        supported_subtask_types=["graph_publish"],
        allowed_agents=["graph_management_agent"],
        input_artifact_types=["CandidateGraph", "GraphApproval"],
        output_artifact_types=["PublishedGraph"],
        parameters_schema={},
        side_effect=True,
        timeout_seconds=1,
        max_retries=0,
        implementation="LLM",
        lifecycle="ACTIVE",
    )
    candidate_payload = {
        "schema_version": "1.0.0",
        "entities": [
            {
                "entity_id": "machine-1",
                "name": "数控机床",
                "class_id": "Equipment",
                "aliases": ["CNC"],
                "evidence": [],
            }
        ],
        "relations": [
            {
                "relation_id": "self-1",
                "source_entity_id": "machine-1",
                "relation_type_id": "SAME_AS",
                "target_entity_id": "machine-1",
                "evidence": [],
            }
        ],
        "properties": [
            {
                "property_id": "serial-1",
                "entity_id": "machine-1",
                "property_type_id": "serial_no",
                "value": "SN-001",
                "evidence": [],
            }
        ],
        "evidence": [],
    }
    reader = FakeArtifactReader(
        {
            candidate.artifact_id: candidate_payload,
            approval.artifact_id: {
                "schema_version": "1.0.0",
                "target_artifact_id": str(candidate.artifact_id),
                "target_checksum": candidate.checksum,
                "decision": "APPROVED",
                "publication_title": "设备故障图谱 V1",
                "comment": "同意发布",
            },
        }
    )

    result = await GraphPublishSkill(registration, artifact_reader=reader).execute(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="graph_management_agent",
            skill_name="publish_graph",
            input_artifacts=[candidate, approval],
            parameters={"graph_version": "2026.09"},
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    published = result.outputs[0]
    assert published.parent_artifact_ids == [candidate.artifact_id, approval.artifact_id]
    assert published.content["entities"] == candidate_payload["entities"]
    assert published.content["relations"] == candidate_payload["relations"]
    assert published.content["properties"] == candidate_payload["properties"]
    assert published.content["graph_version"] == "2026.09"
    assert published.content["publication_title"] == "设备故障图谱 V1"
    assert published.content["approval_artifact_id"] == str(approval.artifact_id)
