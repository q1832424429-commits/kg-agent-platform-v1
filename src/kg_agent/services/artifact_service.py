"""实现 Artifact 创建、读取、状态变更和 SharedContext 投影更新。

本服务以确定性规则维护不可变内容、checksum、血缘、容量和 Assignment fencing；
调用方负责数据库事务边界，真实内容由注入的对象存储协议保存。
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from uuid import UUID

from kg_agent.domain.artifact_content_models import (
    ApprovalContent,
    PublishedGraphContent,
    PublishedOntologyContent,
)
from kg_agent.domain.artifact_models import ArtifactCreateCommand, ArtifactRecord, ArtifactRef
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import (
    ArtifactStatus,
    AssignmentStatus,
    SubTaskStatus,
    TaskEventType,
)
from kg_agent.exceptions import (
    ArtifactNotFoundError,
    ArtifactValidationError,
    AssignmentConflictError,
    PersistenceError,
)
from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.repositories.interfaces import (
    ArtifactRepository,
    AssignmentRepository,
    ContextRepository,
    EventRepository,
    TaskRepository,
)
from kg_agent.services.context_projection import build_context_projection

Clock = Callable[[], datetime]


class ArtifactService:
    """在同一任务内创建不可变 Artifact 并维护一致的上下文事实。"""

    def __init__(
        self,
        *,
        artifact_repository: ArtifactRepository,
        task_repository: TaskRepository,
        assignment_repository: AssignmentRepository,
        context_repository: ContextRepository,
        event_repository: EventRepository,
        storage: ObjectStorage,
        registry: ArtifactRegistry,
        max_file_bytes: int,
        max_files_per_task: int,
        max_total_bytes_per_task: int,
        clock: Clock | None = None,
    ) -> None:
        """注入共享事务 Repository、对象存储、Registry 和容量限制。"""

        self._artifacts = artifact_repository
        self._tasks = task_repository
        self._assignments = assignment_repository
        self._contexts = context_repository
        self._events = event_repository
        self._storage = storage
        self._registry = registry
        self._max_file_bytes = max_file_bytes
        self._max_files_per_task = max_files_per_task
        self._max_total_bytes_per_task = max_total_bytes_per_task
        self._clock = clock or (lambda: datetime.now(UTC))
        self._pending_object_keys: set[str] = set()

    @property
    def pending_object_keys(self) -> tuple[str, ...]:
        """返回本次事务新建的对象键，供应用入口在回滚后安全补偿。"""

        return tuple(sorted(self._pending_object_keys))

    async def create(self, command: ArtifactCreateCommand) -> ArtifactRecord:
        """校验并保存 Artifact，重复 artifact_id 仅允许完全幂等重放。

        Raises:
            ArtifactValidationError: 内容、容量、父级或版本不合法。
            AssignmentConflictError: Worker 凭证已经过期或与分配不匹配。
            PersistenceError: 同一 artifact_id 被用于不同内容。
        """

        self._validate_protected_command(command)
        # 上传前只做无锁预检，不能在 MinIO 网络 I/O 期间阻塞 Assignment 心跳。
        await self._validate_claim(command, for_update=False)
        master = await self._tasks.get_master(command.task_id, for_update=False)
        if master is None:
            raise ArtifactValidationError("Artifact 所属 MasterTask 不存在")
        if await self._tasks.get_task_spec(command.task_id, command.task_spec_revision) is None:
            raise ArtifactValidationError("Artifact 引用的 TaskSpec revision 不存在")

        registration = self._registry.require(command.artifact_type)
        content = self._registry.serialize_and_validate(
            artifact_type=command.artifact_type,
            schema_version=command.schema_version,
            content=command.content,
        )
        if registration.content_kind == "JSON" and command.media_type != "application/json":
            raise ArtifactValidationError("JSON Artifact 的 media_type 必须是 application/json")
        checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if command.artifact_type == "RawDocument":
            allowed_media_types = {
                "application/pdf",
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
                "image/jpeg",
                "image/png",
                "image/tiff",
                "image/webp",
            }
            if command.media_type not in allowed_media_types:
                raise ArtifactValidationError("V1 不支持该原始文档 MIME 类型")
        existing = await self._artifacts.get(command.artifact_id)
        if existing is not None:
            self._ensure_idempotent(existing, command, checksum)
            return existing

        parent_ids = list(dict.fromkeys(command.parent_artifact_ids))
        if command.supersedes_artifact_id is not None:
            if command.supersedes_artifact_id not in parent_ids:
                parent_ids.append(command.supersedes_artifact_id)
        parents = await self._validate_parents(command, parent_ids)
        await self._validate_protected_parents(command, parents)
        if command.supersedes_artifact_id is not None:
            superseded = next(
                item for item in parents if item.ref.artifact_id == command.supersedes_artifact_id
            )
            if superseded.ref.artifact_type != command.artifact_type:
                raise ArtifactValidationError("只能替代相同类型的 Artifact")
        await self._validate_capacity(command, len(content))

        now = self._clock()
        suffix = "json" if command.media_type == "application/json" else "bin"
        # 内容摘要进入键，避免同一幂等 ID 的冲突请求在数据库校验前覆盖彼此。
        object_key = (
            f"tasks/{command.task_id}/artifacts/{command.artifact_id}/"
            f"content-{checksum.removeprefix('sha256:')}.{suffix}"
        )
        object_existed = await self._storage.exists(object_key)
        content_uri = await self._storage.put_bytes(object_key, content, command.media_type)
        if not object_existed:
            self._pending_object_keys.add(object_key)
        ref = self._build_ref(
            command=command,
            parent_ids=parent_ids,
            content_uri=content_uri,
            checksum=checksum,
            created_at=now,
        )
        record = ArtifactRecord(
            ref=ref,
            object_key=object_key,
            media_type=command.media_type,
            size_bytes=len(content),
        )
        try:
            # 外部上传完成后才短暂获取领域锁，并与 AssignmentService 保持相同锁顺序。
            await self._validate_claim(command, for_update=True)
            locked_master = await self._tasks.get_master(command.task_id, for_update=True)
            if locked_master is None:
                raise ArtifactValidationError("上传期间 MasterTask 已不存在")
            existing = await self._artifacts.get(command.artifact_id)
            if existing is not None:
                self._ensure_idempotent(existing, command, checksum)
                return existing
            # 容量、父级状态和审核绑定可能在上传期间改变，锁内必须再次验证。
            locked_parents = await self._validate_parents(command, parent_ids)
            await self._validate_protected_parents(command, locked_parents)
            await self._validate_capacity(command, len(content))
            # 与回滚补偿共用任务锁，不能登记已经被并发失败请求清理的对象。
            if not await self._storage.exists(object_key):
                raise ArtifactValidationError("上传对象已不存在，不能登记 Artifact 元数据")
            await self._artifacts.add(record)
            if command.supersedes_artifact_id is not None:
                await self._artifacts.update_status(
                    command.supersedes_artifact_id, ArtifactStatus.SUPERSEDED
                )
                await self._events.append(
                    task_id=command.task_id,
                    event_type=TaskEventType.ARTIFACT_STATUS_CHANGED,
                    actor_type="SERVICE",
                    actor_id="artifact_service",
                    occurred_at=now,
                    reason_code="REPLACED_BY_NEW_ARTIFACT",
                    payload={
                        "artifact_id": str(command.supersedes_artifact_id),
                        "from": ArtifactStatus.AVAILABLE.value,
                        "to": ArtifactStatus.SUPERSEDED.value,
                        "replacement_artifact_id": str(command.artifact_id),
                    },
                )
            await self._events.append(
                task_id=command.task_id,
                event_type=TaskEventType.ARTIFACT_CREATED,
                actor_type="SERVICE",
                actor_id="artifact_service",
                occurred_at=now,
                assignment_id=command.assignment_id,
                payload={
                    "artifact_id": str(command.artifact_id),
                    "artifact_type": command.artifact_type,
                    "checksum": checksum,
                },
            )
            await self._create_context_snapshot(command.task_id, now)
        except Exception:
            # 此时数据库事务可能已失败；必须先回滚，再由应用入口加任务锁确认无引用后清理。
            # 不在这里盲删，防止误删另一并发幂等请求已经提交的同内容对象。
            raise
        return record

    async def read_content(self, artifact_id: UUID) -> bytes:
        """读取内容并重新计算 checksum，检测对象存储内容篡改或损坏。"""

        record = await self._artifacts.get(artifact_id)
        if record is None:
            raise ArtifactNotFoundError("Artifact 不存在")
        content = await self._storage.get_bytes(record.object_key)
        checksum = f"sha256:{hashlib.sha256(content).hexdigest()}"
        if checksum != record.ref.checksum or len(content) != record.size_bytes:
            raise ArtifactValidationError("Artifact 内容 checksum 与元数据不一致")
        return content

    async def change_status(
        self,
        *,
        artifact_id: UUID,
        target: ArtifactStatus,
        actor_id: str,
        reason_code: str,
    ) -> ArtifactRecord:
        """把可用 Artifact 标记为失效或已被替代；禁止恢复和内容覆盖。"""

        record = await self._artifacts.get(artifact_id)
        if record is not None:
            await self._tasks.get_master(record.ref.task_id, for_update=True)
            record = await self._artifacts.get(artifact_id, for_update=True)
        if record is None:
            raise ArtifactNotFoundError("Artifact 不存在")
        if record.ref.status is not ArtifactStatus.AVAILABLE:
            raise ArtifactValidationError("只有 AVAILABLE Artifact 可以变更生命周期状态")
        if target not in {ArtifactStatus.INVALIDATED, ArtifactStatus.SUPERSEDED}:
            raise ArtifactValidationError("Artifact 状态只能变为 INVALIDATED 或 SUPERSEDED")
        now = self._clock()
        await self._artifacts.update_status(artifact_id, target)
        await self._events.append(
            task_id=record.ref.task_id,
            event_type=TaskEventType.ARTIFACT_STATUS_CHANGED,
            actor_type="SERVICE",
            actor_id=actor_id,
            occurred_at=now,
            reason_code=reason_code,
            payload={
                "artifact_id": str(artifact_id),
                "from": record.ref.status.value,
                "to": target.value,
            },
        )
        await self._create_context_snapshot(record.ref.task_id, now)
        return record.model_copy(update={"ref": record.ref.model_copy(update={"status": target})})

    @staticmethod
    def _validate_protected_command(command: ArtifactCreateCommand) -> None:
        """限制 Approval 和 Published Artifact 只能由确定性服务路径创建。"""

        approval_types = {"OntologyApproval", "GraphApproval"}
        published_skills = {
            "PublishedOntology": "publish_ontology",
            "PublishedGraph": "publish_graph",
        }
        if command.artifact_type in approval_types:
            if (
                command.producer_agent != "user"
                or command.producer_skill != "human_review_service"
                or command.assignment_id is not None
            ):
                raise ArtifactValidationError("Approval 只能由 HumanReviewService 代表用户创建")
            if not isinstance(command.content, dict):
                raise ArtifactValidationError("Approval 内容必须是 JSON 对象")
            approval = ApprovalContent.model_validate(command.content)
            if approval.decision != "APPROVED":
                raise ArtifactValidationError("Approval Artifact 只能记录 APPROVED 决策")
        if command.artifact_type in published_skills:
            if (
                command.artifact_type == "PublishedOntology"
                and command.producer_agent == "system"
                and command.producer_skill == "published_asset_import"
                and command.assignment_id is None
            ):
                return
            if (
                command.producer_agent != "graph_management_agent"
                or command.producer_skill != published_skills[command.artifact_type]
                or command.assignment_id is None
            ):
                raise ArtifactValidationError(
                    "Published Artifact 只能由图谱管理 Agent 经 PublishGuard 授权的发布 Skill 创建"
                )

    async def _validate_protected_parents(
        self,
        command: ArtifactCreateCommand,
        parents: list[ArtifactRecord],
    ) -> None:
        """核验 Approval 目标快照及 Published Artifact 的候选/批准血缘。"""

        parent_by_id = {item.ref.artifact_id: item for item in parents}
        if command.producer_skill == "published_asset_import":
            if (
                command.artifact_type != "PublishedOntology"
                or len(parents) != 1
                or parents[0].ref.artifact_type != "PublishedOntology"
            ):
                raise ArtifactValidationError("已发布本体导入必须绑定唯一 PublishedOntology 来源")
            source_payload = json.loads(await self._storage.get_bytes(parents[0].object_key))
            if command.content != source_payload:
                raise ArtifactValidationError("导入本体内容必须与已发布来源完全一致")
            return
        if command.producer_skill == "published_graph_baseline_import":
            if (
                command.artifact_type != "CandidateGraph"
                or len(parents) != 1
                or parents[0].ref.artifact_type != "PublishedGraph"
            ):
                raise ArtifactValidationError("图谱增量基线必须绑定唯一 PublishedGraph 来源")
            source_payload = json.loads(await self._storage.get_bytes(parents[0].object_key))
            expected_candidate = {
                key: value
                for key, value in source_payload.items()
                if key not in {"graph_id", "graph_version", "approval_artifact_id"}
            }
            if command.content != expected_candidate:
                raise ArtifactValidationError("候选图谱基线必须原样继承已发布图谱事实")
            return
        if command.artifact_type in {"OntologyApproval", "GraphApproval"}:
            assert isinstance(command.content, dict)
            approval = ApprovalContent.model_validate(command.content)
            target = parent_by_id.get(approval.target_artifact_id)
            expected_target_types = {
                "OntologyApproval": {"CandidateOntology", "FusedCandidateOntology"},
                "GraphApproval": {"CandidateGraph"},
            }
            if (
                target is None
                or target.ref.artifact_type not in expected_target_types[command.artifact_type]
            ):
                raise ArtifactValidationError("Approval 父级中缺少对应候选 Artifact")
            if target.ref.checksum != approval.target_checksum:
                raise ArtifactValidationError("Approval 目标 checksum 与候选 Artifact 不一致")
            return

        if command.artifact_type not in {"PublishedOntology", "PublishedGraph"}:
            return
        assert isinstance(command.content, dict)
        if command.artifact_type == "PublishedOntology":
            ontology = PublishedOntologyContent.model_validate(command.content)
            approval_artifact_id = ontology.approval_artifact_id
            approval_type = "OntologyApproval"
            candidate_types = {"CandidateOntology", "FusedCandidateOntology"}
        else:
            graph = PublishedGraphContent.model_validate(command.content)
            approval_artifact_id = graph.approval_artifact_id
            approval_type = "GraphApproval"
            candidate_types = {"CandidateGraph"}

        approval_record = parent_by_id.get(approval_artifact_id)
        candidate_records = [item for item in parents if item.ref.artifact_type in candidate_types]
        if (
            approval_record is None
            or approval_record.ref.artifact_type != approval_type
            or len(candidate_records) != 1
        ):
            raise ArtifactValidationError(
                "Published Artifact 必须同时继承一个候选结果和对应 Approval"
            )
        raw_approval = await self._storage.get_bytes(approval_record.object_key)
        try:
            approval_payload = json.loads(raw_approval)
            approval = ApprovalContent.model_validate(approval_payload)
        except (json.JSONDecodeError, ValueError) as exc:
            raise ArtifactValidationError("Approval Artifact 内容无法验证") from exc
        candidate = candidate_records[0].ref
        if (
            approval.decision != "APPROVED"
            or approval.target_artifact_id != candidate.artifact_id
            or approval.target_checksum != candidate.checksum
        ):
            raise ArtifactValidationError("Approval 未绑定当前候选 Artifact 版本")

    async def _validate_claim(self, command: ArtifactCreateCommand, *, for_update: bool) -> None:
        """校验专业 Agent 写入时携带的 Assignment lease 和 fencing token。"""

        if command.assignment_id is None:
            if command.producer_agent not in {"user", "system"}:
                raise AssignmentConflictError("专业 Agent 写 Artifact 必须提供 Assignment 凭证")
            return
        assert command.worker_id is not None
        assert command.fencing_token is not None
        assignment = await self._assignments.get(command.assignment_id, for_update=for_update)
        if assignment is None:
            raise AssignmentConflictError("AgentAssignment 不存在")
        subtask = await self._tasks.get_subtask(assignment.subtask_id, for_update=for_update)
        if subtask is None or subtask.master_task_id != command.task_id:
            raise AssignmentConflictError("Assignment 不属于当前 Artifact 的 MasterTask")
        if subtask.status not in {SubTaskStatus.CLAIMED, SubTaskStatus.RUNNING}:
            raise AssignmentConflictError("子任务当前状态不允许写入 Artifact")
        self._ensure_current_assignment(assignment, command)

    def _ensure_current_assignment(
        self, assignment: AgentAssignment, command: ArtifactCreateCommand
    ) -> None:
        """拒绝过期 Worker、错误角色和旧 fencing token 的结果回写。"""

        if assignment.assigned_agent_type != command.producer_agent:
            raise AssignmentConflictError("Artifact 生产者与 Assignment 角色不一致")
        if assignment.claimed_by != command.worker_id:
            raise AssignmentConflictError("Artifact 写入 Worker 不是当前认领者")
        if assignment.fencing_token != command.fencing_token:
            raise AssignmentConflictError("Artifact 写入 fencing token 已失效")
        if assignment.status not in {AssignmentStatus.CLAIMED, AssignmentStatus.RUNNING}:
            raise AssignmentConflictError("Assignment 当前状态不允许写入 Artifact")
        expires_at = assignment.lease_expires_at
        if expires_at is None:
            raise AssignmentConflictError("Assignment 没有有效租约")
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        now = self._clock()
        if now.tzinfo is None:
            now = now.replace(tzinfo=UTC)
        if expires_at <= now:
            raise AssignmentConflictError("Assignment 租约已经过期")

    async def _validate_parents(
        self, command: ArtifactCreateCommand, parent_ids: list[UUID]
    ) -> list[ArtifactRecord]:
        """校验父级存在、同任务且仍可用，防止伪造跨任务血缘。"""

        if command.artifact_id in parent_ids:
            raise ArtifactValidationError("Artifact 不能把自身作为父级")
        parents: list[ArtifactRecord] = []
        for parent_id in parent_ids:
            parent = await self._artifacts.get(parent_id)
            if parent is None:
                raise ArtifactValidationError(f"父 Artifact 不存在：{parent_id}")
            cross_task_import = (
                command.producer_agent == "system"
                and command.assignment_id is None
                and (
                    (
                        command.producer_skill == "published_asset_import"
                        and command.artifact_type == "PublishedOntology"
                        and parent.ref.artifact_type == "PublishedOntology"
                    )
                    or (
                        command.producer_skill == "published_graph_baseline_import"
                        and command.artifact_type == "CandidateGraph"
                        and parent.ref.artifact_type == "PublishedGraph"
                    )
                )
            )
            if parent.ref.task_id != command.task_id and not cross_task_import:
                raise ArtifactValidationError("Artifact 血缘不能跨 MasterTask")
            if parent.ref.status is not ArtifactStatus.AVAILABLE:
                raise ArtifactValidationError("不能基于已失效或已替代 Artifact 创建新结果")
            parents.append(parent)
        return parents

    async def _validate_capacity(self, command: ArtifactCreateCommand, content_size: int) -> None:
        """校验单对象、RawDocument 数量和任务总容量限制。"""

        raw_document_count, _ = await self._artifacts.task_usage(command.task_id, "RawDocument")
        _, total_size = await self._artifacts.task_usage(command.task_id)
        if content_size > self._max_file_bytes:
            raise ArtifactValidationError("Artifact 内容超过单文件大小限制")
        if (
            command.artifact_type == "RawDocument"
            and raw_document_count >= self._max_files_per_task
        ):
            raise ArtifactValidationError("原始文档数量超过单任务限制")
        if total_size + content_size > self._max_total_bytes_per_task:
            raise ArtifactValidationError("Artifact 内容超过单任务总容量限制")

    @staticmethod
    def _ensure_idempotent(
        existing: ArtifactRecord,
        command: ArtifactCreateCommand,
        checksum: str,
    ) -> None:
        """确保相同 artifact_id 的重试与第一次写入完全一致。"""

        ref = existing.ref
        expected_parents = list(dict.fromkeys(command.parent_artifact_ids))
        if (
            command.supersedes_artifact_id
            and command.supersedes_artifact_id not in expected_parents
        ):
            expected_parents.append(command.supersedes_artifact_id)
        stable_values_match = (
            ref.artifact_type == command.artifact_type
            and ref.schema_version == command.schema_version
            and ref.task_id == command.task_id
            and ref.task_spec_revision == command.task_spec_revision
            and ref.producer_agent == command.producer_agent
            and ref.producer_skill == command.producer_skill
            and ref.producer_run_id == command.producer_run_id
            and ref.parent_artifact_ids == expected_parents
            and ref.ontology_id == command.ontology_id
            and ref.ontology_version == command.ontology_version
            and ref.publication_title == command.publication_title
            and ref.content_summary == command.content_summary
            and ref.checksum == checksum
            and existing.media_type == command.media_type
        )
        if not stable_values_match:
            raise PersistenceError("同一 artifact_id 不能保存不同 Artifact 内容或元数据")

    @staticmethod
    def _build_ref(
        *,
        command: ArtifactCreateCommand,
        parent_ids: list[UUID],
        content_uri: str,
        checksum: str,
        created_at: datetime,
    ) -> ArtifactRef:
        """从已验证命令构造 SharedContext 使用的轻量引用。"""

        return ArtifactRef(
            artifact_id=command.artifact_id,
            artifact_type=command.artifact_type,
            schema_version=command.schema_version,
            task_id=command.task_id,
            task_spec_revision=command.task_spec_revision,
            producer_agent=command.producer_agent,
            producer_skill=command.producer_skill,
            producer_run_id=command.producer_run_id,
            parent_artifact_ids=parent_ids,
            ontology_id=command.ontology_id,
            ontology_version=command.ontology_version,
            publication_title=command.publication_title,
            content_uri=content_uri,
            content_summary=command.content_summary,
            checksum=checksum,
            status=ArtifactStatus.AVAILABLE,
            created_at=created_at,
        )

    async def _create_context_snapshot(self, task_id: UUID, now: datetime) -> SharedTaskContext:
        """把最新 ArtifactRef 投影到一个新的不可变 SharedContext 版本。"""

        current = await self._contexts.get(task_id)
        next_version = 1 if current is None else current.version + 1
        context = await build_context_projection(
            task_id=task_id,
            version=next_version,
            updated_at=now,
            task_repository=self._tasks,
            assignment_repository=self._assignments,
            context_repository=self._contexts,
        )
        await self._contexts.add_snapshot(context)
        await self._events.append(
            task_id=task_id,
            event_type=TaskEventType.CONTEXT_VERSION_CREATED,
            actor_type="SERVICE",
            actor_id="artifact_service",
            occurred_at=now,
            payload={"version": next_version},
        )
        return context
