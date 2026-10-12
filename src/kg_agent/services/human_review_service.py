"""实现本体与候选图谱的确定性人工审核门禁。

本服务负责创建审核请求、验证展示附件、记录用户修改/批准/拒绝、生成决策 Artifact，
以及同步任务状态、审计事件和 SharedContext；它不是 Agent，也不执行发布操作。
"""

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from kg_agent.domain.artifact_content_models import (
    ChangeSetContent,
    QualityReportContent,
)
from kg_agent.domain.artifact_models import (
    ArtifactCreateCommand,
    ArtifactRecord,
    ArtifactRef,
)
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import (
    ArtifactStatus,
    AssignmentStatus,
    HumanReviewStatus,
    MasterTaskStatus,
    ReviewOption,
    ReviewTargetType,
    SubTaskStatus,
    TaskEventType,
)
from kg_agent.domain.review_models import HumanReviewRequest
from kg_agent.domain.state_transitions import (
    ensure_master_task_transition,
    ensure_subtask_transition,
)
from kg_agent.domain.trace_models import ExecutionTrace, TraceEvent
from kg_agent.exceptions import (
    ArtifactValidationError,
    ReviewConflictError,
    StateTransitionError,
)
from kg_agent.repositories.interfaces import (
    AgentRunRepository,
    ArtifactRepository,
    AssignmentRepository,
    ContextRepository,
    EventRepository,
    HumanReviewRepository,
    TaskRepository,
)
from kg_agent.services.artifact_service import ArtifactService
from kg_agent.services.context_projection import build_context_projection
from kg_agent.services.review_commands import (
    CreateHumanReviewCommand,
    HumanReviewMaterials,
    ResolveHumanReviewCommand,
)

Clock = Callable[[], datetime]


