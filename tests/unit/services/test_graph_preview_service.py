"""验证图谱 Artifact 可以投影为安全的节点—边预览。"""

import json
from uuid import uuid4

import pytest

from kg_agent.domain.artifact_models import ArtifactRecord
from kg_agent.services.graph_preview_service import GraphPreviewService
from tests.factories import make_artifact


class FakeArtifactReader:
    """为预览服务提供按 Artifact ID 查找的确定性 JSON 内容。"""

    def __init__(self, payloads: dict[object, dict[str, object]]) -> None:
        """保存测试输入，不访问对象存储。"""

        self._payloads = payloads

    async def read_content(self, artifact_id: object) -> bytes:
        """返回指定 Artifact 的 JSON 字节。"""

        return json.dumps(self._payloads[artifact_id], ensure_ascii=False).encode("utf-8")


@pytest.mark.asyncio
async def test_graph_preview_builds_nodes_edges_properties_and_evidence() -> None:
    """候选图谱预览应把实体、关系、属性和证据映射为可显示数据。"""

    task_id = uuid4()
    ref = make_artifact(task_id, artifact_type="CandidateGraph")
    record = ArtifactRecord(
        ref=ref, object_key="test/graph", media_type="application/json", size_bytes=1
    )
    payload = {
        "schema_version": "1.0.0",
        "entities": [
            {
                "entity_id": "machine-1",
                "name": "冲压机",
                "class_id": "Equipment",
                "aliases": ["A机"],
                "evidence": [
                    {"source_artifact_id": str(uuid4()), "locator": "page:1", "quote": "冲压机"}
                ],
            },
            {
                "entity_id": "fault-1",
                "name": "温度异常",
                "class_id": "Fault",
                "aliases": [],
                "evidence": [],
            },
        ],
        "relations": [
            {
                "relation_id": "rel-1",
                "source_entity_id": "machine-1",
                "relation_type_id": "HAS_FAULT",
                "target_entity_id": "fault-1",
                "evidence": [],
            }
        ],
        "properties": [
            {
                "property_id": "property-1",
                "entity_id": "machine-1",
                "property_type_id": "manufacturer",
                "value": "示例厂商",
                "evidence": [],
            }
        ],
        "evidence": [],
    }

    preview = await GraphPreviewService(FakeArtifactReader({ref.artifact_id: payload})).build(
        record
    )

    assert preview.statistics.model_dump() == {
        "entity_count": 2,
        "relation_count": 1,
        "property_count": 1,
        "evidence_count": 0,
    }
    assert preview.nodes[0].label == "冲压机"
    assert preview.nodes[0].properties[0]["value"] == "示例厂商"
    assert preview.edges[0].source == "machine-1"
    assert preview.edges[0].target == "fault-1"
    assert preview.warnings == []


@pytest.mark.asyncio
async def test_graph_preview_reports_dangling_relation_without_crashing() -> None:
    """历史脏数据的悬空关系应提示用户且不能破坏整个预览。"""

    task_id = uuid4()
    ref = make_artifact(task_id, artifact_type="CandidateGraph")
    record = ArtifactRecord(
        ref=ref, object_key="test/graph", media_type="application/json", size_bytes=1
    )
    payload = {
        "schema_version": "1.0.0",
        "entities": [],
        "relations": [
            {
                "relation_id": "rel-orphan",
                "source_entity_id": "missing-a",
                "relation_type_id": "RELATED_TO",
                "target_entity_id": "missing-b",
                "evidence": [],
            }
        ],
        "properties": [],
        "evidence": [],
    }

    preview = await GraphPreviewService(FakeArtifactReader({ref.artifact_id: payload})).build(
        record
    )

    assert preview.edges == []
    assert preview.warnings[0].code == "DANGLING_RELATION"
