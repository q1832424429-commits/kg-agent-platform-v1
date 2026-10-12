"""将已发布图谱 Artifact 投影为 PostgreSQL 可查询的节点、边和属性表。

本 Repository 只在调用方现有事务中执行持久化，不读取 MinIO、不判断发布权限，也不替代
PublishedGraph Artifact。发布权限继续由 PublishGuard 和 ArtifactService 的血缘校验负责。
"""

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from kg_agent.domain.artifact_content_models import PublishedGraphContent
from kg_agent.domain.artifact_models import ArtifactRecord
from kg_agent.exceptions import PersistenceError
from kg_agent.infrastructure.database.models import (
    PublishedGraphEdgeRow,
    PublishedGraphEvidenceRow,
    PublishedGraphNodeRow,
    PublishedGraphPropertyRow,
    PublishedGraphRow,
)


class SqlAlchemyPublishedGraphRepository:
    """在同一数据库事务内保存不可变 PublishedGraph 的关系型图投影。"""

    def __init__(self, session: AsyncSession) -> None:
        """绑定由应用服务管理生命周期的异步 Session。"""

        self._session = session

    async def ensure_persisted(
        self,
        *,
        artifact: ArtifactRecord,
        graph: PublishedGraphContent,
        source_candidate_artifact_id: UUID,
    ) -> None:
        """幂等保存图版本、节点、边、属性和证据。

        参数：
        - artifact：刚创建或幂等重放的 PublishedGraph Artifact 元数据。
        - graph：已经通过 PublishedGraphContent 严格校验的完整内容。
        - source_candidate_artifact_id：该发布版本唯一的 CandidateGraph 父 Artifact。

        若相同 Artifact 已有投影则只校验版本身份；同一 Artifact 指向不同图版本时抛出
        ``PersistenceError``，由调用方回滚整个发布事务。
        """

        existing = await self._session.scalar(
            select(PublishedGraphRow).where(
                PublishedGraphRow.artifact_id == artifact.ref.artifact_id
            )
        )
        if existing is not None:
            if (
                existing.graph_id != graph.graph_id
                or existing.task_id != artifact.ref.task_id
                or existing.graph_version != graph.graph_version
                or existing.source_candidate_artifact_id != source_candidate_artifact_id
                or existing.approval_artifact_id != graph.approval_artifact_id
            ):
                raise PersistenceError("PublishedGraph Artifact 已存在不一致的 PostgreSQL 图投影")
            return

        self._session.add(
            PublishedGraphRow(
                graph_id=graph.graph_id,
                artifact_id=artifact.ref.artifact_id,
                task_id=artifact.ref.task_id,
                graph_version=graph.graph_version,
                source_candidate_artifact_id=source_candidate_artifact_id,
                approval_artifact_id=graph.approval_artifact_id,
                created_at=artifact.ref.created_at,
            )
        )
        await self._session.flush()

        self._session.add_all(
            [
                PublishedGraphNodeRow(
                    graph_id=graph.graph_id,
                    entity_id=item.entity_id,
                    name=item.name,
                    class_id=item.class_id,
                    aliases=item.aliases,
                    evidence=[pointer.model_dump(mode="json") for pointer in item.evidence],
                )
                for item in graph.entities
            ]
        )
        # 边和属性使用复合外键引用节点，必须先 flush 全部节点。
        await self._session.flush()

        self._session.add_all(
            [
                PublishedGraphEdgeRow(
                    graph_id=graph.graph_id,
                    relation_id=item.relation_id,
                    source_entity_id=item.source_entity_id,
                    relation_type_id=item.relation_type_id,
                    target_entity_id=item.target_entity_id,
                    evidence=[pointer.model_dump(mode="json") for pointer in item.evidence],
                )
                for item in graph.relations
            ]
        )
        self._session.add_all(
            [
                PublishedGraphPropertyRow(
                    graph_id=graph.graph_id,
                    property_id=item.property_id,
                    entity_id=item.entity_id,
                    property_type_id=item.property_type_id,
                    value=item.value,
                    evidence=[pointer.model_dump(mode="json") for pointer in item.evidence],
                )
                for item in graph.properties
            ]
        )
        self._session.add_all(
            [
                PublishedGraphEvidenceRow(
                    graph_id=graph.graph_id,
                    evidence_id=item.evidence_id,
                    source_artifact_id=item.pointer.source_artifact_id,
                    locator=item.pointer.locator,
                    quote=item.pointer.quote,
                    confidence=item.confidence,
                )
                for item in graph.evidence
            ]
        )
        await self._session.flush()
