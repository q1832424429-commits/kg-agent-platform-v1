"""提供 SkillRegistry 可调用的短事务 PostgreSQL PublishGuard。"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.repositories import SqlAlchemyHumanReviewRepository
from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.services.publish_guard import PublishGuard


class PostgresPublishGuard:
    """为每次发布授权检查创建独立只读数据库 Session。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        agents: AgentRegistry,
        storage: ObjectStorage,
    ) -> None:
        """绑定 Session 工厂、Agent Registry 和 Approval 对象存储。"""

        self._session_factory = session_factory
        self._agents = agents
        self._storage = storage

    async def authorize(self, invocation: SkillInvocation) -> None:
        """读取正式 Artifact 和审核记录并执行确定性发布授权。"""

        async with self._session_factory() as session:
            await PublishGuard(
                artifacts=SqlAlchemyArtifactRepository(session),
                reviews=SqlAlchemyHumanReviewRepository(session),
                agents=self._agents,
                storage=self._storage,
            ).authorize(invocation)
