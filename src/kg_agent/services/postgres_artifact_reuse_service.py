"""为总控 Agent 提供短生命周期 Session 的 Artifact 复用校验入口。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kg_agent.domain.artifact_models import ArtifactRef, ArtifactReuseDecision, ArtifactReuseRequest
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.services.artifact_reuse_service import ArtifactReuseService


class PostgresArtifactReuseService:
    """每次复用校验都读取正式 Artifact 记录，不在 LLM 调用期间持有数据库 Session。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """绑定应用级 Session 工厂。"""

        self._session_factory = session_factory

    async def evaluate(self, request: ArtifactReuseRequest) -> ArtifactReuseDecision:
        """根据最新数据库元数据和规范化父血缘判定复用兼容性。

        受控的已发布资产导入副本允许拥有跨任务父级。当前 MasterTask 的 Context View
        不包含其他任务的 Artifact，因此调用方无法计算这类父级的 checksum。这里必须
        从同一 PostgreSQL 权威快照补齐期望值，再执行统一复用规则；普通 Artifact 仍严格
        使用调用方给出的期望 checksum，不能借此放宽门禁。
        """

        async with self._session_factory() as session:
            repository = SqlAlchemyArtifactRepository(session)
            record = await repository.get(request.artifact_id)
            parents = (
                await repository.get_parent_refs(request.artifact_id) if record is not None else []
            )
            ref = record.ref if record is not None else None
            controlled_import = (
                ref is not None
                and ref.producer_agent == "system"
                and ref.producer_skill
                in {"published_asset_import", "published_graph_baseline_import"}
            )
            if controlled_import and parents and not request.expected_parent_checksums:
                request = request.model_copy(
                    update={"expected_parent_checksums": {item.checksum for item in parents}}
                )
            return ArtifactReuseService.evaluate_snapshot(request, ref, parents)

    async def get_ref(self, artifact_id: UUID) -> ArtifactRef | None:
        """读取一个已持久化 Artifact 的轻量引用，供请求内临时血缘校验使用。

        新任务选择已发布本体或已发布图谱时，会先在请求作用域创建受控导入副本。
        该副本的父级属于历史任务，不能因为它不在本轮临时列表中而被误判为断链。
        此方法只返回元数据，不读取对象内容，也不绕过正式复用校验。
        """

        async with self._session_factory() as session:
            record = await SqlAlchemyArtifactRepository(session).get(artifact_id)
            return record.ref if record is not None else None
