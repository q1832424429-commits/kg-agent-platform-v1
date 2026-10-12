"""验证 PostgreSQL 复用入口对受控跨任务资产补齐权威父级 checksum。"""

from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from kg_agent.domain.artifact_models import ArtifactRecord, ArtifactRef, ArtifactReuseRequest
from kg_agent.services.postgres_artifact_reuse_service import PostgresArtifactReuseService
from tests.factories import make_artifact


class _SessionContext:
    """提供无需真实数据库的异步 Session 上下文。"""

    async def __aenter__(self) -> object:
        """返回仅用于构造 Repository 的占位 Session。"""

        return object()

    async def __aexit__(self, *args: object) -> None:
        """结束测试上下文，不执行额外清理。"""


@pytest.mark.asyncio
async def test_controlled_published_ontology_import_uses_database_parent_checksum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """内部续跑时局部 Context 缺少跨任务父级，不应误报 checksum 不一致。"""

    source_task_id, target_task_id = uuid4(), uuid4()
    ontology_id = uuid4()
    source = make_artifact(source_task_id, artifact_type="PublishedOntology").model_copy(
        update={
            "ontology_id": ontology_id,
            "ontology_version": "1.0.0",
            "checksum": "sha256:published-source",
        }
    )
    imported = make_artifact(target_task_id, artifact_type="PublishedOntology").model_copy(
        update={
            "producer_agent": "system",
            "producer_skill": "published_asset_import",
            "parent_artifact_ids": [source.artifact_id],
            "ontology_id": ontology_id,
            "ontology_version": "1.0.0",
        }
    )
    record = ArtifactRecord(
        ref=imported,
        object_key="artifacts/imported.json",
        media_type="application/json",
        size_bytes=1,
    )

    class _Repository:
        """返回一个受控导入副本及其跨任务来源。"""

        def __init__(self, session: object) -> None:
            """接收占位 Session，以匹配正式 Repository 枃造方式。"""

            del session

        async def get(self, artifact_id: UUID) -> ArtifactRecord | None:
            """返回当前待校验的导入记录。"""

            assert artifact_id == imported.artifact_id
            return record

        async def get_parent_refs(self, artifact_id: UUID) -> list[ArtifactRef]:
            """返回 PostgreSQL 权威跨任务父级快照。"""

            assert artifact_id == imported.artifact_id
            return [source]

    monkeypatch.setattr(
        "kg_agent.services.postgres_artifact_reuse_service.SqlAlchemyArtifactRepository",
        _Repository,
    )
    service = PostgresArtifactReuseService(cast(Any, lambda: _SessionContext()))

    decision = await service.evaluate(
        ArtifactReuseRequest(
            artifact_id=imported.artifact_id,
            expected_artifact_type="PublishedOntology",
            expected_schema_version="1.0.0",
            compatible_task_spec_revisions={1},
            expected_parent_checksums=set(),
            expected_ontology_id=ontology_id,
            expected_ontology_version="1.0.0",
        )
    )

    assert decision.reusable is True
    assert decision.reason_codes == []