class HumanReviewService:
    """管理一个事务中的人工审核请求、用户决策和任务门禁状态。"""

    def __init__(
        self,
        *,
        reviews: HumanReviewRepository,
        artifacts: ArtifactRepository,
        tasks: TaskRepository,
        assignments: AssignmentRepository,
        contexts: ContextRepository,
        events: EventRepository,
        traces: AgentRunRepository,
        artifact_service: ArtifactService,
        clock: Clock | None = None,
    ) -> None:
        """注入同一事务内的 Repository、ArtifactService 和可测试时钟。"""

        self._reviews = reviews
        self._artifacts = artifacts
        self._tasks = tasks
        self._assignments = assignments
        self._contexts = contexts
        self._events = events
        self._traces = traces
        self._artifact_service = artifact_service
        self._clock = clock or (lambda: datetime.now(UTC))

    async def request_review(
        self,
        command: CreateHumanReviewCommand,
    ) -> HumanReviewRequest:
        """验证候选结果及附件，创建审核请求并让 MasterTask 等待用户审核。

        Raises:
            ArtifactValidationError: 目标或附件类型、任务、checksum、质量目标不合法。
            ReviewConflictError: 同一目标版本已经存在不同的待处理审核请求。
            StateTransitionError: 当前 MasterTask 不能进入人工审核状态。
        """

        existing_by_id = await self._reviews.get(command.review_id)
        if existing_by_id is not None:
            self._ensure_request_idempotent(existing_by_id, command)
            return existing_by_id

        master = await self._tasks.get_master(command.task_id, for_update=True)
        if master is None:
            raise StateTransitionError("HumanReviewRequest 所属 MasterTask 不存在")
        if master.status not in {
            MasterTaskStatus.ACTIVE,
            MasterTaskStatus.WAITING_HUMAN,
        }:
            raise StateTransitionError("当前 MasterTask 状态不能创建人工审核请求")

        target = await self._require_artifact(
            command.target_artifact_id,
            command.task_id,
            self._target_artifact_types(command.target_type),
        )
        if command.subtask_id is not None:
            subtask = await self._tasks.get_subtask(command.subtask_id)
            if subtask is None or subtask.master_task_id != command.task_id:
                raise ReviewConflictError("审核关联的 AgentSubTask 不属于当前任务")
        await self._validate_attachments(command, target)

        pending = await self._reviews.find_pending(
            task_id=command.task_id,
            target_artifact_id=target.ref.artifact_id,
            target_artifact_checksum=target.ref.checksum,
        )
        if pending is not None:
            self._ensure_request_idempotent(pending, command, ignore_review_id=True)
            return pending

        now = self._clock()
        review = HumanReviewRequest(
            review_id=command.review_id,
            task_id=command.task_id,
            subtask_id=command.subtask_id,
            target_type=command.target_type,
            target_artifact_id=target.ref.artifact_id,
            target_artifact_checksum=target.ref.checksum,
            quality_report_artifact_ids=command.quality_report_artifact_ids,
            conflict_report_artifact_ids=command.conflict_report_artifact_ids,
            evidence_artifact_ids=command.evidence_artifact_ids,
            allowed_actions=command.allowed_actions,
            status=HumanReviewStatus.PENDING,
            requested_at=now,
        )
        await self._reviews.add(review)
        if master.status is MasterTaskStatus.ACTIVE:
            ensure_master_task_transition(master.status, MasterTaskStatus.WAITING_HUMAN)
            await self._tasks.update_master(
                master.model_copy(
                    update={
                        "status": MasterTaskStatus.WAITING_HUMAN,
                        "updated_at": now,
                    }
                )
            )
            await self._append_master_status_event(
                task_id=command.task_id,
                actor_id="human_review_service",
                before=master.status,
                after=MasterTaskStatus.WAITING_HUMAN,
                now=now,
            )
        await self._events.append(
            task_id=command.task_id,
            subtask_id=command.subtask_id,
            event_type=TaskEventType.HUMAN_REVIEW_REQUESTED,
            actor_type="SERVICE",
            actor_id="human_review_service",
            payload={
                "review_id": str(review.review_id),
                "target_type": review.target_type.value,
                "target_artifact_id": str(review.target_artifact_id),
                "target_artifact_checksum": review.target_artifact_checksum,
            },
            occurred_at=now,
        )
        trace = self._review_trace(
            review=review,
            action="REQUESTED",
            actor_type="SERVICE",
            actor_name="human_review_service",
            summary="创建人工审核请求",
            occurred_at=now,
        )
        await self._traces.add_trace(trace)
        await self._create_snapshot(
            command.task_id,
            now,
            review_id=review.review_id,
            trace_id=trace.trace_id,
        )
        return review

    async def resolve_review(
        self,
        command: ResolveHumanReviewCommand,
    ) -> HumanReviewRequest:
        """记录用户审核决策，并生成 Approval 或 ChangeSet Artifact。

        APPROVE 生成绑定目标 ID/checksum 的 Approval；SUBMIT_CHANGES 只生成不可变
        ChangeSet，不能沿用旧 Approval；REJECT 保存反馈并交回总控 Agent 安排返工。
        """

        review = await self._reviews.get(command.review_id, for_update=True)
        if review is None:
            raise ReviewConflictError("HumanReviewRequest 不存在")
        if review.status is not HumanReviewStatus.PENDING:
            await self._ensure_resolution_idempotent(review, command)
            return review
        if command.action not in review.allowed_actions:
            raise ReviewConflictError("该审核请求不允许当前操作")

        master = await self._tasks.get_master(review.task_id, for_update=True)
        if master is None or master.status is not MasterTaskStatus.WAITING_HUMAN:
            raise StateTransitionError("MasterTask 当前不在 WAITING_HUMAN")
        target = await self._require_artifact(
            review.target_artifact_id,
            review.task_id,
            self._target_artifact_types(review.target_type),
        )
        if target.ref.checksum != review.target_artifact_checksum:
            raise ReviewConflictError("审核目标 checksum 已变化，旧请求不能继续使用")

        now = self._clock()
        decision_artifact_id: UUID | None = None
        resolved_publication_title: str | None = None
        if command.action is ReviewOption.APPROVE:
            # 工作台会强制用户填写名称。保留 API 调用方的安全兼容回退：早期自动化或
            # 历史集成未携带字段时，用当前候选摘要生成不可变名称，而不会中断既有审核。
            # 任何名称都只会写入本次 Approval，绝不修改候选 Artifact。
            publication_title = self._publication_title(command, target)
            resolved_publication_title = publication_title
            decision = await self._create_approval(
                review,
                target,
                command.model_copy(update={"publication_title": publication_title}),
                now,
            )
            decision_artifact_id = decision.ref.artifact_id
            target_status = HumanReviewStatus.APPROVED
            await self._unlock_approved_dependents(review.subtask_id, now)
        elif command.action is ReviewOption.SUBMIT_CHANGES:
            decision = await self._create_changeset(review, target, command, now)
            decision_artifact_id = decision.ref.artifact_id
            target_status = HumanReviewStatus.CHANGES_REQUESTED
        else:
            target_status = HumanReviewStatus.REJECTED

        resolved = review.model_copy(
            update={
                "status": target_status,
                "resolved_at": now,
                "decision_artifact_id": decision_artifact_id,
                "reviewer_feedback": command.reviewer_feedback,
            }
        )
        await self._reviews.update(resolved)
        await self._events.append(
            task_id=review.task_id,
            subtask_id=review.subtask_id,
            event_type=TaskEventType.HUMAN_REVIEW_RESOLVED,
            actor_type="USER",
            actor_id=command.reviewer_id,
            reason_code=command.action.value,
            payload={
                "review_id": str(review.review_id),
                "status": target_status.value,
                "decision_artifact_id": (
                    str(decision_artifact_id) if decision_artifact_id else None
                ),
                "reviewer_feedback": command.reviewer_feedback,
                "publication_title": resolved_publication_title,
            },
            occurred_at=now,
        )

        pending = [
            item
            for item in await self._reviews.list_for_task(review.task_id)
            if item.status is HumanReviewStatus.PENDING
        ]
        if not pending:
            ensure_master_task_transition(
                MasterTaskStatus.WAITING_HUMAN,
                MasterTaskStatus.ACTIVE,
            )
            await self._tasks.update_master(
                master.model_copy(update={"status": MasterTaskStatus.ACTIVE, "updated_at": now})
            )
            await self._append_master_status_event(
                task_id=review.task_id,
                actor_id=command.reviewer_id,
                before=MasterTaskStatus.WAITING_HUMAN,
                after=MasterTaskStatus.ACTIVE,
                now=now,
            )

        summary = f"用户执行审核操作 {command.action.value}"
        if command.reviewer_feedback:
            summary = f"{summary}；反馈：{command.reviewer_feedback}"
        trace = self._review_trace(
            review=resolved,
            action=command.action.value,
            actor_type="USER",
            actor_name=command.reviewer_id,
            summary=summary,
            occurred_at=now,
            output_id=decision_artifact_id,
        )
        await self._traces.add_trace(trace)
        await self._create_snapshot(
            review.task_id,
            now,
            review_id=review.review_id,
            trace_id=trace.trace_id,
        )
        return resolved

    async def _unlock_approved_dependents(self, subtask_id: UUID | None, now: datetime) -> None:
        """审核批准后解锁依赖审核质量任务的发布子任务。

        Approval Artifact 已在同一事务中创建；Worker 启动发布任务时会通过依赖输入
        解析器绑定该 Approval。非发布下游不应跨越人工审核门禁。
        """

        # 兼容 API 主动创建且未关联质量子任务的审核；这类审核可以产出 Approval，
        # 但没有确定依赖锚点，因此不会自动解锁任何发布 Assignment。
        if subtask_id is None:
            return
        for dependent in await self._tasks.list_dependents(subtask_id):
            if dependent.status is not SubTaskStatus.BLOCKED:
                continue
            if dependent.subtask_type not in {"ontology_publish", "graph_publish"}:
                continue
            statuses = await self._tasks.list_dependency_statuses(dependent.subtask_id)
            if any(status is not SubTaskStatus.SUCCEEDED for status in statuses):
                continue
            ensure_subtask_transition(SubTaskStatus.BLOCKED, SubTaskStatus.READY)
            await self._tasks.update_subtask_status(
                dependent.subtask_id,
                SubTaskStatus.READY,
                now,
            )
            assignment = AgentAssignment(
                assignment_id=uuid5(
                    NAMESPACE_URL,
                    f"kg-agent:approved-assignment:{dependent.subtask_id}",
                ),
                subtask_id=dependent.subtask_id,
                assigned_agent_type=dependent.assigned_agent_type,
                status=AssignmentStatus.PENDING,
                created_at=now,
            )
            await self._assignments.add(assignment)
            ensure_subtask_transition(SubTaskStatus.READY, SubTaskStatus.ASSIGNED)
            await self._tasks.update_subtask_status(
                dependent.subtask_id,
                SubTaskStatus.ASSIGNED,
                now,
            )
            await self._events.append(
                task_id=dependent.master_task_id,
                subtask_id=dependent.subtask_id,
                assignment_id=assignment.assignment_id,
                event_type=TaskEventType.ASSIGNMENT_CREATED,
                actor_type="SERVICE",
                actor_id="human_review_service",
                payload={"attempt": 1, "unlocked_by_review": True},
                occurred_at=now,
            )

    async def get_review(self, review_id: UUID) -> HumanReviewRequest | None:
        """读取一个审核请求；不存在时返回 None。"""

        return await self._reviews.get(review_id)

    async def list_reviews(self, task_id: UUID) -> list[HumanReviewRequest]:
        """按时间返回一个 MasterTask 的全部审核请求。"""

        return await self._reviews.list_for_task(task_id)

    async def get_review_materials(
        self,
        review_id: UUID,
    ) -> HumanReviewMaterials:
        """返回审核预览所需全部 ArtifactRef，不内嵌大型 Artifact 内容。"""

        review = await self._reviews.get(review_id)
        if review is None:
            raise ReviewConflictError("HumanReviewRequest 不存在")
        target = await self._require_artifact(
            review.target_artifact_id,
            review.task_id,
            self._target_artifact_types(review.target_type),
        )

        async def refs(ids: list[UUID], allowed: set[str]) -> list[ArtifactRef]:
            """按审核记录顺序读取并校验一组附件引用。"""

            return [
                (await self._require_artifact(item, review.task_id, allowed)).ref for item in ids
            ]

        quality_type = (
            "OntologyQualityReport"
            if review.target_type is ReviewTargetType.ONTOLOGY
            else "GraphQualityReport"
        )
        return HumanReviewMaterials(
            review=review,
            target=target.ref,
            quality_reports=await refs(
                review.quality_report_artifact_ids,
                {quality_type},
            ),
            conflict_reports=await refs(
                review.conflict_report_artifact_ids,
                {"ConflictReport"},
            ),
            evidence_artifacts=await refs(
                review.evidence_artifact_ids,
                {"EvidenceDataset"},
            ),
        )

    async def _validate_attachments(
        self,
        command: CreateHumanReviewCommand,
        target: ArtifactRecord,
    ) -> None:
        """验证质量、冲突和证据附件都属于目标任务且内容指向目标版本。"""

        quality_type = (
            "OntologyQualityReport"
            if command.target_type is ReviewTargetType.ONTOLOGY
            else "GraphQualityReport"
        )
        if (
            command.target_type is ReviewTargetType.GRAPH
            and not command.quality_report_artifact_ids
        ):
            raise ArtifactValidationError("候选图谱审核必须提供 GraphQualityReport")
        for artifact_id in command.quality_report_artifact_ids:
            report = await self._require_artifact(
                artifact_id,
                command.task_id,
                {quality_type},
            )
            payload = QualityReportContent.model_validate(
                json.loads(await self._artifact_service.read_content(report.ref.artifact_id))
            )
            if payload.target_artifact_id != target.ref.artifact_id:
                raise ArtifactValidationError("质量报告未指向当前审核目标")
        for artifact_id in command.conflict_report_artifact_ids:
            await self._require_artifact(artifact_id, command.task_id, {"ConflictReport"})
        for artifact_id in command.evidence_artifact_ids:
            await self._require_artifact(artifact_id, command.task_id, {"EvidenceDataset"})

    async def _require_artifact(
        self,
        artifact_id: UUID,
        task_id: UUID,
        allowed_types: set[str],
    ) -> ArtifactRecord:
        """读取可用 Artifact，并校验任务隔离与允许类型。"""

        record = await self._artifacts.get(artifact_id)
        if record is None:
            raise ArtifactValidationError(f"审核 Artifact 不存在：{artifact_id}")
        if record.ref.task_id != task_id:
            raise ArtifactValidationError("人工审核不能引用其他 MasterTask 的 Artifact")
        if record.ref.status is not ArtifactStatus.AVAILABLE:
            raise ArtifactValidationError("人工审核只能使用 AVAILABLE Artifact")
        if record.ref.artifact_type not in allowed_types:
            raise ArtifactValidationError(f"审核 Artifact 类型不匹配：{record.ref.artifact_type}")
        return record

    async def _create_approval(
        self,
        review: HumanReviewRequest,
        target: ArtifactRecord,
        command: ResolveHumanReviewCommand,
        now: datetime,
    ) -> ArtifactRecord:
        """创建只能由 HumanReviewService 产生的目标版本 Approval。"""

        artifact_type = (
            "OntologyApproval"
            if review.target_type is ReviewTargetType.ONTOLOGY
            else "GraphApproval"
        )
        artifact_id = uuid5(
            NAMESPACE_URL,
            f"kg-agent:review-decision:{review.review_id}:APPROVE",
        )
        return await self._artifact_service.create(
            ArtifactCreateCommand(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                task_id=review.task_id,
                task_spec_revision=target.ref.task_spec_revision,
                producer_agent="user",
                producer_skill="human_review_service",
                producer_run_id=review.review_id,
                parent_artifact_ids=[
                    review.target_artifact_id,
                    *review.quality_report_artifact_ids,
                    *review.conflict_report_artifact_ids,
                    *review.evidence_artifact_ids,
                ],
                ontology_id=target.ref.ontology_id,
                ontology_version=target.ref.ontology_version,
                content_summary=f"用户批准 {review.target_type.value} 候选版本",
                content={
                    "schema_version": "1.0.0",
                    "target_artifact_id": str(review.target_artifact_id),
                    "target_checksum": review.target_artifact_checksum,
                    "decision": "APPROVED",
                    "publication_title": (command.publication_title or "").strip(),
                    "comment": command.reviewer_feedback or "",
                },
            )
        )

    @staticmethod
    def _publication_title(command: ResolveHumanReviewCommand, target: ArtifactRecord) -> str:
        """规范化用户发布名称，并为旧 API 调用保留可追溯的候选摘要回退。"""

        if command.publication_title is not None and command.publication_title.strip():
            return command.publication_title.strip()
        fallback = target.ref.content_summary.strip()
        if fallback:
            return fallback[:120]
        prefix = "本体" if target.ref.artifact_type != "CandidateGraph" else "图谱"
        return f"{prefix}-{str(target.ref.artifact_id)[:8]}"

    async def _create_changeset(
        self,
        review: HumanReviewRequest,
        target: ArtifactRecord,
        command: ResolveHumanReviewCommand,
        now: datetime,
    ) -> ArtifactRecord:
        """把用户修改保存为不可变 ChangeSet，后续由正式子任务应用并重新评估。"""

        artifact_type = (
            "OntologyChangeSet"
            if review.target_type is ReviewTargetType.ONTOLOGY
            else "GraphChangeSet"
        )
        artifact_id = uuid5(
            NAMESPACE_URL,
            f"kg-agent:review-decision:{review.review_id}:SUBMIT_CHANGES",
        )
        return await self._artifact_service.create(
            ArtifactCreateCommand(
                artifact_id=artifact_id,
                artifact_type=artifact_type,
                task_id=review.task_id,
                task_spec_revision=target.ref.task_spec_revision,
                producer_agent="user",
                producer_skill="human_review_service",
                producer_run_id=review.review_id,
                parent_artifact_ids=[review.target_artifact_id],
                ontology_id=target.ref.ontology_id,
                ontology_version=target.ref.ontology_version,
                content_summary=f"用户提交 {review.target_type.value} 结构化修改",
                content={
                    "schema_version": "1.0.0",
                    "target_artifact_id": str(review.target_artifact_id),
                    "target_checksum": review.target_artifact_checksum,
                    "operations": [item.model_dump(mode="json") for item in command.operations],
                },
            )
        )

    async def _create_snapshot(
        self,
        task_id: UUID,
        now: datetime,
        *,
        review_id: UUID,
        trace_id: UUID,
    ) -> SharedTaskContext:
        """追加包含审核和公开轨迹引用的 SharedContext 快照。"""

        current = await self._contexts.get(task_id)
        context = await build_context_projection(
            task_id=task_id,
            version=1 if current is None else current.version + 1,
            updated_at=now,
            task_repository=self._tasks,
            assignment_repository=self._assignments,
            context_repository=self._contexts,
        )
        review_ids = list(context.human_review_request_ids)
        trace_ids = list(context.execution_trace_ids)
        if review_id not in review_ids:
            review_ids.append(review_id)
        if trace_id not in trace_ids:
            trace_ids.append(trace_id)
        context = context.model_copy(
            update={
                "human_review_request_ids": review_ids,
                "execution_trace_ids": trace_ids,
            }
        )
        await self._contexts.add_snapshot(context)
        await self._events.append(
            task_id=task_id,
            event_type=TaskEventType.CONTEXT_VERSION_CREATED,
            actor_type="SERVICE",
            actor_id="human_review_service",
            payload={"version": context.version},
            occurred_at=now,
        )
        return context

    async def _append_master_status_event(
        self,
        *,
        task_id: UUID,
        actor_id: str,
        before: MasterTaskStatus,
        after: MasterTaskStatus,
        now: datetime,
    ) -> None:
        """记录审核门禁引起的 MasterTask 状态变化。"""

        await self._events.append(
            task_id=task_id,
            event_type=TaskEventType.MASTER_TASK_STATUS_CHANGED,
            actor_type="SERVICE",
            actor_id=actor_id,
            reason_code="HUMAN_REVIEW_GATE",
            payload={"from": before.value, "to": after.value},
            occurred_at=now,
        )

    @staticmethod
    def _target_artifact_types(target_type: ReviewTargetType) -> set[str]:
        """返回每类审核门禁允许的候选 Artifact 类型。"""

        if target_type is ReviewTargetType.ONTOLOGY:
            return {"CandidateOntology", "FusedCandidateOntology"}
        return {"CandidateGraph"}

    @staticmethod
    def _ensure_request_idempotent(
        existing: HumanReviewRequest,
        command: CreateHumanReviewCommand,
        *,
        ignore_review_id: bool = False,
    ) -> None:
        """确保重复请求没有把同一审核 ID 或目标版本用于不同参数。"""

        if not ignore_review_id and existing.review_id != command.review_id:
            raise ReviewConflictError("review_id 不一致")
        if (
            existing.task_id != command.task_id
            or existing.subtask_id != command.subtask_id
            or existing.target_type != command.target_type
            or existing.target_artifact_id != command.target_artifact_id
            or existing.quality_report_artifact_ids != command.quality_report_artifact_ids
            or existing.conflict_report_artifact_ids != command.conflict_report_artifact_ids
            or existing.evidence_artifact_ids != command.evidence_artifact_ids
            or existing.allowed_actions != command.allowed_actions
        ):
            raise ReviewConflictError("重复 HumanReviewRequest 的内容不一致")

    async def _ensure_resolution_idempotent(
        self,
        review: HumanReviewRequest,
        command: ResolveHumanReviewCommand,
    ) -> None:
        """仅允许与已经提交的审核终态和 ChangeSet 完全一致的重放。"""

        status_by_action = {
            ReviewOption.APPROVE: HumanReviewStatus.APPROVED,
            ReviewOption.REJECT: HumanReviewStatus.REJECTED,
            ReviewOption.SUBMIT_CHANGES: HumanReviewStatus.CHANGES_REQUESTED,
        }
        if (
            review.status is not status_by_action[command.action]
            or review.reviewer_feedback != command.reviewer_feedback
        ):
            raise ReviewConflictError("HumanReviewRequest 已被其他决策处理")
        if command.action is ReviewOption.SUBMIT_CHANGES:
            if review.decision_artifact_id is None:
                raise ReviewConflictError("已处理审核缺少 ChangeSet Artifact")
            payload = ChangeSetContent.model_validate(
                json.loads(await self._artifact_service.read_content(review.decision_artifact_id))
            )
            if payload.operations != command.operations:
                raise ReviewConflictError("重复审核提交的 ChangeSet 操作不一致")

    @staticmethod
    def _review_trace(
        *,
        review: HumanReviewRequest,
        action: str,
        actor_type: Literal["USER", "AGENT", "SKILL", "SERVICE", "MODEL"],
        actor_name: str,
        summary: str,
        occurred_at: datetime,
        output_id: UUID | None = None,
    ) -> ExecutionTrace:
        """构造不包含隐藏思维过程的审核请求或用户决策轨迹。"""

        trace_id = uuid5(
            NAMESPACE_URL,
            f"kg-agent:review-trace:{review.review_id}:{action}",
        )
        event = TraceEvent(
            event_id=uuid5(
                NAMESPACE_URL,
                f"kg-agent:review-trace-event:{review.review_id}:{action}",
            ),
            event_type=f"HUMAN_REVIEW_{action}",
            occurred_at=occurred_at,
            actor_type=actor_type,
            actor_name=actor_name,
            summary=summary,
            input_refs=[str(review.target_artifact_id)],
            output_refs=[str(output_id)] if output_id else [],
        )
        return ExecutionTrace(
            trace_id=trace_id,
            task_id=review.task_id,
            subtask_id=review.subtask_id,
            events=[event],
            started_at=occurred_at,
            finished_at=occurred_at,
            status=review.status.value,
        )
