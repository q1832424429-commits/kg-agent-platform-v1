"""组合 HumanReviewService、数据库事务、Artifact 写入与对象补偿。

该应用入口是第六阶段审核服务 API；HTTP 路由仍属于第七阶段。
"""

from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kg_agent.domain.enums import ReviewOption
from kg_agent.domain.review_models import HumanReviewRequest
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAgentRunRepository,
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyHumanReviewRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.services.artifact_service import ArtifactService
from kg_agent.services.human_review_service import HumanReviewService
from kg_agent.services.review_commands import (
    CreateHumanReviewCommand,
    HumanReviewMaterials,
    ResolveHumanReviewCommand,
)


class PostgresHumanReviewService:
    """为每个审核操作管理短事务，并补偿未被元数据引用的对象。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        storage: ObjectStorage,
        registry: ArtifactRegistry,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_files_per_task: int = 20,
        max_total_bytes_per_task: int = 500 * 1024 * 1024,
    ) -> None:
        """绑定数据库、对象存储、Artifact Registry 和容量限制。"""

        self._session_factory = session_factory
        self._storage = storage
        self._registry = registry
        self._max_file_bytes = max_file_bytes
        self._max_files_per_task = max_files_per_task
        self._max_total_bytes_per_task = max_total_bytes_per_task

    def _build(
        self,
        session: AsyncSession,
    ) -> tuple[HumanReviewService, ArtifactService]:
        """在同一 Session 内组装审核、Artifact、事件、轨迹和上下文服务。"""

        tasks = SqlAlchemyTaskRepository(session)
        assignments = SqlAlchemyAssignmentRepository(session)
        contexts = SqlAlchemyContextRepository(session)
        events = SqlAlchemyEventRepository(session)
        artifacts = SqlAlchemyArtifactRepository(session)
        artifact_service = ArtifactService(
            artifact_repository=artifacts,
            task_repository=tasks,
            assignment_repository=assignments,
            context_repository=contexts,
            event_repository=events,
            storage=self._storage,
            registry=self._registry,
            max_file_bytes=self._max_file_bytes,
            max_files_per_task=self._max_files_per_task,
            max_total_bytes_per_task=self._max_total_bytes_per_task,
        )
        return (
            HumanReviewService(
                reviews=SqlAlchemyHumanReviewRepository(session),
                artifacts=artifacts,
                tasks=tasks,
                assignments=assignments,
                contexts=contexts,
                events=events,
                traces=SqlAlchemyAgentRunRepository(session),
                artifact_service=artifact_service,
            ),
            artifact_service,
        )

    async def request_review(
        self,
        command: CreateHumanReviewCommand,
    ) -> HumanReviewRequest:
        """原子创建审核请求、任务状态、轨迹、事件与 SharedContext 快照。"""

        async with self._session_factory() as session, session.begin():
            service, _ = self._build(session)
            return await service.request_review(command)

    async def resolve_review(
        self,
        command: ResolveHumanReviewCommand,
    ) -> HumanReviewRequest:
        """原子提交用户决策；失败或提交不明时安全补偿对象存储。"""

        async with self._session_factory() as session:
            service, artifact_service = self._build(session)
            try:
                result = await service.resolve_review(command)
                await session.commit()
                return result
            except Exception:
                await session.rollback()
                await self._compensate_decision_artifact(command, artifact_service)
                raise

    async def get_review(self, review_id: UUID) -> HumanReviewRequest | None:
        """读取一个审核请求；不存在时返回 None。"""

        async with self._session_factory() as session:
            service, _ = self._build(session)
            return await service.get_review(review_id)

    async def list_reviews(self, task_id: UUID) -> list[HumanReviewRequest]:
        """返回任务全部审核请求。"""

        async with self._session_factory() as session:
            service, _ = self._build(session)
            return await service.list_reviews(task_id)

    async def get_review_materials(
        self,
        review_id: UUID,
    ) -> HumanReviewMaterials:
        """返回后续审核接口可展示的轻量 Artifact 引用集合。"""

        async with self._session_factory() as session:
            service, _ = self._build(session)
            return await service.get_review_materials(review_id)

    async def _compensate_decision_artifact(
        self,
        command: ResolveHumanReviewCommand,
        artifact_service: ArtifactService,
    ) -> None:
        """仅清理确认没有已提交元数据引用的本次 Approval 或 ChangeSet 对象。"""

        if (
            command.action not in {ReviewOption.APPROVE, ReviewOption.SUBMIT_CHANGES}
            or not artifact_service.pending_object_keys
        ):
            return
        action = "APPROVE" if command.action is ReviewOption.APPROVE else "SUBMIT_CHANGES"
        artifact_id = uuid5(
            NAMESPACE_URL,
            f"kg-agent:review-decision:{command.review_id}:{action}",
        )
        async with self._session_factory() as check_session:
            persisted = await SqlAlchemyArtifactRepository(check_session).get(artifact_id)
        for object_key in artifact_service.pending_object_keys:
            referenced = persisted is not None and object_key == persisted.object_key
            if not referenced:
                await self._storage.delete(object_key)
