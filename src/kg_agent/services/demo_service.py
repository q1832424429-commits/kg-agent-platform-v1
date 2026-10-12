"""组合文件接入、总控路由、Worker 推进和结果观测的本地演示服务。

该服务用于可观察演示和隔离验证接口。它复用真实 Agent、正式状态服务和 LLM/确定性
业务 Skill，不实现生产级文档解析，也不替代生产环境独立 Worker 进程。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Literal, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from kg_agent.agents.controller.agent import ControllerAgent
from kg_agent.agents.controller.prompts import PromptRepository
from kg_agent.agents.controller.state import ControllerRequest, ControllerRunResult
from kg_agent.agents.professional.factory import build_professional_agent_pool
from kg_agent.agents.professional.prompts import ProfessionalPromptRepository
from kg_agent.domain.agent_models import AgentRunResult, TaskDecompositionResult
from kg_agent.domain.artifact_models import (
    ArtifactCreateCommand,
    ArtifactRecord,
    ArtifactRef,
    ArtifactReuseDecision,
    ArtifactReuseRequest,
)
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import ArtifactStatus, AssignmentStatus, MasterTaskStatus
from kg_agent.domain.memory_models import MemoryRef, MemoryWriteReceipt
from kg_agent.domain.task_models import AgentSubTask, TaskSpec
from kg_agent.exceptions import ArtifactValidationError, ContextConflictError, MemoryProviderError
from kg_agent.extensions.runtime_registry import build_composite_registries
from kg_agent.extensions.service import CustomExtensionService
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAgentRunRepository,
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.memory.memory_service import LongTermMemoryService
from kg_agent.memory.stores import DisabledLongTermMemoryStore
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.artifact_reuse_service import ArtifactReuseService
from kg_agent.services.artifact_service import ArtifactService
from kg_agent.services.completed_task_memory_service import CompletedTaskMemoryService
from kg_agent.services.context_service import ContextService
from kg_agent.services.postgres_artifact_reuse_service import PostgresArtifactReuseService
from kg_agent.services.postgres_artifact_service import PostgresArtifactService
from kg_agent.services.postgres_context_service import PostgresContextService
from kg_agent.services.postgres_human_review_service import PostgresHumanReviewService
from kg_agent.services.postgres_publish_guard import PostgresPublishGuard
from kg_agent.services.postgres_worker_services import PostgresWorkerServices
from kg_agent.services.run_log_service import RunLogService
from kg_agent.services.task_coordinator import TaskCoordinator
from kg_agent.services.task_state_service import TaskStateService
from kg_agent.workers.assignment_worker import AssignmentWorker

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class UploadedFile:
    """保存一次 API 请求中尚未落库的文件及其明确业务类型。

    ``artifact_type`` 由不同输入来源确定，不能根据文件名让模型自行猜测。普通资料
    使用 ``RawDocument``，专用本体窗口产生 ``ExternalOntology``。
    """

    filename: str
    media_type: str
    content: bytes
    artifact_type: Literal[
        "RawDocument",
        "ExternalOntology",
        "PublishedOntology",
        "CandidateGraph",
    ] = "RawDocument"
    source_artifact_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class StagedInputArtifact:
    """关联供总控查看的临时输入引用与最终 Artifact 创建命令。"""

    provisional_ref: ArtifactRef
    command: ArtifactCreateCommand


@dataclass(frozen=True, slots=True)
class DemoTaskSnapshot:
    """汇总一次演示任务中可展示的正式状态和业务产物。"""

    task_id: UUID
    conversation_id: UUID
    controller_result: ControllerRunResult
    subtasks: list[AgentSubTask]
    assignments: list[AgentAssignment]
    runs: list[AgentRunResult]
    artifacts: list[ArtifactRecord]
    # 进度展示必须读取该持久化终态，而不能仅依赖本次 HTTP 请求临时收集的协调器动作。
    master_task_status: MasterTaskStatus | None = None
    recalled_memories: list[MemoryRef] = field(default_factory=list)
    memory_write_receipt: MemoryWriteReceipt | None = None
    memory_enabled: bool = False
    memory_provider: str = "disabled"
    memory_app_id: str = ""
    memory_recall_status: Literal["SUCCEEDED", "FAILED", "DISABLED"] = "DISABLED"
    coordination_actions: list[str] = field(default_factory=list)


class RequestScopedArtifactReuseEvaluator:
    """同时校验本次待接入文件快照和数据库中的历史 Artifact。

    新建任务的上传文件会在总控计划通过后才与任务事实原子落库，因此规划期间数据库
    尚不可见。这里只对明确属于本次请求的不可变引用使用同一套快照兼容规则；其他 ID
    始终委托正式 PostgreSQL 校验，不能借此绕过历史 Artifact 门禁。
    """

    def __init__(
        self,
        request_artifacts: list[ArtifactRef],
        persisted_evaluator: PostgresArtifactReuseService,
    ) -> None:
        """绑定当前请求引用和正式持久化复用校验器。"""

        self._request_artifacts = {item.artifact_id: item for item in request_artifacts}
        self._persisted = persisted_evaluator

    async def evaluate(self, request: ArtifactReuseRequest) -> ArtifactReuseDecision:
        """当前请求引用使用快照规则；其余引用必须从数据库读取。"""

        ref = self._request_artifacts.get(request.artifact_id)
        if ref is None:
            return await self._persisted.evaluate(request)
        parents: list[ArtifactRef] = []
        for parent_id in ref.parent_artifact_ids:
            # 本轮上传的普通输入仍在请求快照内；而“已发布资产导入”的父级
            # 必然属于历史任务，必须回查 PostgreSQL，不能把合法跨任务导入
            # 误判成父级断链。找不到时保留为空，由统一复用规则给出拒绝原因。
            parent = self._request_artifacts.get(parent_id)
            if parent is None:
                parent = await self._persisted.get_ref(parent_id)
            if parent is not None:
                parents.append(parent)
        # 受控已发布资产导入的临时副本尚未进入当前任务数据库；调用方的模型可见
        # Artifact 集合不会包含其跨任务来源，因此不能从那个不完整视图计算期望
        # checksum。这里使用刚刚从权威 PostgreSQL 解析出的同一父级快照补齐期望值。
        # 普通历史 Artifact 仍沿用调用方提供的 checksum 要求，不放宽复用门禁。
        controlled_import = ref.producer_skill in {
            "published_asset_import",
            "published_graph_baseline_import",
        }
        if controlled_import and parents and not request.expected_parent_checksums:
            request = request.model_copy(
                update={"expected_parent_checksums": {parent.checksum for parent in parents}}
            )
        return ArtifactReuseService.evaluate_snapshot(request, ref, parents)


class NewTaskIntakeContextService:
    """在总控确认计划后，把新任务、Assignment 和上传文件一起落库。

    临时 ArtifactRef 只用于总控的结构化校验，不写入数据库。正式发布时先由
    TaskStateService 创建任务事实，再由 ArtifactService 在同一事务保存类型化输入。
    """

    def __init__(
        self,
        *,
        database: Database,
        storage: ObjectStorage,
        registry: ArtifactRegistry,
        staged_inputs: list[StagedInputArtifact],
        max_file_bytes: int,
        max_files_per_task: int,
        max_total_bytes_per_task: int,
    ) -> None:
        """绑定数据库、对象存储、Registry、待接入文件和容量限制。"""

        self._database = database
        self._delegate = PostgresContextService(database.session_factory)
        self._storage = storage
        self._registry = registry
        self._staged_inputs = staged_inputs
        self._max_file_bytes = max_file_bytes
        self._max_files_per_task = max_files_per_task
        self._max_total_bytes_per_task = max_total_bytes_per_task

    async def get(self, task_id: UUID) -> SharedTaskContext | None:
        """读取既有任务；新建任务正常返回 None。"""

        return await self._delegate.get(task_id)

    async def list_subtasks(self, task_id: UUID) -> list[AgentSubTask]:
        """委托正式上下文服务读取历史子任务。"""

        return await self._delegate.list_subtasks(task_id)

    async def get_task_spec(self, task_id: UUID, revision: int) -> TaskSpec | None:
        """委托正式上下文服务读取历史 TaskSpec。"""

        return await self._delegate.get_task_spec(task_id, revision)

    async def publish_decomposition(
        self,
        *,
        conversation_id: UUID,
        result: TaskDecompositionResult,
        source_artifacts: Sequence[ArtifactRef],
        expected_version: int | None,
        recalled_long_term_memories: Sequence[MemoryRef] = (),
    ) -> SharedTaskContext:
        """原子发布新任务并把临时文件引用转成正式输入 Artifact。"""

        expected_ids = {item.provisional_ref.artifact_id for item in self._staged_inputs}
        supplied_ids = {item.artifact_id for item in source_artifacts}
        if expected_version is not None or supplied_ids != expected_ids:
            raise ContextConflictError("新任务文件接入上下文与总控请求不一致")

        artifact_service: ArtifactService | None = None
        try:
            async with self._database.session_factory() as session:
                async with session.begin():
                    tasks = SqlAlchemyTaskRepository(session)
                    assignments = SqlAlchemyAssignmentRepository(session)
                    contexts = SqlAlchemyContextRepository(session)
                    events = SqlAlchemyEventRepository(session)
                    await TaskStateService(
                        task_repository=tasks,
                        assignment_repository=assignments,
                        context_repository=contexts,
                        event_repository=events,
                    ).publish_decomposition(
                        conversation_id=conversation_id,
                        result=result,
                        source_artifacts=[],
                        recalled_long_term_memories=recalled_long_term_memories,
                        expected_version=None,
                    )
                    artifact_service = ArtifactService(
                        artifact_repository=SqlAlchemyArtifactRepository(session),
                        task_repository=tasks,
                        assignment_repository=assignments,
                        context_repository=contexts,
                        event_repository=events,
                        storage=self._storage,
                        registry=self._registry,
                        max_file_bytes=self._max_file_bytes,
                        max_files_per_task=self._max_files_per_task,
                        max_total_bytes_per_task=self._max_total_bytes_per_task,
                    )
                    for staged_input in self._staged_inputs:
                        await artifact_service.create(staged_input.command)
                    context = await contexts.get(result.task_spec.task_id)
                    if context is None:
                        raise ContextConflictError("文件接入后未生成 SharedContext")
                    return context
        except Exception:
            if artifact_service is not None:
                for object_key in artifact_service.pending_object_keys:
                    await self._storage.delete(object_key)
            raise


class DemoOrchestrationService:
    """执行“输入 → 路由 → Worker → 所选模式 Skill → 观测”的演示闭环。"""

    def __init__(
        self,
        *,
        database: Database,
        storage: ObjectStorage,
        model_gateway: ModelGateway,
        project_root: Path,
        max_repairs: int = 2,
        lease_seconds: int = 60,
        heartbeat_seconds: int = 20,
        max_file_bytes: int = 50 * 1024 * 1024,
        max_files_per_task: int = 20,
        max_total_bytes_per_task: int = 500 * 1024 * 1024,
        memory_service: LongTermMemoryService | None = None,
        auto_remember_long_term: bool = False,
        run_log_service: RunLogService | None = None,
        custom_extensions: CustomExtensionService | None = None,
    ) -> None:
        """组装数据库、Agent、Skill Registry、Worker 和容量配置。"""

        self._database = database
        self._storage = storage
        self._model = model_gateway
        self._project_root = project_root
        self._max_repairs = max_repairs
        self._lease_seconds = lease_seconds
        self._heartbeat_seconds = heartbeat_seconds
        self._max_file_bytes = max_file_bytes
        self._max_files_per_task = max_files_per_task
        self._max_total_bytes_per_task = max_total_bytes_per_task
        self._memory = memory_service or LongTermMemoryService(
            DisabledLongTermMemoryStore(), user_id="kg-agent-v1-single-user"
        )
        self._auto_remember_long_term = auto_remember_long_term
        self._run_logs = run_log_service or RunLogService(None)
        self._completed_task_memory = CompletedTaskMemoryService(
            self._database.session_factory,
            memory=self._memory,
            run_logs=self._run_logs,
            auto_authorized=auto_remember_long_term,
        )
        self._custom_extensions = custom_extensions
        self._artifact_registry = ArtifactRegistry.v1()
        self._agent_registry = AgentRegistry.from_directory(project_root / "configs" / "agents")
        self._coordinator_artifacts = self._artifact_writer()
        self._skill_registry = SkillRegistry.from_directory(
            project_root / "src" / "kg_agent" / "skills" / "catalog",
            artifact_registry=self._artifact_registry,
            model_gateway=self._model,
            artifact_reader=self._coordinator_artifacts,
            # 测试端必须复用正式端同一审核门禁。否则审核虽然产生 Approval，发布
            # Skill 仍会因 Registry 未装配副作用授权器而失败，整条下游链被取消。
            side_effect_authorizer=PostgresPublishGuard(
                self._database.session_factory,
                agents=self._agent_registry,
                storage=self._storage,
            ),
        )
        # 内置能力是不可变基线。每次用户提交前都会据 PostgreSQL 当前已发布的扩展定义
        # 创建新的运行期快照，避免历史草稿或被停用版本污染之后的任务发现。
        self._base_agent_registry = self._agent_registry
        self._base_skill_registry = self._skill_registry
        self._reviews = PostgresHumanReviewService(
            self._database.session_factory,
            storage=self._storage,
            registry=self._artifact_registry,
            max_file_bytes=self._max_file_bytes,
            max_files_per_task=self._max_files_per_task,
            max_total_bytes_per_task=self._max_total_bytes_per_task,
        )

    @property
    def memory_service(self) -> LongTermMemoryService:
        """返回当前演示服务绑定的长期记忆服务，供隔离观察端读取状态。"""

        return self._memory

    async def remember_completed_task_experience(self, task_id: UUID) -> MemoryWriteReceipt | None:
        """在 MasterTask 成功收口后触发唯一一次整轮经验沉淀。

        返回值只供兼容现有快照展示；写入的完整动作、相似度和供应商状态会进入
        任务 JSONL。Mem0 失败由聚合服务隔离，不改变任务成功状态。
        """

        result = await self._completed_task_memory.remember(task_id)
        return result.receipt if result is not None else None

    @property
    def database(self) -> Database:
        """暴露协调器只读写任务事实所需的数据库边界。"""

        return self._database

    @property
    def storage(self) -> ObjectStorage:
        """暴露待审核计划暂存真实上传内容所需的对象存储边界。"""

        return self._storage

    @property
    def orchestration(self) -> DemoOrchestrationService:
        """让确定性协调器复用同一个总控 Agent 编排入口。"""

        return self

    @property
    def artifacts(self) -> PostgresArtifactService:
        """暴露协调器读取审核报告内容所需的 Artifact 服务。"""

        return self._coordinator_artifacts

    @property
    def reviews(self) -> PostgresHumanReviewService:
        """暴露测试环境隔离的人工审核服务。"""

        return self._reviews

    @property
    def run_logs(self) -> RunLogService:
        """返回只记录公开结构化结论的任务文件日志服务。"""

        return self._run_logs

    async def _refresh_custom_registries(self) -> None:
        """将 PostgreSQL 中已发布的自定义定义合并为本轮运行期 Registry 快照。

        草稿、历史修订和停用定义永远不会进入该快照。反向 Skill 授权列表仅临时由
        Agent 的 permissions.yaml 推导，数据库中不会写回第二份 Skill allowlist。
        """

        if self._custom_extensions is None:
            return
        combined_agents, combined_skills = await build_composite_registries(
            builtin_agents=self._base_agent_registry,
            builtin_skills=self._base_skill_registry,
            extensions=self._custom_extensions,
            model_gateway=self._model,
            artifact_reader=self._coordinator_artifacts,
            artifact_registry=self._artifact_registry,
        )
        self._agent_registry = combined_agents
        self._skill_registry = combined_skills

    async def task_exists(self, task_id: UUID | None) -> bool:
        """判断测试端是否确有对应 SharedContext，避免仅凭传入 ID 误判为续接任务。"""

        if task_id is None:
            return False
        return await PostgresContextService(self._database.session_factory).get(task_id) is not None

    async def snapshot_task(self, task_id: UUID) -> DemoTaskSnapshot:
        """恢复一个已持久化任务的公开快照，不重新调用总控或执行 Worker。

        页面刷新只能读取已落库的任务、产物、审核和运行事实；该方法不会制造新的
        Artifact，也不会改变 Assignment 或审核状态。
        """

        context = await PostgresContextService(self._database.session_factory).get(task_id)
        if context is None:
            raise ContextConflictError("任务不存在，不能恢复快照")
        async with self._database.session_factory() as session:
            subtasks = await SqlAlchemyTaskRepository(session).list_subtasks(task_id)
        revision = max(
            (item.decomposition_revision for item in subtasks),
            default=context.master_task.current_decomposition_revision,
        )
        decomposition = TaskDecompositionResult(
            task_intent=context.current_task_spec.task_intent,
            task_spec=context.current_task_spec,
            subtasks=[item for item in subtasks if item.decomposition_revision == revision],
        )
        return await self._snapshot(
            task_id,
            context.conversation.conversation_id,
            ControllerRunResult(decomposition=decomposition, shared_context=context),
        )

    async def publish_planned_decomposition(
        self,
        *,
        decomposition: TaskDecompositionResult,
        conversation_id: UUID,
        request_id: UUID,
        files: list[UploadedFile],
    ) -> DemoTaskSnapshot:
        """在用户批准候选计划后，原子发布原计划而不再次调用模型。

        新任务会用原 request_id 重建完全相同的临时 Artifact ID，并把暂存文件与
        MasterTask、TaskSpec、AgentSubTask 和 Assignment 一起落库。既有任务的新增
        文件已在规划阶段作为输入 Artifact 落库，因此发布时只读取当前上下文引用。
        """

        task_id = decomposition.task_spec.task_id
        current = await PostgresContextService(self._database.session_factory).get(task_id)
        if current is None:
            staged = self._stage_inputs(
                task_id=task_id,
                request_id=request_id,
                task_spec_revision=decomposition.task_spec.revision,
                files=files,
            )
            context_service: ContextService = NewTaskIntakeContextService(
                database=self._database,
                storage=self._storage,
                registry=self._artifact_registry,
                staged_inputs=staged,
                max_file_bytes=self._max_file_bytes,
                max_files_per_task=self._max_files_per_task,
                max_total_bytes_per_task=self._max_total_bytes_per_task,
            )
            source_artifacts = [item.provisional_ref for item in staged]
            expected_version = None
        else:
            context_service = PostgresContextService(self._database.session_factory)
            source_artifacts = list(current.artifact_refs)
            expected_version = current.version
        published = await context_service.publish_decomposition(
            conversation_id=conversation_id,
            result=decomposition,
            source_artifacts=source_artifacts,
            expected_version=expected_version,
        )
        result = ControllerRunResult(
            decomposition=decomposition,
            shared_context=published,
        )
        return await self._snapshot(task_id, conversation_id, result)

    async def record_approved_plan_message(
        self,
        *,
        task_id: UUID,
        conversation_id: UUID,
        message: str,
        decomposition: TaskDecompositionResult | None = None,
    ) -> None:
        """记录获批计划对应用户消息；基础演示服务不额外持久化对话正文。"""

    async def submit_and_run(
        self,
        *,
        question: str,
        files: list[UploadedFile],
        task_id: UUID | None = None,
        conversation_id: UUID | None = None,
        request_id: UUID | None = None,
        execute_workers: bool = True,
        max_worker_runs: int = 10,
        conversation_history: list[dict[str, object]] | None = None,
        internal_continuation: bool = False,
        controller_operation: Literal[
            "USER_TURN",
            "SYSTEM_CONTINUATION",
            "FINAL_VERIFICATION",
        ]
        | None = None,
        publish_decomposition: bool = True,
    ) -> DemoTaskSnapshot:
        """提交用户问题和文件，可选推进 Worker，并返回完整可观察快照。

        ``internal_continuation`` 只供 TaskCoordinator 使用。该轮仍调用真实总控 Agent，
        但不把系统生成的推进指令当成用户长期记忆写入 Mem0。
        """

        orchestration_started = perf_counter()
        await self._refresh_custom_registries()
        if not question.strip():
            raise ValueError("用户问题不能为空")
        if max_worker_runs < 0 or max_worker_runs > 100:
            raise ValueError("max_worker_runs 必须在 0 到 100 之间")

        resolved_task_id = task_id or uuid4()
        resolved_request_id = request_id or uuid4()
        current = await PostgresContextService(self._database.session_factory).get(resolved_task_id)
        resolved_conversation_id = (
            current.conversation.conversation_id
            if current is not None
            else conversation_id or uuid4()
        )
        if (
            current is not None
            and conversation_id is not None
            and conversation_id != resolved_conversation_id
        ):
            raise ContextConflictError("conversation_id 与现有任务不一致")

        # 在可能耗时的 Mem0 与模型调用前先写入公开开始事件，让 SSE 客户端不会在
        # 首个结果产生前长期停留在无反馈状态。这里只记录阶段事实，不包含隐藏推理。
        await self._run_logs.append(
            task_id=resolved_task_id,
            record_type="ORCHESTRATION_STARTED",
            actor="task_orchestration_service",
            payload={
                "request_id": str(resolved_request_id),
                "internal_continuation": internal_continuation,
            },
        )

        staged = self._stage_inputs(
            task_id=resolved_task_id,
            request_id=resolved_request_id,
            task_spec_revision=(current.current_task_spec.revision if current else 1),
            files=files,
        )
        # 长期记忆只以轻量摘要进入模型上下文；Mem0 故障不会阻断本轮知识构建。
        recalled_memories: list[MemoryRef] = []
        memory_recall_status: Literal["SUCCEEDED", "FAILED", "DISABLED"] = "DISABLED"
        memory_recall_started = perf_counter()
        if self._memory.enabled and not internal_continuation:
            try:
                recalled_memories = await self._memory.recall_refs(question)
                memory_recall_status = "SUCCEEDED"
            except MemoryProviderError:
                # 测试端必须把云端失败呈现给用户，但仍按无记忆继续完成 Agent 观察。
                memory_recall_status = "FAILED"
                LOGGER.warning("测试端 Mem0 长期记忆召回失败", exc_info=True)
        await self._run_logs.append(
            task_id=resolved_task_id,
            record_type="MEMORY_RECALL",
            actor="memory_service",
            payload={
                "request_id": str(resolved_request_id),
                "internal_continuation": internal_continuation,
                "status": memory_recall_status,
                "recalled_count": len(recalled_memories),
                # 这里只记录已经通过阈值、重排、失效过滤和去重并实际提供给
                # Agent 的最终摘要；不记录候选池、Mem0 原始响应或供应商凭据。
                "memories": [
                    {
                        "rank": index,
                        "memory_id": str(item.memory_id),
                        "summary": item.summary[:500],
                        "score": next(
                            (
                                ref.removeprefix("score:")
                                for ref in item.source_refs
                                if ref.startswith("score:")
                            ),
                            None,
                        ),
                    }
                    for index, item in enumerate(recalled_memories, start=1)
                ],
                "duration_ms": round((perf_counter() - memory_recall_started) * 1000, 3),
            },
        )
        if current is None:
            context_service: ContextService = NewTaskIntakeContextService(
                database=self._database,
                storage=self._storage,
                registry=self._artifact_registry,
                staged_inputs=staged,
                max_file_bytes=self._max_file_bytes,
                max_files_per_task=self._max_files_per_task,
                max_total_bytes_per_task=self._max_total_bytes_per_task,
            )
            available = [item.provisional_ref for item in staged]
        else:
            artifact_writer = self._artifact_writer()
            uploaded = [(await artifact_writer.create(item.command)).ref for item in staged]
            # 总控必须同时看到 SharedContext 中的历史产物和本轮新增产物，才能在
            # CONTINUE/MODIFY 时安全复用已完成节点，而不会错误地要求重新执行。
            available = [*current.artifact_refs, *uploaded]
            context_service = PostgresContextService(self._database.session_factory)

        persisted_reuse_evaluator = PostgresArtifactReuseService(self._database.session_factory)
        reuse_evaluator = (
            RequestScopedArtifactReuseEvaluator(available, persisted_reuse_evaluator)
            if current is None
            else persisted_reuse_evaluator
        )
        controller = ControllerAgent(
            model_gateway=self._model,
            agent_registry=self._agent_registry,
            context_service=context_service,
            prompt_repository=PromptRepository(
                self._project_root / "configs" / "prompts" / "controller"
            ),
            max_repairs=self._max_repairs,
            artifact_registry=self._artifact_registry,
            skill_registry=self._skill_registry,
            artifact_reuse_evaluator=reuse_evaluator,
        )
        await self._run_logs.append(
            task_id=resolved_task_id,
            record_type="CONTROLLER_STARTED",
            actor="controller_agent",
            payload={
                "request_id": str(resolved_request_id),
                "internal_continuation": internal_continuation,
            },
        )
        controller_started = perf_counter()
        try:
            controller_result = await controller.run(
                ControllerRequest(
                    task_id=resolved_task_id,
                    conversation_id=resolved_conversation_id,
                    user_message=question,
                    conversation_history=conversation_history or [],
                    current_task_id=resolved_task_id if current else None,
                    existing_artifacts=available,
                    recalled_long_term_memories=recalled_memories,
                    operation=(
                        controller_operation
                        or ("SYSTEM_CONTINUATION" if internal_continuation else "USER_TURN")
                    ),
                    publish_decomposition=publish_decomposition,
                )
            )
        except Exception as exc:
            # 无论初次规划还是最终目标检查失败，都必须先形成持久化公开日志。此前异常
            # 直接向上冒泡，页面只能看到 409，无法判断模型网络、Schema 还是计划校验错误。
            controller_duration_ms = round((perf_counter() - controller_started) * 1000, 3)
            await self._run_logs.append(
                task_id=resolved_task_id,
                record_type="CONTROLLER_FAILED",
                actor="controller_agent",
                payload={
                    "request_id": str(resolved_request_id),
                    "internal_continuation": internal_continuation,
                    "duration_ms": controller_duration_ms,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:2000],
                },
            )
            LOGGER.warning(
                "任务 %s 的总控 Agent 执行失败（internal_continuation=%s）",
                resolved_task_id,
                internal_continuation,
                exc_info=True,
            )
            raise
        controller_duration_ms = round((perf_counter() - controller_started) * 1000, 3)
        await self._run_logs.append(
            task_id=resolved_task_id,
            record_type="CONTROLLER_RUN",
            actor="controller_agent",
            payload={
                "request_id": str(resolved_request_id),
                "internal_continuation": internal_continuation,
                "duration_ms": controller_duration_ms,
                "task_intent": controller_result.decomposition.task_intent.value,
                "requires_clarification": (controller_result.decomposition.requires_clarification),
                "task_spec": controller_result.decomposition.task_spec.model_dump(mode="json"),
                "subtasks": [
                    item.model_dump(mode="json")
                    for item in controller_result.decomposition.subtasks
                ],
            },
        )
        coordination_actions: list[str] = []
        if execute_workers and controller_result.shared_context is not None:
            coordination_actions = await self._run_workers(
                task_id=resolved_task_id,
                max_worker_runs=max_worker_runs,
            )
        snapshot = await self._snapshot(
            resolved_task_id,
            resolved_conversation_id,
            controller_result,
        )
        if not publish_decomposition:
            # 候选计划尚未写入 Repository；测试审核页仍需展示经过完整校验的草稿。
            # Assignment 和运行结果保持为空/历史值，明确区分“候选”与“已发布”。
            snapshot = replace(snapshot, subtasks=controller_result.decomposition.subtasks)
        response = replace(
            snapshot,
            recalled_memories=recalled_memories,
            # 自动长期记忆已经移到 MasterTask 成功收口点；提交计划时不再写入
            # 未完成经验，也不把单个 Agent 运行片段误认为本轮最终结果。
            memory_write_receipt=None,
            memory_enabled=self._memory.enabled,
            memory_provider=self._memory.provider_name,
            memory_app_id=self._memory.app_id,
            memory_recall_status=memory_recall_status,
            coordination_actions=coordination_actions,
        )
        await self._run_logs.append(
            task_id=resolved_task_id,
            record_type="ORCHESTRATION_RUN",
            actor="task_orchestration_service",
            payload={
                "request_id": str(resolved_request_id),
                "internal_continuation": internal_continuation,
                "duration_ms": round((perf_counter() - orchestration_started) * 1000, 3),
            },
        )
        return response

    def _stage_inputs(
        self,
        *,
        task_id: UUID,
        request_id: UUID,
        task_spec_revision: int,
        files: list[UploadedFile],
    ) -> list[StagedInputArtifact]:
        """为总控构造临时引用，并准备可幂等落库的输入 Artifact 命令。

        外部本体在进入总控前必须通过 UTF-8、JSON 对象和
        ``ExternalOntologyContent`` 三层校验，确保总控看到的引用一定可以正式落库。
        """

        if sum(item.artifact_type == "RawDocument" for item in files) > self._max_files_per_task:
            raise ArtifactValidationError("单次上传文件数量超过任务限制")
        now = datetime.now(UTC)
        staged: list[StagedInputArtifact] = []
        for index, item in enumerate(files):
            if not item.filename.strip():
                raise ArtifactValidationError("上传文件名不能为空")
            content: bytes | dict[str, object]
            media_type = item.media_type
            producer_skill = "file_upload"
            content_summary = f"用户上传文件：{item.filename}"
            ontology_id: UUID | None = None
            ontology_version: str | None = None
            if item.artifact_type in {"ExternalOntology", "PublishedOntology", "CandidateGraph"}:
                if item.artifact_type == "CandidateGraph" and item.source_artifact_id is None:
                    raise ArtifactValidationError("图谱基线导入缺少来源 PublishedGraph")
                if Path(item.filename).suffix.lower() != ".json":
                    raise ArtifactValidationError("本体或图谱资产只支持 .json 内容")
                try:
                    decoded = item.content.decode("utf-8-sig")
                except UnicodeDecodeError as exc:
                    raise ArtifactValidationError("本体或图谱资产必须是 UTF-8 JSON") from exc
                try:
                    parsed = json.loads(decoded)
                except json.JSONDecodeError as exc:
                    raise ArtifactValidationError("本体或图谱资产不是合法 JSON") from exc
                if not isinstance(parsed, dict):
                    raise ArtifactValidationError("本体或图谱资产 JSON 顶层必须是对象")
                content = cast(dict[str, object], parsed)
                media_type = "application/json"
                if item.artifact_type == "PublishedOntology":
                    if item.source_artifact_id is None:
                        raise ArtifactValidationError("已发布本体导入缺少来源 Artifact")
                    producer_skill = "published_asset_import"
                    content_summary = f"复用已发布本体：{parsed.get('name', item.filename)}"
                    ontology_id = UUID(str(parsed["ontology_id"]))
                    ontology_version = str(parsed["ontology_version"])
                elif item.artifact_type == "CandidateGraph":
                    producer_skill = "published_graph_baseline_import"
                    content_summary = "从已发布图谱创建的增量候选基线"
                else:
                    producer_skill = "ontology_upload"
                    content_summary = (
                        f"用户上传外部本体：{parsed.get('name', item.filename)}"
                        f"（版本 {parsed.get('version', '未标注')}）"
                    )
            else:
                content = item.content
            artifact_id = uuid5(
                NAMESPACE_URL,
                f"kg-agent:upload:{request_id}:{index}:{item.artifact_type}:{item.filename}",
            )
            # JSON Artifact 的 checksum 必须基于 Registry 规范化后的字节，而不是上传原文。
            normalized = self._artifact_registry.serialize_and_validate(
                artifact_type=item.artifact_type,
                schema_version="1.0.0",
                content=content,
            )
            checksum = f"sha256:{hashlib.sha256(normalized).hexdigest()}"
            producer_run_id = uuid5(
                NAMESPACE_URL,
                f"kg-agent:upload-run:{request_id}",
            )
            command = ArtifactCreateCommand(
                artifact_id=artifact_id,
                artifact_type=item.artifact_type,
                task_id=task_id,
                task_spec_revision=task_spec_revision,
                producer_agent=(
                    "system"
                    if item.artifact_type in {"PublishedOntology", "CandidateGraph"}
                    else "user"
                ),
                producer_skill=producer_skill,
                producer_run_id=producer_run_id,
                parent_artifact_ids=(
                    [item.source_artifact_id] if item.source_artifact_id is not None else []
                ),
                content_summary=content_summary,
                media_type=media_type,
                content=content,
                ontology_id=ontology_id,
                ontology_version=ontology_version,
            )
            staged.append(
                StagedInputArtifact(
                    provisional_ref=ArtifactRef(
                        artifact_id=artifact_id,
                        artifact_type=item.artifact_type,
                        schema_version="1.0.0",
                        task_id=task_id,
                        task_spec_revision=task_spec_revision,
                        producer_agent=(
                            "system"
                            if item.artifact_type in {"PublishedOntology", "CandidateGraph"}
                            else "user"
                        ),
                        producer_skill=producer_skill,
                        producer_run_id=producer_run_id,
                        parent_artifact_ids=(
                            [item.source_artifact_id] if item.source_artifact_id is not None else []
                        ),
                        content_uri=f"staged://{artifact_id}",
                        content_summary=command.content_summary,
                        checksum=checksum,
                        ontology_id=ontology_id,
                        ontology_version=ontology_version,
                        status=ArtifactStatus.AVAILABLE,
                        created_at=now,
                    ),
                    command=command,
                )
            )
        return staged

    def _artifact_writer(self) -> PostgresArtifactService:
        """创建供用户上传和专业 Agent 共用的正式 Artifact 应用服务。"""

        return PostgresArtifactService(
            self._database.session_factory,
            storage=self._storage,
            registry=self._artifact_registry,
            max_file_bytes=self._max_file_bytes,
            max_files_per_task=self._max_files_per_task,
            max_total_bytes_per_task=self._max_total_bytes_per_task,
        )

    async def _run_workers(
        self,
        *,
        task_id: UUID,
        max_worker_runs: int,
    ) -> list[str]:
        """按依赖并行执行完整 Agent 计划，直到完成、等待或达到 Worker 安全上限。

        每批并行任务结束后调用与正式 Worker 相同的 TaskCoordinator。协调器只检查
        依赖、审核和终态，全部计划终止前不重复调用总控 Agent。
        """

        if max_worker_runs == 0:
            return []
        artifact_writer = self._artifact_writer()
        pool = build_professional_agent_pool(
            model_gateway=self._model,
            agent_registry=self._agent_registry,
            skill_registry=self._skill_registry,
            artifact_registry=self._artifact_registry,
            artifact_writer=artifact_writer,
            prompt_repository=ProfessionalPromptRepository(
                self._project_root / "configs" / "prompts" / "professional"
            ),
            max_repairs=self._max_repairs,
        )
        services = PostgresWorkerServices(
            self._database.session_factory,
            agents=self._agent_registry,
            skills=self._skill_registry,
            lease_seconds=self._lease_seconds,
            memory_service=self._memory,
        )
        # 测试端的同步 HTTP 请求可能被浏览器中断；进程重启后先回收已经过期的租约，
        # 否则协调器只能看见 RUNNING Assignment，而新 Worker 永远无法重新认领。
        recovered = await services.recover_expired()
        if recovered:
            await self._run_logs.append(
                task_id=task_id,
                record_type="ASSIGNMENT_RECOVERED",
                actor="assignment_service",
                payload={
                    "recovered_count": len(recovered),
                    "assignment_ids": [str(item.assignment_id) for item in recovered],
                },
            )
        completed = 0
        actions: list[str] = []
        coordinator = TaskCoordinator(self)  # type: ignore[arg-type]
        while completed < max_worker_runs:
            async with self._database.session_factory() as session:
                current_assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(
                    task_id
                )
                current_subtasks = {
                    item.subtask_id: item
                    for item in await SqlAlchemyTaskRepository(session).list_subtasks(task_id)
                }
            pending_assignments = [
                item for item in current_assignments if item.status is AssignmentStatus.PENDING
            ]

            # Worker 上限限制的是本轮实际执行次数，而不是 Registry 中 Agent 类型的
            # 前 N 项。必须从当前 PENDING Assignment 反推需要轮询的角色；否则上限
            # 较小时会固定只轮询数据源解析 Agent，并把已经解锁的本体/图谱发布任务
            # 永久留在 WAITING_ASSIGNMENTS。
            def pending_order(
                item: AgentAssignment,
                subtasks: dict[UUID, AgentSubTask] = current_subtasks,
            ) -> tuple[int, datetime, str]:
                """按子任务优先级和 Assignment 创建顺序生成稳定排序键。"""

                subtask = subtasks.get(item.subtask_id)
                return (
                    -(subtask.priority if subtask is not None else 0),
                    item.created_at,
                    str(item.assignment_id),
                )

            ordered_pending = sorted(
                pending_assignments,
                key=pending_order,
            )
            ready_agent_types: list[str] = []
            for assignment in ordered_pending:
                if assignment.assigned_agent_type not in ready_agent_types:
                    ready_agent_types.append(assignment.assigned_agent_type)
            for assignment in pending_assignments:
                subtask = current_subtasks.get(assignment.subtask_id)
                await self._run_logs.append(
                    task_id=task_id,
                    record_type="ASSIGNMENT_READY",
                    actor=assignment.assigned_agent_type,
                    payload={
                        "assignment_id": str(assignment.assignment_id),
                        "subtask_id": str(assignment.subtask_id),
                        "subtask_name": subtask.name if subtask is not None else "未知子任务",
                        "subtask_type": (
                            subtask.subtask_type if subtask is not None else "UNKNOWN"
                        ),
                        "expected_output_types": (
                            subtask.expected_output_types if subtask is not None else []
                        ),
                    },
                )

            async def run_agent(
                agent_type: str,
                pending_assignments_for_batch: list[AgentAssignment] = pending_assignments,
                subtasks_for_batch: dict[UUID, AgentSubTask] = current_subtasks,
            ) -> tuple[AgentRunResult | None, float]:
                """并行尝试认领一个角色任务，并返回包含模型调用的墙钟耗时。"""

                candidates = [
                    item
                    for item in pending_assignments_for_batch
                    if item.assigned_agent_type == agent_type
                ]
                if candidates:

                    def candidate_order(item: AgentAssignment) -> tuple[int, float]:
                        """复用当前批次快照选择该角色最应先认领的 Assignment。"""

                        target = subtasks_for_batch.get(item.subtask_id)
                        return (
                            target.priority if target is not None else 0,
                            -item.created_at.timestamp(),
                        )

                    candidate = max(
                        candidates,
                        key=candidate_order,
                    )
                    subtask = subtasks_for_batch.get(candidate.subtask_id)
                    await self._run_logs.append(
                        task_id=task_id,
                        record_type="AGENT_RUN_STARTED",
                        actor=agent_type,
                        payload={
                            "assignment_id": str(candidate.assignment_id),
                            "subtask_id": str(candidate.subtask_id),
                            "subtask_name": (subtask.name if subtask is not None else "未知子任务"),
                            "subtask_type": (
                                subtask.subtask_type if subtask is not None else "UNKNOWN"
                            ),
                        },
                    )
                worker_started = perf_counter()
                result = await AssignmentWorker(
                    worker_id=f"demo-worker-{agent_type}",
                    services=services,
                    agents=pool,
                    heartbeat_seconds=self._heartbeat_seconds,
                ).run_once(agent_type, task_id=task_id)
                return result, round((perf_counter() - worker_started) * 1000, 3)

            remaining = max_worker_runs - completed
            batch = await asyncio.gather(
                *(run_agent(agent_type) for agent_type in ready_agent_types[:remaining])
            )
            progressed = False
            for result, worker_duration_ms in batch:
                if result is not None:
                    completed += 1
                    progressed = True
                    payload = result.model_dump(mode="json")
                    payload["worker_total_duration_ms"] = worker_duration_ms
                    await self._run_logs.append(
                        task_id=task_id,
                        record_type="AGENT_RUN",
                        actor=result.agent_name,
                        payload=payload,
                    )
            await self._run_logs.append(
                task_id=task_id,
                record_type="COORDINATION_STARTED",
                actor="task_coordinator",
                payload={"completed_worker_runs": completed},
            )
            coordinator_started = perf_counter()
            action = await coordinator.advance(task_id)
            coordinator_duration_ms = round((perf_counter() - coordinator_started) * 1000, 3)
            actions.append(action)
            await self._run_logs.append(
                task_id=task_id,
                record_type="COORDINATION",
                actor="task_coordinator",
                payload={
                    "action": action,
                    "completed_worker_runs": completed,
                    "duration_ms": coordinator_duration_ms,
                },
            )
            if (
                action
                in {
                    "CONTROLLER_REPLANNED",
                    "WAITING_ASSIGNMENTS",
                    "WAITING_DEPENDENCIES",
                }
                and progressed
            ):
                continue
            if not progressed or action in {
                "TASK_COMPLETED",
                "WAITING_HUMAN",
                "REVIEW_CREATED",
                "WAITING_USER",
                "FINAL_CHECK_FAILED",
                "REVISION_LIMIT_REACHED",
                "NOOP",
            }:
                if not progressed:
                    async with self._database.session_factory() as session:
                        waiting = await SqlAlchemyAssignmentRepository(session).list_for_task(
                            task_id
                        )
                    await self._run_logs.append(
                        task_id=task_id,
                        record_type="WORKER_IDLE",
                        actor="assignment_service",
                        payload={
                            "coordination_action": action,
                            "active_assignments": [
                                {
                                    "assignment_id": str(item.assignment_id),
                                    "agent_type": item.assigned_agent_type,
                                    "status": item.status.value,
                                    "lease_expires_at": (
                                        item.lease_expires_at.isoformat()
                                        if item.lease_expires_at is not None
                                        else None
                                    ),
                                }
                                for item in waiting
                                if item.status
                                in {
                                    AssignmentStatus.PENDING,
                                    AssignmentStatus.CLAIMED,
                                    AssignmentStatus.RUNNING,
                                }
                            ],
                        },
                    )
                return actions
        actions.append("WORKER_RUN_LIMIT_REACHED")
        return actions

    async def _snapshot(
        self,
        task_id: UUID,
        conversation_id: UUID,
        controller_result: ControllerRunResult,
    ) -> DemoTaskSnapshot:
        """从正式 Repository 读取子任务、Assignment、运行结果和 Artifact 内容。"""

        async with self._database.session_factory() as session:
            tasks = SqlAlchemyTaskRepository(session)
            master = await tasks.get_master(task_id)
            subtasks = await tasks.list_subtasks(task_id)
            assignments = await SqlAlchemyAssignmentRepository(session).list_for_task(task_id)
            runs = await SqlAlchemyAgentRunRepository(session).list_results(task_id)
            artifacts = await SqlAlchemyArtifactRepository(session).list_for_task(task_id)
        return DemoTaskSnapshot(
            task_id=task_id,
            conversation_id=conversation_id,
            controller_result=controller_result,
            subtasks=subtasks,
            assignments=assignments,
            runs=runs,
            artifacts=artifacts,
            master_task_status=master.status if master is not None else None,
        )

    async def artifact_preview(self, record: ArtifactRecord) -> object | None:
        """读取非 RawDocument JSON 内容，供演示接口展示 Skill 的实际输出。"""

        if record.ref.artifact_type == "RawDocument":
            return None
        raw = await self._artifact_writer().read_content(record.ref.artifact_id)
        return cast(object, json.loads(raw))

    async def resume_ready_workers(
        self,
        *,
        task_id: UUID,
        max_worker_runs: int = 30,
    ) -> DemoTaskSnapshot:
        """在人工审核批准后继续执行已经解锁的原完整计划。

        本方法不会在开始时重新调用总控 Agent；它只运行现有 Assignment。只有全部
        计划进入终态时，TaskCoordinator 才会按既有规则调用总控做最终目标检查。
        """

        await self._refresh_custom_registries()
        context = await PostgresContextService(self._database.session_factory).get(task_id)
        if context is None:
            raise ContextConflictError("实验任务不存在，不能继续审核后的计划")
        actions = await self._run_workers(
            task_id=task_id,
            max_worker_runs=max_worker_runs,
        )
        async with self._database.session_factory() as session:
            subtasks = await SqlAlchemyTaskRepository(session).list_subtasks(task_id)
        latest_revision = max(
            (item.decomposition_revision for item in subtasks),
            default=context.master_task.current_decomposition_revision,
        )
        current_subtasks = [
            item for item in subtasks if item.decomposition_revision == latest_revision
        ]
        decomposition = TaskDecompositionResult(
            task_intent=context.current_task_spec.task_intent,
            task_spec=context.current_task_spec,
            subtasks=current_subtasks,
        )
        snapshot = await self._snapshot(
            task_id,
            context.conversation.conversation_id,
            ControllerRunResult(
                decomposition=decomposition,
                shared_context=context,
            ),
        )
        return replace(snapshot, coordination_actions=actions)
