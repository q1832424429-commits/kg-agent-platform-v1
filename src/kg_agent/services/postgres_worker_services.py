"""提供 Assignment Worker 使用的短事务 PostgreSQL 应用入口。"""

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_view_models import ProfessionalAgentContextView
from kg_agent.domain.task_models import AgentSubTask
from kg_agent.exceptions import ContextConflictError
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAgentRunRepository,
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.memory.memory_service import LongTermMemoryService
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.agent_run_service import AgentRunService
from kg_agent.services.assignment_service import AssignmentService
from kg_agent.services.context_view_service import ContextViewService
from kg_agent.services.subtask_input_resolver import (
    resolve_candidate_input_ids,
    select_effective_input_refs,
)


class ProfessionalWorkContext:
    """保存 Worker 加载的完整子任务、角色视图、输入引用和 TaskSpec revision。"""

    def __init__(
        self,
        subtask: AgentSubTask,
        view: ProfessionalAgentContextView,
        inputs: list[ArtifactRef],
        task_spec_revision: int,
    ) -> None:
        self.subtask, self.view, self.inputs = subtask, view, inputs
        self.task_spec_revision = task_spec_revision


class PostgresWorkerServices:
    """让 Worker 的认领、启动、心跳、读取和结果提交各自使用短事务。"""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        agents: AgentRegistry,
        skills: SkillRegistry,
        lease_seconds: int = 60,
        memory_service: LongTermMemoryService | None = None,
    ) -> None:
        """绑定 Session 工厂、Registry 事实来源和 Assignment 租约长度。"""

        self._factory, self._agents, self._skills = session_factory, agents, skills
        self._lease_seconds = lease_seconds
        self._memory = memory_service

    def _assignment(self, session: AsyncSession) -> AssignmentService:
        return AssignmentService(
            task_repository=SqlAlchemyTaskRepository(session),
            assignment_repository=SqlAlchemyAssignmentRepository(session),
            context_repository=SqlAlchemyContextRepository(session),
            event_repository=SqlAlchemyEventRepository(session),
            lease_seconds=self._lease_seconds,
        )

    async def claim(
        self,
        agent_type: str,
        worker_id: str,
        *,
        task_id: UUID | None = None,
    ) -> AgentAssignment | None:
        """原子认领一个与角色及可选任务范围匹配的待处理 Assignment。"""
        async with self._factory() as session, session.begin():
            return await self._assignment(session).claim_next(
                agent_type=agent_type,
                worker_id=worker_id,
                task_id=task_id,
            )

    async def recover_expired(self) -> list[AgentAssignment]:
        """回收因请求中断或进程退出而遗留的过期 Assignment 租约。

        恢复动作仍由 ``AssignmentService`` 在短事务内完成，并遵守原子状态转换、
        重试额度和 fencing token 规则；本方法不修改仍在有效期内的执行尝试。
        """

        async with self._factory() as session, session.begin():
            return await self._assignment(session).recover_expired()

    async def start(self, assignment: AgentAssignment, worker_id: str) -> AgentAssignment:
        """以当前 fencing token 启动 Assignment。"""
        async with self._factory() as session, session.begin():
            return await self._assignment(session).start(
                assignment_id=assignment.assignment_id,
                worker_id=worker_id,
                fencing_token=assignment.fencing_token,
            )

    async def heartbeat(self, assignment: AgentAssignment, worker_id: str) -> None:
        """在 Agent 运行期间续租，不持有跨模型调用的数据库事务。"""
        async with self._factory() as session, session.begin():
            await self._assignment(session).heartbeat(
                assignment_id=assignment.assignment_id,
                worker_id=worker_id,
                fencing_token=assignment.fencing_token,
            )

    async def load(self, assignment: AgentAssignment) -> ProfessionalWorkContext:
        """构造角色化 Context View，同时读取 Skill 执行所需正式 ArtifactRef。"""
        async with self._factory() as session:
            tasks = SqlAlchemyTaskRepository(session)
            subtask = await tasks.get_subtask(assignment.subtask_id)
            if subtask is None:
                raise ContextConflictError("Assignment 对应子任务不存在")
            context = await SqlAlchemyContextRepository(session).get(subtask.master_task_id)
            if context is None:
                raise ContextConflictError("SharedContext 不存在")
            refs = {item.artifact_id: item for item in context.artifact_refs}
            candidate_ids = await resolve_candidate_input_ids(
                subtask=subtask,
                tasks=tasks,
                runs=SqlAlchemyAgentRunRepository(session),
            )
            accepted_types = set(
                self._agents.require(subtask.assigned_agent_type).accepted_input_types
            )
            explicit_ids = set(subtask.input_artifact_ids)
            # 先用全部祖先建立血缘，再按角色可接受类型裁剪；否则跨类型
            # 中间产物会被提前丢弃，无法识别更远的同类新版本。
            candidates = [refs[item] for item in candidate_ids if item in refs]
            inputs = select_effective_input_refs(
                candidates, explicit_ids=explicit_ids, accepted_types=accepted_types
            )
            if not explicit_ids <= {item.artifact_id for item in inputs}:
                raise ContextConflictError("子任务输入 Artifact 不完整")
            resolved_ids = [item.artifact_id for item in inputs]
            view = await ContextViewService(
                task_repository=tasks,
                assignment_repository=SqlAlchemyAssignmentRepository(session),
                context_repository=SqlAlchemyContextRepository(session),
            ).build_professional_view(
                assignment_id=assignment.assignment_id,
                requested_agent_type=assignment.assigned_agent_type,
                resolved_input_artifact_ids=resolved_ids,
            )
            # 数据库中的计划保持不可变；只为本次执行构造已绑定真实 Artifact 的运行副本。
            runtime_subtask = subtask.model_copy(update={"input_artifact_ids": resolved_ids})
            return ProfessionalWorkContext(
                runtime_subtask, view, inputs, context.current_task_spec.revision
            )

    async def submit(self, result: AgentRunResult, worker_id: str, token: int) -> AgentRunResult:
        """原子持久化结果、轨迹、Assignment 终态和 SharedContext 快照。

        本方法只处理数据库终态，不访问 Mem0。调用方必须先停止 Assignment 心跳，
        再调用 ``remember_result``，避免已完成任务被过期心跳再次续租。
        """

        async with self._factory() as session, session.begin():
            service = AgentRunService(
                runs=SqlAlchemyAgentRunRepository(session),
                tasks=SqlAlchemyTaskRepository(session),
                assignments=self._assignment(session),
                agents=self._agents,
                skills=self._skills,
            )
            persisted = await service.submit(
                result=result,
                worker_id=worker_id,
                fencing_token=token,
            )
        return persisted

    async def remember_result(self, result: AgentRunResult) -> None:
        """兼容旧 Worker 调用，但不再写入单个专业 Agent 的零散长期经验。

        完整经验由 TaskCoordinator 在 MasterTask 成功收口后统一构建和写入。参数保留
        是为了让已有扩展 Worker 平滑升级，不改变 Assignment 提交接口。
        """

        del result
