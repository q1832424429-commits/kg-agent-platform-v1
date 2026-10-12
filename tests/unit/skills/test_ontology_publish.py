"""验证 publish_ontology 原样固化用户审核过的候选本体及发布名称。"""

import json
from uuid import uuid4

import pytest

from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.skills.ontology_publish import OntologyPublishSkill
from tests.factories import make_artifact


class FakeArtifactReader:
    """按 Artifact ID 返回本用例的候选本体与批准内容。"""

    def __init__(self, payloads: dict[object, dict[str, object]]) -> None:
        """保存不会变化的 JSON 输入。"""

        self._payloads = payloads

    async def read_content(self, artifact_id: object) -> bytes:
        """以 UTF-8 JSON 返回受控 Artifact 内容。"""

        return json.dumps(self._payloads[artifact_id], ensure_ascii=False).encode("utf-8")


@pytest.mark.asyncio
async def test_publish_ontology_preserves_candidate_and_publication_title() -> None:
    """发布阶段不得让模型或调用方重写已批准本体，只应附加版本和名称。"""

    task_id = uuid4()
    candidate = make_artifact(task_id, artifact_type="CandidateOntology")
    approval = make_artifact(task_id, artifact_type="OntologyApproval")
    registration = SkillRegistration(
        name="publish_ontology",
        version="1.0.0",
        description="test",
        owner_agent="graph_management_agent",
        supported_subtask_types=["ontology_publish"],
        allowed_agents=["graph_management_agent"],
        input_artifact_types=["CandidateOntology", "OntologyApproval"],
        output_artifact_types=["PublishedOntology"],
        parameters_schema={},
        side_effect=True,
        timeout_seconds=1,
        max_retries=0,
        implementation="LLM",
        lifecycle="ACTIVE",
    )
    candidate_payload = {
        "schema_version": "1.0.0",
        "name": "设备故障本体",
        "classes": [],
        "relations": [],
        "properties": [],
        "source_artifact_ids": [],
    }
    reader = FakeArtifactReader(
        {
            candidate.artifact_id: candidate_payload,
            approval.artifact_id: {
                "schema_version": "1.0.0",
                "target_artifact_id": str(candidate.artifact_id),
                "target_checksum": candidate.checksum,
                "decision": "APPROVED",
                "publication_title": "汽车设备故障本体 V1",
                "comment": "确认发布",
            },
        }
    )

    result = await OntologyPublishSkill(registration, artifact_reader=reader).execute(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="graph_management_agent",
            skill_name="publish_ontology",
            input_artifacts=[candidate, approval],
            parameters={"ontology_version": "2026.09"},
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    published = result.outputs[0]
    assert published.content["name"] == candidate_payload["name"]
    assert published.content["classes"] == candidate_payload["classes"]
    assert published.content["publication_title"] == "汽车设备故障本体 V1"
    assert published.content["ontology_version"] == "2026.09"
    assert published.publication_title == "汽车设备故障本体 V1"
