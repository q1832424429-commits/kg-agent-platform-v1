"""确定性校验并原子提交专业 Agent 的运行结果。"""

from uuid import UUID, uuid4

from kg_agent.domain.agent_models import AgentRunResult
from kg_agent.domain.enums import AgentRunStatus, ArtifactStatus
from kg_agent.domain.trace_models import ExecutionTrace, TraceEvent
from kg_agent.exceptions import AssignmentConflictError, PersistenceError
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.repositories.interfaces import AgentRunRepository, TaskRepository
from kg_agent.services.assignment_service import AssignmentService
from kg_agent.services.subtask_input_resolver import (
    resolve_candidate_input_ids,
    select_effective_input_refs,
)


class AgentRunService:
    """验证身份、Skill、输入输出 Artifact 后保存结果并完成 Assignment。"""

    def __init__(
        self,
        *,
        runs: AgentRunRepository,
        tasks: TaskRepository,
        assignments: AssignmentService,
        agents: AgentRegistry,
        skills: SkillRegistry,
    ) -> None:
        """绑定同一事务内的 Repository、Assignment 服务和 Registry。"""

        self._runs, self._tasks, self._assignments = runs, tasks, assignments
        self._agents, self._skills = agents, skills

    async def submit(
        self, *, result: AgentRunResult, worker_id: str, fencing_token: int
    ) -> AgentRunResult:
        """幂等保存合法结果；过期 Worker、伪造输出或越权 Skill 会被拒绝。"""

        existing = await self._runs.get_result(result.run_id)
        if existing is not None:
            if existing != result:
                raise PersistenceError("同一 run_id 不能保存不同 AgentRunResult")
            return existing
        subtask = await self._tasks.get_subtask(result.subtask_id)
        if subtask is None or subtask.master_task_id != result.task_id:
            raise AssignmentConflictError("AgentRunResult 子任务归属不正确")
        if result.agent_name != subtask.assigned_agent_type:
            raise AssignmentConflictError("AgentRunResult 角色与子任务分配不一致")
        await self._assignments.validate_submission(
            assignment_id=result.assignment_id,
            subtask_id=result.subtask_id,
            agent_type=result.agent_name,
            worker_id=worker_id,
            fencing_token=fencing_token,
        )
        registration = self._agents.require(result.agent_name)
        candidate_ids = await resolve_candidate_input_ids(
            subtask=subtask,
            tasks=self._tasks,
            runs=self._runs,
        )
        candidates = [
            artifact
            for artifact_id in candidate_ids
            if (artifact := await self._tasks.get_artifact_ref(artifact_id)) is not None
        ]
        resolved_ids = {
            artifact.artifact_id
            for artifact in select_effective_input_refs(
                candidates,
                explicit_ids=set(subtask.input_artifact_ids),
                accepted_types=set(registration.accepted_input_types),
            )
        }
        if set(result.input_artifact_ids) != resolved_ids:
            raise AssignmentConflictError("AgentRunResult 输入与子任务分配不一致")
        if not set(result.skills_used) <= set(registration.allowed_skills):
            raise AssignmentConflictError("AgentRunResult 包含 allowlist 外 Skill")
        for name in result.skills_used:
            if result.agent_name not in self._skills.require(name).allowed_agents:
                raise AssignmentConflictError("Skill Registry 未授权结果中的 Agent")
        output_types: set[str] = set()
        output_types_by_id: dict[UUID, str] = {}
        for artifact_id in result.output_artifact_ids:
            artifact = await self._tasks.get_artifact_ref(artifact_id)
            if artifact is None or artifact.task_id != result.task_id:
                raise AssignmentConflictError("AgentRunResult 输出 Artifact 不存在或跨任务")
            if (
                artifact.producer_run_id != result.run_id
                or artifact.producer_agent != result.agent_name
            ):
                raise AssignmentConflictError("输出 Artifact 生产者与 AgentRunResult 不一致")
            if artifact.status is not ArtifactStatus.AVAILABLE:
                raise AssignmentConflictError("AgentRunResult 输出 Artifact 当前不可用")
            output_types.add(artifact.artifact_type)
            output_types_by_id[artifact_id] = artifact.artifact_type
        if result.skill_artifact_bindings:
            bound_ids = [
                artifact_id
                for binding in result.skill_artifact_bindings
                for artifact_id in binding.output_artifact_ids
            ]
            if len(bound_ids) != len(set(bound_ids)) or set(bound_ids) != set(
                result.output_artifact_ids
            ):
                raise AssignmentConflictError("Skill 与输出 Artifact 编号映射不完整")
            if not {binding.skill_name for binding in result.skill_artifact_bindings} <= set(
                result.skills_used
            ):
                raise AssignmentConflictError("Artifact 映射包含未执行的 Skill")
            for binding in result.skill_artifact_bindings:
                actual_types = [output_types_by_id[item] for item in binding.output_artifact_ids]
                if actual_types != binding.output_artifact_types:
                    raise AssignmentConflictError("Artifact 映射中的类型与持久化记录不一致")
        if (
            result.status is AgentRunStatus.SUCCESS
            and not set(subtask.expected_output_types) <= output_types
        ):
            raise AssignmentConflictError("SUCCESS 输出未覆盖子任务预期类型")
        if result.status is AgentRunStatus.RETRYABLE_FAILED and not result.retryable:
            raise AssignmentConflictError("RETRYABLE_FAILED 结果必须明确 retryable=True")

        trace = self._trace(result)
        await self._runs.add_result(result)
        await self._runs.add_trace(trace)
        if result.status is AgentRunStatus.SUCCESS:
            await self._assignments.complete(
                assignment_id=result.assignment_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                succeeded=True,
                agent_run_result_id=result.run_id,
                execution_trace_id=trace.trace_id,
                result_status=result.status.value,
            )
        elif result.status is AgentRunStatus.RETRYABLE_FAILED:
            if self._is_llm_timeout(result):
                # 对同一个超大模型请求反复重试不会获得新的输入或新条件，只会让用户
                # 连续等待数分钟。保留失败现场，由 TaskCoordinator 暂停任务并让用户
                # 决定重试或切换模型，而不是自动制造同类循环。
                await self._assignments.complete(
                    assignment_id=result.assignment_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    succeeded=False,
                    agent_run_result_id=result.run_id,
                    execution_trace_id=trace.trace_id,
                    result_status=result.status.value,
                )
            else:
                await self._assignments.retry(
                    assignment_id=result.assignment_id,
                    worker_id=worker_id,
                    fencing_token=fencing_token,
                    agent_run_result_id=result.run_id,
                    execution_trace_id=trace.trace_id,
                    result_status=result.status.value,
                )
        elif result.status is AgentRunStatus.NEEDS_USER_INPUT:
            await self._assignments.wait_for_user(
                assignment_id=result.assignment_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                agent_run_result_id=result.run_id,
                execution_trace_id=trace.trace_id,
                result_status=result.status.value,
            )
        elif result.status is AgentRunStatus.CANCELLED:
            await self._assignments.cancel(
                assignment_id=result.assignment_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                agent_run_result_id=result.run_id,
                execution_trace_id=trace.trace_id,
                result_status=result.status.value,
            )
        else:
            # PARTIAL_SUCCESS、REWORK_REQUIRED 与最终 FAILED 都不解锁下游任务；
            # 总控 Agent 后续根据 AgentRunResult 决定是否发布新的正式子任务。
            await self._assignments.complete(
                assignment_id=result.assignment_id,
                worker_id=worker_id,
                fencing_token=fencing_token,
                succeeded=False,
                agent_run_result_id=result.run_id,
                execution_trace_id=trace.trace_id,
                result_status=result.status.value,
            )
        # 所有专业 Agent 的共享记忆统一沉淀到同一任务 SharedContext，
        # 不建立 Agent 私有且不可见的第二套事实来源。
        await self._assignments.append_memories(
            task_id=result.task_id, entries=result.memory_entries
        )
        return result

    @staticmethod
    def _is_llm_timeout(result: AgentRunResult) -> bool:
        """识别由 LLM Skill 明确报告的超时，避免进入多层自动重试。"""

        return any(issue.startswith("TimeoutError:") for issue in result.blocking_issues)

    @staticmethod
    def _trace(result: AgentRunResult) -> ExecutionTrace:
        """从公开结果构造不包含模型隐藏思维的最小可审计轨迹。"""

        event = TraceEvent(
            event_id=uuid4(),
            event_type="AGENT_RUN_COMPLETED",
            occurred_at=result.finished_at,
            actor_type="AGENT",
            actor_name=result.agent_name,
            summary=f"专业 Agent 运行结束：{result.status.value}",
            input_refs=[str(item) for item in result.input_artifact_ids],
            output_refs=[str(item) for item in result.output_artifact_ids],
            metrics=result.metrics,
        )
        return ExecutionTrace(
            trace_id=result.execution_trace_id,
            task_id=result.task_id,
            subtask_id=result.subtask_id,
            assignment_id=result.assignment_id,
            agent_run_id=result.run_id,
            events=[event],
            started_at=result.started_at,
            finished_at=result.finished_at,
            status=result.status.value,
        )
