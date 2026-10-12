"""验证已发布本体和图谱只能通过受控副本跨任务复用。

本文件只测试 ArtifactService 的跨任务边界，不依赖 PostgreSQL、MinIO 或外部模型。
"""

import json
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from kg_agent.domain.artifact_models import ArtifactCreateCommand, ArtifactRecord
from kg_agent.exceptions import ArtifactValidationError
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.services.artifact_service import ArtifactService
from tests.factories import make_artifact


class ArtifactLookup:
    """为单元测试提供只读 Artifact 查询。"""

    def __init__(self, records: list[ArtifactRecord]) -> None:
        """按 Artifact ID 建立测试记录索引。"""

        self._records = {item.ref.artifact_id: item for item in records}

    async def get(self, artifact_id: UUID, *, for_update: bool = False) -> ArtifactRecord | None:
        """返回指定记录；for_update 在无数据库测试中不产生额外行为。"""

        del for_update
        return self._records.get(artifact_id)


def _service(
    records: list[ArtifactRecord], storage: InMemoryObjectStorage
) -> ArtifactService:
    """构造只用于调用血缘校验方法的 ArtifactService。"""

    unused = cast(Any, object())
    return ArtifactService(
        artifact_repository=cast(Any, ArtifactLookup(records)),
        task_repository=unused,
        assignment_repository=unused,
        context_repository=unused,
        event_repository=unused,
        storage=storage,
        registry=ArtifactRegistry.v1(),
        max_file_bytes=10_000_000,
        max_files_per_task=10,
        max_total_bytes_per_task=100_000_000,
    )


async def _source_record(
    *,
    storage: InMemoryObjectStorage,
    task_id: UUID,
    artifact_type: str,
    payload: dict[str, Any],
) -> ArtifactRecord:
    """把已发布内容写入内存对象存储并返回对应元数据。"""

    ref = make_artifact(task_id, artifact_type=artifact_type)
    object_key = f"published/{ref.artifact_id}.json"
    encoded = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    await storage.put_bytes(object_key, encoded, "application/json")
    return ArtifactRecord(
        ref=ref,
        object_key=object_key,
        media_type="application/json",
        size_bytes=len(encoded),
    )


@pytest.mark.asyncio
async def test_import_identical_published_ontology_into_another_task() -> None:
    """内容完全一致的 PublishedOntology 可以复制为新任务输入。"""

    storage = InMemoryObjectStorage()
    source_task_id, target_task_id = uuid4(), uuid4()
    payload = {
        "schema_version": "1.0.0",
        "name": "汽车制造本体",
        "classes": [],
        "relations": [],
        "properties": [],
        "source_artifact_ids": [],
        "ontology_id": str(uuid4()),
        "ontology_version": "1.0.0",
        "approval_artifact_id": str(uuid4()),
    }
    source = await _source_record(
        storage=storage,
        task_id=source_task_id,
        artifact_type="PublishedOntology",
        payload=payload,
    )
    service = _service([source], storage)
    command = ArtifactCreateCommand(
        artifact_type="PublishedOntology",
        task_id=target_task_id,
        task_spec_revision=1,
        producer_agent="system",
        producer_skill="published_asset_import",
        producer_run_id=uuid4(),
        parent_artifact_ids=[source.ref.artifact_id],
        ontology_id=UUID(payload["ontology_id"]),
        ontology_version="1.0.0",
        content_summary="复用已发布本体",
        content=payload,
    )

    parents = await service._validate_parents(command, command.parent_artifact_ids)
    await service._validate_protected_parents(command, parents)


@pytest.mark.asyncio
async def test_reject_modified_published_ontology_import() -> None:
    """导入时修改已发布本体内容必须被拒绝。"""

    storage = InMemoryObjectStorage()
    payload = {
        "schema_version": "1.0.0",
        "name": "原始本体",
        "classes": [],
        "relations": [],
        "properties": [],
        "source_artifact_ids": [],
        "ontology_id": str(uuid4()),
        "ontology_version": "1.0.0",
        "approval_artifact_id": str(uuid4()),
    }
    source = await _source_record(
        storage=storage,
        task_id=uuid4(),
        artifact_type="PublishedOntology",
        payload=payload,
    )
    service = _service([source], storage)
    modified = dict(payload)
    modified["name"] = "被篡改的本体"
    command = ArtifactCreateCommand(
        artifact_type="PublishedOntology",
        task_id=uuid4(),
        task_spec_revision=1,
        producer_agent="system",
        producer_skill="published_asset_import",
        producer_run_id=uuid4(),
        parent_artifact_ids=[source.ref.artifact_id],
        content_summary="非法修改",
        content=modified,
    )

    parents = await service._validate_parents(command, command.parent_artifact_ids)
    with pytest.raises(ArtifactValidationError, match="完全一致"):
        await service._validate_protected_parents(command, parents)


@pytest.mark.asyncio
async def test_import_published_graph_as_candidate_baseline() -> None:
    """PublishedGraph 只能去除发布元数据后复制为 CandidateGraph。"""

    storage = InMemoryObjectStorage()
    source_payload = {
        "schema_version": "1.0.0",
        "entities": [],
        "relations": [],
        "properties": [],
        "evidence": [],
        "graph_id": str(uuid4()),
        "graph_version": "2.0.0",
        "approval_artifact_id": str(uuid4()),
    }
    source = await _source_record(
        storage=storage,
        task_id=uuid4(),
        artifact_type="PublishedGraph",
        payload=source_payload,
    )
    service = _service([source], storage)
    candidate_payload = {
        key: value
        for key, value in source_payload.items()
        if key not in {"graph_id", "graph_version", "approval_artifact_id"}
    }
    command = ArtifactCreateCommand(
        artifact_type="CandidateGraph",
        task_id=uuid4(),
        task_spec_revision=1,
        producer_agent="system",
        producer_skill="published_graph_baseline_import",
        producer_run_id=uuid4(),
        parent_artifact_ids=[source.ref.artifact_id],
        content_summary="图谱增量候选基线",
        content=candidate_payload,
    )

    parents = await service._validate_parents(command, command.parent_artifact_ids)
    await service._validate_protected_parents(command, parents)


@pytest.mark.asyncio
async def test_reject_arbitrary_cross_task_lineage() -> None:
    """除两种已发布资产导入外，其他 Artifact 仍禁止跨 MasterTask。"""

    storage = InMemoryObjectStorage()
    source = await _source_record(
        storage=storage,
        task_id=uuid4(),
        artifact_type="SemanticDocument",
        payload={"schema_version": "1.0.0"},
    )
    service = _service([source], storage)
    command = ArtifactCreateCommand(
        artifact_type="EntityDataset",
        task_id=uuid4(),
        task_spec_revision=1,
        producer_agent="system",
        producer_skill="illegal_import",
        producer_run_id=uuid4(),
        parent_artifact_ids=[source.ref.artifact_id],
        content_summary="非法跨任务血缘",
        content={"schema_version": "1.0.0", "entities": []},
    )

    with pytest.raises(ArtifactValidationError, match="不能跨 MasterTask"):
        await service._validate_parents(command, command.parent_artifact_ids)
