"""把不可变图谱 Artifact 转换成只读节点—边预览。

该服务只读取已校验的 ``CandidateGraph`` 或 ``PublishedGraph`` 内容，负责发现悬空边、
重复标识等展示风险；它不修改 Artifact、不参与审核决策，也不写入真实图数据库。
"""

from __future__ import annotations

import json
from collections import defaultdict
from typing import Protocol
from uuid import UUID

from kg_agent.domain.artifact_content_models import (
    CandidateGraphContent,
    EntityRecord,
    PublishedGraphContent,
)
from kg_agent.domain.artifact_models import ArtifactRecord
from kg_agent.domain.graph_preview_models import (
    GraphPreview,
    GraphPreviewEdge,
    GraphPreviewNode,
    GraphPreviewStatistics,
    GraphPreviewWarning,
)
from kg_agent.exceptions import ArtifactValidationError


class GraphPreviewContentReader(Protocol):
    """约束预览服务读取既有 Artifact 正文所需的最小能力。"""

    async def read_content(self, artifact_id: UUID) -> bytes:
        """返回已完成校验的 Artifact 原始字节。"""
        ...


class GraphPreviewService:
    """在 API 查询阶段按需生成图谱可视化投影，不持有任务状态。"""

    _GRAPH_TYPES = {"CandidateGraph", "PublishedGraph"}

    def __init__(self, reader: GraphPreviewContentReader) -> None:
        """绑定只读内容读取器；调用方负责 Artifact 的访问范围校验。"""

        self._reader = reader

    async def build(self, record: ArtifactRecord) -> GraphPreview:
        """读取一个图谱 Artifact 并生成适合前端绘图的节点—边视图。

        Args:
            record: 已从数据库查询出的 Artifact 元数据。

        Returns:
            不含浏览器布局状态的稳定图谱预览投影。

        Raises:
            ArtifactValidationError: Artifact 不是图谱，或其 JSON 内容不能通过图谱契约。
        """

        ref = record.ref
        if ref.artifact_type not in self._GRAPH_TYPES:
            raise ArtifactValidationError("只有 CandidateGraph 或 PublishedGraph 支持图形化预览")
        try:
            raw = await self._reader.read_content(ref.artifact_id)
            content_type = (
                PublishedGraphContent
                if ref.artifact_type == "PublishedGraph"
                else CandidateGraphContent
            )
            content = content_type.model_validate(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            raise ArtifactValidationError("图谱 Artifact 内容无法生成预览") from exc

        warnings: list[GraphPreviewWarning] = []
        entities: dict[str, EntityRecord] = {}
        for entity in content.entities:
            previous = entities.get(entity.entity_id)
            if previous is None:
                entities[entity.entity_id] = entity
            elif previous.model_dump(mode="json") != entity.model_dump(mode="json"):
                warnings.append(
                    GraphPreviewWarning(
                        code="DUPLICATE_ENTITY_ID",
                        message="存在相同实体 ID 且内容不一致，预览仅保留首个实体。",
                        affected_ids=[entity.entity_id],
                    )
                )

        properties_by_entity: dict[str, list[dict[str, object]]] = defaultdict(list)
        for property_record in content.properties:
            properties_by_entity[property_record.entity_id].append(
                property_record.model_dump(mode="json")
            )
            if property_record.entity_id not in entities:
                warnings.append(
                    GraphPreviewWarning(
                        code="ORPHAN_PROPERTY",
                        message="属性所属实体不存在，属性不会显示在节点详情中。",
                        affected_ids=[property_record.property_id, property_record.entity_id],
                    )
                )

        nodes = [
            GraphPreviewNode(
                id=entity.entity_id,
                label=entity.name,
                class_id=entity.class_id,
                aliases=entity.aliases,
                properties=properties_by_entity[entity.entity_id],
                evidence=[item.model_dump(mode="json") for item in entity.evidence],
            )
            for entity in entities.values()
        ]

        edges: list[GraphPreviewEdge] = []
        seen_relation_ids: set[str] = set()
        for relation in content.relations:
            if relation.relation_id in seen_relation_ids:
                warnings.append(
                    GraphPreviewWarning(
                        code="DUPLICATE_RELATION_ID",
                        message="存在重复关系 ID，预览仅保留第一条关系。",
                        affected_ids=[relation.relation_id],
                    )
                )
                continue
            seen_relation_ids.add(relation.relation_id)
            missing = [
                entity_id
                for entity_id in (relation.source_entity_id, relation.target_entity_id)
                if entity_id not in entities
            ]
            if missing:
                warnings.append(
                    GraphPreviewWarning(
                        code="DANGLING_RELATION",
                        message="关系端点实体不存在，该关系不会绘制。",
                        affected_ids=[relation.relation_id, *missing],
                    )
                )
                continue
            edges.append(
                GraphPreviewEdge(
                    id=relation.relation_id,
                    source=relation.source_entity_id,
                    target=relation.target_entity_id,
                    label=relation.relation_type_id,
                    relation_type_id=relation.relation_type_id,
                    evidence=[item.model_dump(mode="json") for item in relation.evidence],
                )
            )

        return GraphPreview(
            artifact_id=ref.artifact_id,
            artifact_type=ref.artifact_type,
            task_id=ref.task_id,
            nodes=nodes,
            edges=edges,
            statistics=GraphPreviewStatistics(
                entity_count=len(nodes),
                relation_count=len(edges),
                property_count=len(content.properties),
                evidence_count=len(content.evidence),
            ),
            warnings=warnings,
        )
