"""验证人工 GraphChangeSet 能确定性生成新的候选图谱。"""

import json
from uuid import uuid4

import pytest

from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.skills.graph_changes import GraphChangeSetApplySkill
from tests.factories import make_artifact


class FakeArtifactReader:
    """以 Artifact ID 为键提供不可变 JSON，模拟实际内容读取服务。"""

    def __init__(self, payloads: dict[object, dict[str, object]]) -> None:
        """保存本用例要读取的内容。"""

        self._payloads = payloads

    async def read_content(self, artifact_id: object) -> bytes:
        """返回指定 Artifact 的 JSON 字节。"""

        return json.dumps(self._payloads[artifact_id], ensure_ascii=False).encode("utf-8")


def _registration() -> SkillRegistration:
    """构造与正式 Registry 一致的 apply_graph_changeset 注册契约。"""

    return SkillRegistration(
        name="apply_graph_changeset",
        version="1.0.0",
        description="test",
        owner_agent="graph_management_agent",
        supported_subtask_types=["graph_changeset_apply"],
        allowed_agents=["graph_management_agent"],
        input_artifact_types=["CandidateGraph", "GraphChangeSet"],
        output_artifact_types=["CandidateGraph"],
        parameters_schema={},
        side_effect=False,
        timeout_seconds=1,
        max_retries=0,
        implementation="LLM",
        lifecycle="ACTIVE",
    )


@pytest.mark.asyncio
async def test_apply_graph_changeset_applies_add_update_merge_and_split_atomically() -> None:
    """同一修改集应保留血缘并把所有显式图谱操作应用到新候选图谱。"""

    task_id = uuid4()
    graph = make_artifact(task_id, artifact_type="CandidateGraph")
    changeset = make_artifact(task_id, artifact_type="GraphChangeSet")
    payloads = {
        graph.artifact_id: {
            "schema_version": "1.0.0",
            "entities": [
                {
                    "entity_id": "machine",
                    "name": "机床",
                    "class_id": "Equipment",
                    "aliases": [],
                    "evidence": [],
                },
                {
                    "entity_id": "machine-copy",
                    "name": "机床A",
                    "class_id": "Equipment",
                    "aliases": [],
                    "evidence": [],
                },
                {
                    "entity_id": "fault",
                    "name": "故障",
                    "class_id": "Fault",
                    "aliases": [],
                    "evidence": [],
                },
            ],
            "relations": [
                {
                    "relation_id": "r-1",
                    "source_entity_id": "machine-copy",
                    "relation_type_id": "HAS_FAULT",
                    "target_entity_id": "fault",
                    "evidence": [],
                }
            ],
            "properties": [
                {
                    "property_id": "p-1",
                    "entity_id": "machine-copy",
                    "property_type_id": "serial_no",
                    "value": "A-01",
                    "evidence": [],
                }
            ],
            "evidence": [],
        },
        changeset.artifact_id: {
            "schema_version": "1.0.0",
            "target_artifact_id": str(graph.artifact_id),
            "target_checksum": graph.checksum,
            "operations": [
                {
                    "operation_id": "op-merge",
                    "action": "MERGE",
                    "target_kind": "ENTITY",
                    "target_id": "machine",
                    "value": {"source_entity_ids": ["machine-copy"]},
                },
                {
                    "operation_id": "op-update",
                    "action": "UPDATE",
                    "target_kind": "ENTITY",
                    "target_id": "machine",
                    "value": {"name": "加工中心"},
                },
                {
                    "operation_id": "op-add",
                    "action": "ADD",
                    "target_kind": "RELATION",
                    "target_id": "r-2",
                    "value": {
                        "relation_id": "r-2",
                        "source_entity_id": "machine",
                        "relation_type_id": "LOCATED_AT",
                        "target_entity_id": "fault",
                        "evidence": [],
                    },
                },
                {
                    "operation_id": "op-split",
                    "action": "SPLIT",
                    "target_kind": "ENTITY",
                    "target_id": "fault",
                    "value": {
                        "entities": [
                            {
                                "entity_id": "fault-a",
                                "name": "机械故障",
                                "class_id": "Fault",
                                "aliases": [],
                                "evidence": [],
                            },
                            {
                                "entity_id": "fault-b",
                                "name": "电气故障",
                                "class_id": "Fault",
                                "aliases": [],
                                "evidence": [],
                            },
                        ]
                    },
                },
            ],
        },
    }

    result = await GraphChangeSetApplySkill(
        _registration(), artifact_reader=FakeArtifactReader(payloads)
    ).execute(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="graph_management_agent",
            skill_name="apply_graph_changeset",
            input_artifacts=[graph, changeset],
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    output = result.outputs[0]
    assert output.parent_artifact_ids == [graph.artifact_id, changeset.artifact_id]
    assert {item["entity_id"] for item in output.content["entities"]} == {
        "machine",
        "fault-a",
        "fault-b",
    }
    assert next(item for item in output.content["entities"] if item["entity_id"] == "machine")[
        "aliases"
    ] == ["机床A"]
    assert {item["target_entity_id"] for item in output.content["relations"]} == {"fault-a"}
    assert output.content["properties"][0]["entity_id"] == "machine"


@pytest.mark.asyncio
async def test_apply_graph_changeset_rejects_stale_target_without_partial_output() -> None:
    """ChangeSet 的目标校验和不匹配时必须拒绝，不能悄然修改更新后的图谱。"""

    task_id = uuid4()
    graph = make_artifact(task_id, artifact_type="CandidateGraph")
    changeset = make_artifact(task_id, artifact_type="GraphChangeSet")
    payloads = {
        graph.artifact_id: {
            "schema_version": "1.0.0",
            "entities": [],
            "relations": [],
            "properties": [],
            "evidence": [],
        },
        changeset.artifact_id: {
            "schema_version": "1.0.0",
            "target_artifact_id": str(graph.artifact_id),
            "target_checksum": "sha256:stale",
            "operations": [],
        },
    }

    result = await GraphChangeSetApplySkill(
        _registration(), artifact_reader=FakeArtifactReader(payloads)
    ).execute(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="graph_management_agent",
            skill_name="apply_graph_changeset",
            input_artifacts=[graph, changeset],
        )
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error_code == "GRAPH_CHANGESET_APPLY_FAILED"
    assert result.outputs == []
