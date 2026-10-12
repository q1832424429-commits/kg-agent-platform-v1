"""用 PostgreSQL Repository 实现总控 Agent 所需的 ContextService 协议。"""

from collections.abc import Sequence
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.domain.task_models import AgentSubTask, TaskSpec
from kg_agent.exceptions import PersistenceError
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.services.task_state_service import TaskStateService


class PostgresContextService:
    """为每次上下文读写创建独立异步 Session 和原子事务。"""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        """绑定应用级异步 Session 工厂。"""

        self._session_factory = session_factory

    async def get(self, task_id: UUID) -> SharedTaskContext | None:
        """读取最新 SharedContext 快照；任务不存在时返回 None。"""

        async with self._session_factory() as session:
            return await SqlAlchemyContextRepository(session).get(task_id)

    async def get_task_spec(self, task_id: UUID, revision: int) -> TaskSpec | None:
        """读取 Artifact 生产时的不可变 TaskSpec，用于确定性影响比较。"""

        async with self._session_factory() as session:
            return await SqlAlchemyTaskRepository(session).get_task_spec(task_id, revision)

    async def list_subtasks(self, task_id: UUID) -> list[AgentSubTask]:
        """读取任务全部历史子任务及其当前状态。"""

        async with self._session_factory() as session:
            return await SqlAlchemyTaskRepository(session).list_subtasks(task_id)

    async def publish_decomposition(
        self,
        *,
        conversation_id: UUID,
        result: TaskDecompositionResult,
        source_artifacts: Sequence[ArtifactRef],
        expected_version: int | None,
        recalled_long_term_memories: Sequence[MemoryRef] = (),
    ) -> SharedTaskContext:
        """在一个事务中发布拆解、Assignment、事件和上下文快照。

        Raises:
            PersistenceError: 数据库唯一约束或外键约束拒绝写入。
        """

        async with self._session_factory() as session:
            try:
                async with session.begin():
                    service = TaskStateService(
                        task_repository=SqlAlchemyTaskRepository(session),
                        assignment_repository=SqlAlchemyAssignmentRepository(session),
                        context_repository=SqlAlchemyContextRepository(session),
                        event_repository=SqlAlchemyEventRepository(session),
                    )
                    return await service.publish_decomposition(
                        conversation_id=conversation_id,
                        result=result,
                        source_artifacts=source_artifacts,
                        recalled_long_term_memories=recalled_long_term_memories,
                        expected_version=expected_version,
                    )
            except IntegrityError as exc:
                raise PersistenceError("任务拆解持久化违反数据库约束") from exc
