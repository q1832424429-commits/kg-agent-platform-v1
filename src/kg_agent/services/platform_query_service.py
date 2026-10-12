"""提供任务、事件、运行轨迹和 Artifact 的只读查询。"""

from typing import Any
from uuid import UUID

from sqlalchemy import exists, or_, select

from kg_agent.domain.enums import TaskEventType
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.models import (
    ArtifactContentRow,
    ArtifactRefRow,
    MasterTaskRow,
    TaskEventRow,
)
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAgentRunRepository,
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyHumanReviewRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.database.session import Database
from kg_agent.services.postgres_artifact_service import PostgresArtifactService


class PlatformQueryService:
    """从持久化事实构造不含隐藏思维过程的公开查询结果。"""

    def __init__(self, database: Database, artifacts: PostgresArtifactService) -> None:
        self._database, self._artifacts = database, artifacts

    async def list_tasks(self, *, limit: int = 100) -> list[dict[str, Any]]:
        """按最近更新时间返回 MasterTask 摘要。"""
        async with self._database.session_factory() as session:
            rows = await session.scalars(
                select(MasterTaskRow)
                .where(
                    ~exists(
                        select(TaskEventRow.event_id).where(
                            TaskEventRow.task_id == MasterTaskRow.task_id,
                            TaskEventRow.event_type == TaskEventType.TASK_ARCHIVED.value,
                        )
                    )
                )
                .order_by(MasterTaskRow.updated_at.desc())
                .limit(limit)
            )
            return [
                {
                    "task_id": row.task_id,
                    "conversation_id": row.conversation_id,
                    "title": row.title,
                    "status": row.status,
                    "task_spec_revision": row.current_task_spec_revision,
                    "decomposition_revision": row.current_decomposition_revision,
                    "created_at": row.created_at,
                    "updated_at": row.updated_at,
                }
                for row in rows
            ]

    async def list_published_assets(
        self, *, artifact_type: str, limit: int = 100
    ) -> list[dict[str, Any]]:
        """列出跨任务可浏览的已发布本体或已发布图谱元数据。

        这里只提供资产库发现和预览入口，不把另一个任务的 Artifact ID 直接塞入
        SharedContext。跨任务复用必须经后续专用导入服务复制为本任务的受控引用。
        """

        async with self._database.session_factory() as session:
            rows = await session.execute(
                select(ArtifactRefRow, ArtifactContentRow)
                .join(
                    ArtifactContentRow,
                    ArtifactContentRow.artifact_id == ArtifactRefRow.artifact_id,
                )
                .where(
                    ArtifactRefRow.artifact_type == artifact_type,
                    ArtifactRefRow.status == "AVAILABLE",
                    or_(
                        ArtifactRefRow.payload["catalog_status"].as_string().is_(None),
                        ArtifactRefRow.payload["catalog_status"].as_string() != "REMOVED",
                    ),
                    # 选择既有资产时会生成任务内受控导入副本，以保存本轮输入和跨任务
                    # 血缘。副本本身不是一次新的“发布”，绝不能再次占用资产目录。
                    or_(
                        ArtifactRefRow.payload["producer_skill"].as_string().is_(None),
                        ArtifactRefRow.payload["producer_skill"].as_string()
                        != "published_asset_import",
                    ),
                )
                .order_by(ArtifactRefRow.created_at.desc())
                .limit(limit)
            )
            return [
                {
                    "artifact_id": ref.artifact_id,
                    "task_id": ref.task_id,
                    "artifact_type": ref.artifact_type,
                    "created_at": ref.created_at,
                    "content_summary": ref.payload.get("content_summary", ""),
                    "checksum": ref.payload.get("checksum", ""),
                    "ontology_id": ref.payload.get("ontology_id"),
                    "ontology_version": ref.payload.get("ontology_version"),
                    "publication_title": ref.payload.get("catalog_title")
                    or ref.payload.get("publication_title")
                    or ref.payload.get("content_summary", ""),
                    "size_bytes": content.size_bytes,
                }
                for ref, content in rows
            ]

    async def task_snapshot(self, task_id: UUID) -> dict[str, Any] | None:
        """返回 SharedContext 及其关联执行事实。"""
        async with self._database.session_factory() as session:
            tasks = SqlAlchemyTaskRepository(session)
            master = await tasks.get_master(task_id)
            if master is None:
                return None
            return {
                "master_task": master,
                "shared_context": await SqlAlchemyContextRepository(session).get(task_id),
                "subtasks": await tasks.list_subtasks(task_id),
                "assignments": await SqlAlchemyAssignmentRepository(session).list_for_task(task_id),
                "agent_runs": await SqlAlchemyAgentRunRepository(session).list_results(task_id),
                "artifacts": await SqlAlchemyArtifactRepository(session).list_for_task(task_id),
                "human_reviews": await SqlAlchemyHumanReviewRepository(session).list_for_task(
                    task_id
                ),
                "events": await SqlAlchemyEventRepository(session).list_for_task(task_id),
            }

    async def artifact(self, artifact_id: UUID) -> Any | None:
        """返回 Artifact 元数据。"""
        async with self._database.session_factory() as session:
            return await SqlAlchemyArtifactRepository(session).get(artifact_id)

    async def artifact_content(self, artifact_id: UUID) -> bytes:
        """读取 Artifact 原始内容。"""
        return await self._artifacts.read_content(artifact_id)

    async def events(self, task_id: UUID, *, after: int = 0) -> list[Any]:
        """返回给定 sequence 之后的公开事件。"""
        async with self._database.session_factory() as session:
            values = await SqlAlchemyEventRepository(session).list_for_task(task_id)
            return [item for item in values if item.sequence > after]
