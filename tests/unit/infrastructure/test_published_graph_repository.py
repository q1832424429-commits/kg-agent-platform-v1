"""验证 PublishedGraph Repository 生成完整 PostgreSQL 节点—边投影。"""

from typing import Any, cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from kg_agent.domain.artifact_content_models import PublishedGraphContent
from kg_agent.domain.artifact_models import ArtifactRecord
from kg_agent.infrastructure.database.models import (
    PublishedGraphEdgeRow,
    PublishedGraphEvidenceRow,
    PublishedGraphNodeRow,
    PublishedGraphPropertyRow,
    PublishedGraphRow,
)
from kg_agent.infrastructure.database.published_graph_repository import (
    SqlAlchemyPublishedGraphRepository,
)
from tests.factories import make_artifact


class RecordingSession:
    """记录 Repository 准备交给 SQLAlchemy 的行，不连接真实数据库。"""

    def __init__(self) -> None:
        """初始化空投影和幂等查询结果。"""

        self.rows: list[Any] = []
        self.graph: PublishedGraphRow | None = None

    async def scalar(self, _query: Any) -> PublishedGraphRow | None:
        """模拟按 Artifact ID 查询已存在图版本。"""

        return self.graph

    def add(self, row: Any) -> None:
        """记录单行，并让后续幂等查询命中图版本。"""

        self.rows.append(row)
        if isinstance(row, PublishedGraphRow):
            self.graph = row

    def add_all(self, rows: list[Any]) -> None:
        """记录一批节点、边、属性或证据行。"""

        self.rows.extend(rows)

    async def flush(self) -> None:
        """模拟 SQLAlchemy flush；本测试只检查映射结果。"""


@pytest.mark.asyncio
async def test_repository_builds_idempotent_queryable_graph_projection() -> None:
    """发布内容应展开为一个图版本、两个节点及对应边、属性和证据。"""

    task_id = uuid4()
    candidate_id = uuid4()
    approval_id = uuid4()
    artifact_ref = make_artifact(task_id, artifact_type="PublishedGraph")
    artifact = ArtifactRecord(
        ref=artifact_ref,
        object_key="tasks/test/published.json",
        media_type="application/json",
        size_bytes=100,
    )
    source_id = uuid4()
    graph = PublishedGraphContent.model_validate(
        {
            "schema_version": "1.0.0",
            "graph_id": str(uuid4()),
            "graph_version": "1.0.0",
            "approval_artifact_id": str(approval_id),
            "entities": [
                {
                    "entity_id": "e-1",
                    "name": "设备",
                    "class_id": "Equipment",
                    "aliases": ["机器"],
                    "evidence": [],
                },
                {
                    "entity_id": "e-2",
                    "name": "故障",
                    "class_id": "Fault",
                    "aliases": [],
                    "evidence": [],
                },
            ],
            "relations": [
                {
                    "relation_id": "r-1",
                    "source_entity_id": "e-1",
                    "relation_type_id": "HAS_FAULT",
                    "target_entity_id": "e-2",
                    "evidence": [],
                }
            ],
            "properties": [
                {
                    "property_id": "p-1",
                    "entity_id": "e-1",
                    "property_type_id": "serial_no",
                    "value": "SN-001",
                    "evidence": [],
                }
            ],
            "evidence": [
                {
                    "evidence_id": "ev-1",
                    "pointer": {
                        "source_artifact_id": str(source_id),
                        "locator": "page:1",
                        "quote": "设备发生故障",
                    },
                    "confidence": 0.98,
                }
            ],
        }
    )
    session = RecordingSession()
    repository = SqlAlchemyPublishedGraphRepository(cast(AsyncSession, session))

    await repository.ensure_persisted(
        artifact=artifact,
        graph=graph,
        source_candidate_artifact_id=candidate_id,
    )
    first_count = len(session.rows)
    await repository.ensure_persisted(
        artifact=artifact,
        graph=graph,
        source_candidate_artifact_id=candidate_id,
    )

    assert len(session.rows) == first_count
    assert sum(isinstance(row, PublishedGraphRow) for row in session.rows) == 1
    assert sum(isinstance(row, PublishedGraphNodeRow) for row in session.rows) == 2
    assert sum(isinstance(row, PublishedGraphEdgeRow) for row in session.rows) == 1
    assert sum(isinstance(row, PublishedGraphPropertyRow) for row in session.rows) == 1
    assert sum(isinstance(row, PublishedGraphEvidenceRow) for row in session.rows) == 1
    edge = next(row for row in session.rows if isinstance(row, PublishedGraphEdgeRow))
    assert edge.source_entity_id == "e-1"
    assert edge.target_entity_id == "e-2"
