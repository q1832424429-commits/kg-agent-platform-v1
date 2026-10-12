"""组合 ArtifactService、数据库事务和对象存储失败补偿。

本模块是持久化应用入口，不负责专业 Agent 决策。调用方应优先使用本入口，以覆盖领域服务
返回后、数据库 commit 阶段失败的情况；裸 ArtifactService 则要求调用方自行管理事务。
"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kg_agent.domain.artifact_content_models import PublishedGraphContent
from kg_agent.domain.artifact_models import ArtifactCreateCommand, ArtifactRecord
from kg_agent.domain.enums import ArtifactStatus
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.published_graph_repository import (
    SqlAlchemyPublishedGraphRepository,
)
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.services.artifact_service import ArtifactService


class PostgresArtifactService:
    """为每个 Artifact 命令管理独立 Session、事务及可验证的对象补偿。"""

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
        """绑定数据库、受控存储、内容 Registry 和可覆盖的容量配置。"""

        self._session_factory = session_factory
        self._storage = storage
        self._registry = registry
        self._max_file_bytes = max_file_bytes
        self._max_files_per_task = max_files_per_task
        self._max_total_bytes_per_task = max_total_bytes_per_task

    def _service(self, session: AsyncSession) -> ArtifactService:
        """在同一 Session 内组合全部 Repository，避免状态和快照分开提交。"""

        return ArtifactService(
            artifact_repository=SqlAlchemyArtifactRepository(session),
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
            storage=self._storage,
            registry=self._registry,
            max_file_bytes=self._max_file_bytes,
            max_files_per_task=self._max_files_per_task,
            max_total_bytes_per_task=self._max_total_bytes_per_task,
        )

    async def create(self, command: ArtifactCreateCommand) -> ArtifactRecord:
        """创建并提交 Artifact；提交失败后仅清理确认没有元数据引用的对象。

        参数为已经过公共契约校验的创建命令，返回持久化记录；校验、存储和数据库异常
        继续向上层传播。若提交结果不明且数据库也不可读取，保留对象，不冒险误删。
        """

        async with self._session_factory() as session:
            service = self._service(session)
            try:
                record = await service.create(command)
                if command.artifact_type == "PublishedGraph":
                    await self._persist_published_graph(session, command=command, record=record)
                await session.commit()
                return record
            except Exception:
                await session.rollback()
                if service.pending_object_keys:
                    # 使用新的事务重新确认：即使 commit 响应丢失，也不能删除已提交产物。
                    async with self._session_factory() as check_session, check_session.begin():
                        await SqlAlchemyTaskRepository(check_session).get_master(
                            command.task_id, for_update=True
                        )
                        persisted = await SqlAlchemyArtifactRepository(check_session).get(
                            command.artifact_id
                        )
                        for object_key in service.pending_object_keys:
                            referenced = (
                                persisted is not None and object_key == persisted.object_key
                            )
                            if not referenced:
                                await self._storage.delete(object_key)
                raise

    @staticmethod
    async def _persist_published_graph(
        session: AsyncSession,
        *,
        command: ArtifactCreateCommand,
        record: ArtifactRecord,
    ) -> None:
        """在 Artifact 同一事务内写入 PostgreSQL 节点—边图投影。

        发布命令已经由 ArtifactService 验证父级 CandidateGraph 与 GraphApproval；这里再次
        从数据库血缘解析唯一候选父级，避免依赖调用方传入顺序。任何投影失败都会回滚
        Artifact 元数据，并由外层补偿本次新写入的 MinIO 对象。
        """

        if not isinstance(command.content, dict):
            raise TypeError("PublishedGraph 内容必须是 JSON 对象")
        graph = PublishedGraphContent.model_validate(command.content)
        artifacts = SqlAlchemyArtifactRepository(session)
        parents = await artifacts.get_parent_refs(record.ref.artifact_id)
        candidates = [item for item in parents if item.artifact_type == "CandidateGraph"]
        if len(candidates) != 1:
            raise ValueError("PublishedGraph PostgreSQL 投影需要唯一 CandidateGraph 父级")
        await SqlAlchemyPublishedGraphRepository(session).ensure_persisted(
            artifact=record,
            graph=graph,
            source_candidate_artifact_id=candidates[0].artifact_id,
        )

    async def read_content(self, artifact_id: UUID) -> bytes:
        """读取并校验完整内容；目标缺失或 checksum 异常时向调用方报告。"""

        async with self._session_factory() as session:
            return await self._service(session).read_content(artifact_id)

    async def change_status(
        self,
        *,
        artifact_id: UUID,
        target: ArtifactStatus,
        actor_id: str,
        reason_code: str,
    ) -> ArtifactRecord:
        """原子提交 Artifact 状态、审计事件与新 SharedContext 快照。"""

        async with self._session_factory() as session, session.begin():
            return await self._service(session).change_status(
                artifact_id=artifact_id,
                target=target,
                actor_id=actor_id,
                reason_code=reason_code,
            )
