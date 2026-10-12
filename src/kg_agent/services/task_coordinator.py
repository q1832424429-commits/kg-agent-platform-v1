"""协调完整 AgentSubTask 计划的依赖执行、人工审核与最终目标验收。

总控 Agent 在开始时一次发布完整计划；本服务不按波次重新规划，只观察 Assignment、
依赖和审核状态。全部计划进入终态后才再次调用总控 Agent检查最终目标是否满足。
"""

import json
import logging
from typing import TYPE_CHECKING
from uuid import UUID, uuid4

from kg_agent.domain.artifact_models import ArtifactRecord
from kg_agent.domain.enums import (
    AgentRunStatus,
    AssignmentStatus,
    HumanReviewStatus,
    MasterTaskStatus,
    ReviewTargetType,
    SubTaskStatus,
)
from kg_agent.exceptions import KGAgentError, ReviewConflictError, StateTransitionError
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAgentRunRepository,
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.services.review_commands import CreateHumanReviewCommand
from kg_agent.services.task_state_service import TaskStateService

if TYPE_CHECKING:
    from kg_agent.runtime import RuntimeContainer


LOGGER = logging.getLogger(__name__)


class TaskCoordinator:
    """执行依赖等待、审核门禁创建和总控最终目标检查。

    它不是新增 Agent：完整任务拆解仍由总控 Agent 完成；正常依赖解锁期间不调用
    总控。仅当全部已发布子任务进入终态时，才让总控确认完成或发布修复计划。
    """

    def __init__(
        self, runtime: "RuntimeContainer", *, max_decomposition_revisions: int = 30
    ) -> None:
        """绑定运行容器和单任务自动拆解修订安全上限。

        Args:
            runtime: 同时提供数据库、总控编排、Artifact 与审核服务的运行容器。
            max_decomposition_revisions: 单个任务允许的拆解修订上限，用于阻断异常循环。

        Raises:
            ValueError: 安全上限小于 2 时抛出。
        """

        if max_decomposition_revisions < 2:
            raise ValueError("max_decomposition_revisions 必须大于等于 2")
        self._runtime = runtime
        self._max_decomposition_revisions = max_decomposition_revisions

    async def advance(self, task_id: UUID) -> str:
        """检查计划状态，并在全部子任务终止后执行最终目标验收。"""
        async with self._runtime.database.session_factory() as session:
            tasks = SqlAlchemyTaskRepository(session)
            master = await tasks.get_master(task_id)
            context = await SqlAlchemyContextRepository(session).get(task_id)
            assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
            records = await SqlAlchemyArtifactRepository(session).list_for_task(task_id)
            subtasks = await tasks.list_subtasks(task_id)
            runs = await SqlAlchemyAgentRunRepository(session).list_results(task_id)
        if (
            master is None
            or context is None
            or master.status
            in {MasterTaskStatus.SUCCEEDED, MasterTaskStatus.FAILED, MasterTaskStatus.CANCELLED}
        ):
            return "NOOP"
        if master.status is MasterTaskStatus.WAITING_USER:
            return "WAITING_USER"
        if any(
            item.status
            in {AssignmentStatus.PENDING, AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}
            for item in assignments
        ):
            return "WAITING_ASSIGNMENTS"

        available_types = {
            item.ref.artifact_type for item in records if item.ref.status.value == "AVAILABLE"
        }
        targets = set(context.current_task_spec.target_output_types)
        latest_subtasks = [
            item
            for item in subtasks
            if item.decomposition_revision == master.current_decomposition_revision
        ]
        # 空拆解只会由最终验收产生，表示总控已基于全部执行结果确认没有剩余工作。
        if not latest_subtasks and (not targets or targets <= available_types):
            completed = await self._complete(task_id)
            if completed:
                await self._remember_completed_experience(task_id)
            return "TASK_COMPLETED"

        pending_reviews = [
            item
            for item in await self._runtime.reviews.list_reviews(task_id)
            if item.status is HumanReviewStatus.PENDING
        ]
        if pending_reviews:
            return "WAITING_HUMAN"
        review = await self._create_required_review(task_id, records)
        if review:
            return "REVIEW_CREATED"

        unfinished = [
            item
            for item in subtasks
            if item.status
            not in {
                SubTaskStatus.SUCCEEDED,
                SubTaskStatus.FAILED,
                SubTaskStatus.CANCELLED,
                SubTaskStatus.SUPERSEDED,
            }
        ]
        if unfinished:
            # BLOCKED 子任务由依赖完成或人工审核批准自动解锁。这里绝不重新调用总控，
            # 从而避免每个步骤都产生一次昂贵且可能漂移的模型规划。
            return "WAITING_DEPENDENCIES"

        # PublishGuard 的拒绝来自确定性审核安全规则，而不是 LLM 可通过重规划自行
        # 修复的业务缺口。继续把同一输入交给总控只会反复生成相同发布任务，既浪费
        # 模型调用又可能绕过用户对失败原因的判断，因此必须保留现场并等待人工处理。
        if any(
            any(issue.startswith("PublishGuardError:") for issue in run.blocking_issues)
            for run in runs
        ):
            await self._pause_for_guard(task_id, "PUBLISH_GUARD_REJECTED")
            LOGGER.error("任务 %s 的发布门禁拒绝，已暂停自动重规划", task_id)
            return "WAITING_USER"

        # 模型超时不会因总控重新拆解而获得更小的输入或更快的服务端响应。失败的
        # Assignment 已由 AgentRunService 终止；此处只负责把 MasterTask 暂停，避免
        # 自动创建同类“修复”子任务并形成 API 调用循环。
        if any(
            any(issue.startswith("TimeoutError:") for issue in run.blocking_issues) for run in runs
        ):
            await self._pause_for_guard(task_id, "LLM_SKILL_TIMEOUT")
            LOGGER.warning("任务 %s 的 LLM Skill 超时，已暂停自动重规划", task_id)
            return "WAITING_USER"

        # 当前拆解版本中的专业 Agent 已明确返回不可重试失败时，原计划不可能继续完成。
        # 此时再次调用总控做最终验收既不能恢复缺失 Artifact，还会让后续校验错误覆盖最初
        # 的 Skill 失败原因。历史 revision 的失败不阻塞后续修复计划，只检查当前版本子任务。
        latest_subtask_ids = {item.subtask_id for item in latest_subtasks}
        non_retryable_failures = [
            run
            for run in runs
            if run.subtask_id in latest_subtask_ids
            and run.status is AgentRunStatus.FAILED
            and not run.retryable
        ]
        if non_retryable_failures:
            await self._pause_for_guard(task_id, "NON_RETRYABLE_AGENT_FAILURE")
            LOGGER.error(
                "任务 %s 存在 %s 个不可重试的专业 Agent 失败，已保留原始错误并暂停",
                task_id,
                len(non_retryable_failures),
            )
            return "WAITING_USER"

        if master.current_decomposition_revision >= self._max_decomposition_revisions:
            await self._pause_for_guard(task_id, "DECOMPOSITION_REVISION_LIMIT")
            LOGGER.error(
                "任务 %s 达到拆解修订安全上限 %s，已暂停自动推进",
                task_id,
                self._max_decomposition_revisions,
            )
            return "REVISION_LIMIT_REACHED"

        # 完整计划已全部进入终态。总控只在这里检查一次最终目标；若失败或缺失，可基于
        # 全部公开结果发布新的修复子任务，否则返回空 subtasks 确认完成。
        try:
            snapshot = await self._runtime.orchestration.submit_and_run(
                question=(
                    "完整 AgentSubTask 计划已经全部执行到终态。请根据原始 TaskSpec、"
                    "全部 AgentRunResult 与 Artifact 检查最终目标是否完成。若目标已完成，"
                    "保持 TaskSpec 不变并返回空 subtasks；若失败或缺失，只发布必要的修复子任务。"
                ),
                files=[],
                task_id=task_id,
                conversation_id=master.conversation_id,
                request_id=uuid4(),
                execute_workers=False,
                internal_continuation=True,
                controller_operation="FINAL_VERIFICATION",
            )
        except KGAgentError:
            # 最终验收属于独立模型检查。其结构化输出偶发失败时，前面已经成功生成的
            # Artifact 仍然有效；保留现场并暂停，避免整个请求回滚成一个无法解释的 409。
            await self._pause_for_guard(task_id, "FINAL_CONTROLLER_CHECK_FAILED")
            LOGGER.warning("任务 %s 的总控最终目标检查失败，已保留现场并暂停", task_id)
            return "FINAL_CHECK_FAILED"
        if snapshot.controller_result.decomposition.requires_clarification:
            await self._pause_for_guard(task_id, "CONTROLLER_NEEDS_CLARIFICATION")
            return "WAITING_USER"

        # 总控用空拆解确认完成后直接收口。
        if not snapshot.controller_result.decomposition.subtasks:
            async with self._runtime.database.session_factory() as session:
                latest_records = await SqlAlchemyArtifactRepository(session).list_for_task(task_id)
            latest_types = {
                item.ref.artifact_type
                for item in latest_records
                if item.ref.status.value == "AVAILABLE"
            }
            if not targets or targets <= latest_types:
                completed = await self._complete(task_id)
                if completed:
                    await self._remember_completed_experience(task_id)
                return "TASK_COMPLETED"
            await self._pause_for_guard(task_id, "CONTROLLER_NO_EXECUTABLE_SUBTASK")
            return "WAITING_USER"
        return "CONTROLLER_REPLANNED"

    async def _pause_for_guard(self, task_id: UUID, reason_code: str) -> None:
        """安全保护触发时把任务置为等待用户，保留全部事实供人工排查。"""

        async with self._runtime.database.session_factory() as session, session.begin():
            service = TaskStateService(
                task_repository=SqlAlchemyTaskRepository(session),
                assignment_repository=SqlAlchemyAssignmentRepository(session),
                context_repository=SqlAlchemyContextRepository(session),
                event_repository=SqlAlchemyEventRepository(session),
            )
            try:
                await service.transition_master_task(
                    task_id=task_id,
                    target=MasterTaskStatus.WAITING_USER,
                    actor_id="task_coordinator",
                    reason_code=reason_code,
                )
            except StateTransitionError:
                return

    async def _complete(self, task_id: UUID) -> bool:
        """在目标 Artifact 已齐备时确定性标记 MasterTask 成功并返回是否完成转换。"""
        async with self._runtime.database.session_factory() as session, session.begin():
            service = TaskStateService(
                task_repository=SqlAlchemyTaskRepository(session),
                assignment_repository=SqlAlchemyAssignmentRepository(session),
                context_repository=SqlAlchemyContextRepository(session),
                event_repository=SqlAlchemyEventRepository(session),
            )
            try:
                await service.transition_master_task(
                    task_id=task_id,
                    target=MasterTaskStatus.SUCCEEDED,
                    actor_id="task_coordinator",
                    reason_code="TARGET_ARTIFACTS_AVAILABLE",
                )
            except StateTransitionError:
                return False
        return True

    async def _remember_completed_experience(self, task_id: UUID) -> None:
        """非阻断触发整轮经验写入；兼容不提供记忆能力的最小测试 Runtime。"""

        callback = getattr(
            self._runtime.orchestration,
            "remember_completed_task_experience",
            None,
        )
        if callback is None:
            return
        try:
            await callback(task_id)
        except Exception:
            # 任务状态已经成功提交。长期记忆是增强能力，数据库或云端异常不能把
            # 协调器响应改成失败；完整异常仍保留在服务日志中供排查。
            LOGGER.warning("任务 %s 的完整经验沉淀异常，成功状态保持不变", task_id, exc_info=True)

    async def _create_required_review(self, task_id: UUID, records: list[ArtifactRecord]) -> bool:
        """候选本体直接送审；候选图谱须在质量报告齐备后送审。"""
        existing_reviews = await self._runtime.reviews.list_reviews(task_id)
        reviewed_artifact_ids = {item.target_artifact_id for item in existing_reviews}
        candidates = [
            item
            for item in records
            if item.ref.artifact_type
            in {"FusedCandidateOntology", "CandidateOntology", "CandidateGraph"}
            and item.ref.artifact_id not in reviewed_artifact_ids
        ]
        # 本体不经过候选质量评估。候选版本一产生就进入用户预览、修改和批准门禁。
        # 先处理最新本体，避免后续图谱分支掩盖尚未确认的本体。
        ontology_candidates = [
            item
            for item in candidates
            if item.ref.artifact_type in {"CandidateOntology", "FusedCandidateOntology"}
        ]
        for candidate in reversed(ontology_candidates):
            async with self._runtime.database.session_factory() as session:
                producer_run = await SqlAlchemyAgentRunRepository(session).get_result(
                    candidate.ref.producer_run_id
                )
            try:
                await self._runtime.reviews.request_review(
                    CreateHumanReviewCommand(
                        task_id=task_id,
                        subtask_id=(producer_run.subtask_id if producer_run is not None else None),
                        target_type=ReviewTargetType.ONTOLOGY,
                        target_artifact_id=candidate.ref.artifact_id,
                        quality_report_artifact_ids=[],
                    )
                )
                return True
            except ReviewConflictError:
                return True

        reports = [
            item
            for item in records
            if item.ref.artifact_type in {"OntologyQualityReport", "GraphQualityReport"}
        ]
        for candidate in reversed(candidates):
            if candidate.ref.artifact_type != "CandidateGraph":
                continue
            expected_report = "GraphQualityReport"
            for report in reversed(reports):
                if report.ref.artifact_type != expected_report:
                    continue
                try:
                    payload = json.loads(
                        await self._runtime.artifacts.read_content(report.ref.artifact_id)
                    )
                except (KGAgentError, ValueError, UnicodeDecodeError):
                    continue
                if str(payload.get("target_artifact_id")) != str(candidate.ref.artifact_id):
                    continue
                # 审核请求绑定质量评估子任务。这样批准动作才能只解锁其依赖链中的
                # ontology_publish / graph_publish，而不是扫描或误放行无关发布任务。
                async with self._runtime.database.session_factory() as session:
                    quality_run = await SqlAlchemyAgentRunRepository(session).get_result(
                        report.ref.producer_run_id
                    )
                try:
                    await self._runtime.reviews.request_review(
                        CreateHumanReviewCommand(
                            task_id=task_id,
                            subtask_id=(
                                quality_run.subtask_id if quality_run is not None else None
                            ),
                            target_type=ReviewTargetType.GRAPH
                            if candidate.ref.artifact_type == "CandidateGraph"
                            else ReviewTargetType.ONTOLOGY,
                            target_artifact_id=candidate.ref.artifact_id,
                            quality_report_artifact_ids=[report.ref.artifact_id],
                        )
                    )
                    return True
                except ReviewConflictError:
                    return True
        return False
