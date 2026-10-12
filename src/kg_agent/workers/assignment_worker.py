"""实现只负责认领、心跳、调用专业 Agent 和提交结果的后台 Worker。"""

import asyncio
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from kg_agent.agents.professional.models import ProfessionalAgentRequest
from kg_agent.agents.professional.registry import ProfessionalAgentPool
from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.enums import AgentRunStatus
from kg_agent.services.postgres_worker_services import PostgresWorkerServices


class AssignmentWorker:
    """执行确定性运行循环；Worker 不选择 Skill，也不承担新的 Agent 决策职责。"""

    def __init__(
        self,
        *,
        worker_id: str,
        services: PostgresWorkerServices,
        agents: ProfessionalAgentPool,
        heartbeat_seconds: int = 20,
    ) -> None:
        """绑定 Worker 标识、短事务服务、Agent 池和心跳间隔。"""

        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds 必须大于 0")
        self._worker_id, self._services, self._agents = worker_id, services, agents
        self._heartbeat_seconds = heartbeat_seconds

    async def run_once(
        self,
        agent_type: str,
        *,
        task_id: UUID | None = None,
    ) -> AgentRunResult | None:
        """认领并运行指定角色及可选任务范围内的 Assignment。"""

        claimed = await self._services.claim(
            agent_type,
            self._worker_id,
            task_id=task_id,
        )
        if claimed is None:
            return None
        running = await self._services.start(claimed, self._worker_id)
        work = await self._services.load(running)
        stop = asyncio.Event()
        heartbeat = asyncio.create_task(self._heartbeat_loop(running, stop))
        started = datetime.now(UTC)
        try:
            try:
                output = await self._agents.require(agent_type).run(
                    ProfessionalAgentRequest(
                        assignment=running,
                        subtask=work.subtask,
                        task_spec_revision=work.task_spec_revision,
                        context_view=work.view,
                        input_artifacts=work.inputs,
                        worker_id=self._worker_id,
                    )
                )
                result = output.result
            except Exception as exc:
                run_id = uuid5(NAMESPACE_URL, f"kg-agent:run:{running.assignment_id}")
                result = AgentRunResult(
                    task_id=work.view.task_id,
                    subtask_id=work.subtask.subtask_id,
                    assignment_id=running.assignment_id,
                    agent_name=agent_type,
                    run_id=run_id,
                    status=AgentRunStatus.FAILED,
                    input_artifact_ids=[item.artifact_id for item in work.inputs],
                    # 这里只记录公开校验/执行异常，不记录模型隐藏思维。保留错误正文可让
                    # 测试端和正式日志定位究竟是输入绑定、Skill 权限还是契约失败。
                    blocking_issues=[f"{type(exc).__name__}: {exc}"],
                    execution_trace_id=uuid5(
                        NAMESPACE_URL, f"kg-agent:trace:{running.assignment_id}"
                    ),
                    started_at=started,
                    finished_at=datetime.now(UTC),
                )
            persisted = await self._services.submit(
                result,
                self._worker_id,
                running.fencing_token,
            )
        finally:
            # Assignment 一旦进入终态就不再允许续租，必须先终止心跳再访问 Mem0 等外部服务。
            stop.set()
            await heartbeat
        # 单个专业 Agent 的结果只进入 PostgreSQL 任务事实。长期记忆必须等待整个
        # MasterTask 成功后由 TaskCoordinator 聚合，避免一次任务被拆成多条碎片经验。
        return persisted

    async def _heartbeat_loop(self, assignment: AgentAssignment, stop: asyncio.Event) -> None:
        """等待固定间隔续租；停止事件会立即结束，避免阻塞快速测试。"""

        while True:
            try:
                await asyncio.wait_for(stop.wait(), timeout=self._heartbeat_seconds)
                return
            except TimeoutError:
                await self._services.heartbeat(assignment, self._worker_id)
