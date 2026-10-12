"""提供与生产环境隔离的 Agent 真实输入执行日志应用。

本应用使用独立数据库、独立 MinIO Bucket 和独立 HTTP 端口。它真实调用总控 Agent 与
专业 Agent，业务 Skill 通过正式 Registry 执行。应用只
输出公开的结构化决策和执行事实，是否正确由用户人工判断，不生成自动评分。
"""

from __future__ import annotations

import asyncio
import json
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kg_agent.agents.controller.state import ControllerRunResult
from kg_agent.api.schemas import (
    DemoArtifactView,
    DemoAssignmentView,
    DemoRunResponse,
    DemoSkillExecutionView,
    DemoSubTaskView,
    DemoWorkerRunView,
    ErrorResponse,
)
from kg_agent.config import Settings
from kg_agent.domain.enums import MasterTaskStatus
from kg_agent.domain.task_models import AgentSubTask
from kg_agent.exceptions import ConfigurationError, KGAgentError
from kg_agent.extensions.contracts import (
    AvailableSkillOption,
    BusinessAgentDraftInput,
    BusinessSkillDraftInput,
    CustomAgentDefinitionInput,
    CustomAgentDetail,
    CustomResourceSummary,
    CustomSkillContractCheck,
    CustomSkillDefinitionInput,
    CustomSkillDetail,
    SkillContractProposal,
)
from kg_agent.extensions.script_field_mapping import (
    ScriptFunctionAdapter,
    ScriptOutputFieldBinding,
    ScriptOutputMappingProposal,
)
from kg_agent.extensions.service import CustomExtensionService
from kg_agent.infrastructure.database.artifact_repository import SqlAlchemyArtifactRepository
from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.infrastructure.object_storage.local_file import LocalFileObjectStorage
from kg_agent.infrastructure.object_storage.minio import MinioObjectStorage
from kg_agent.memory.base import LongTermMemoryStore
from kg_agent.memory.mem0_cloud import Mem0CloudMemoryStore
from kg_agent.memory.memory_service import LongTermMemoryService
from kg_agent.memory.stores import DisabledLongTermMemoryStore
from kg_agent.model_gateway.deepseek import DeepSeekModelAdapter
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.demo_service import DemoOrchestrationService, DemoTaskSnapshot, UploadedFile
from kg_agent.services.graph_preview_service import GraphPreviewService
from kg_agent.services.model_call_observer import RunLogModelCallObserver
from kg_agent.services.platform_query_service import PlatformQueryService
from kg_agent.services.published_asset_management_service import PublishedAssetManagementService
from kg_agent.services.review_commands import ResolveHumanReviewCommand
from kg_agent.services.run_log_service import RunLogService
from kg_agent.services.task_coordinator import TaskCoordinator
from kg_agent.services.task_orchestration_service import TaskOrchestrationService
from simulation_lab.plan_draft_store import LoadedSimulationPlan, SimulationPlanDraftStore
from simulation_lab.schemas import (
    SimulationLogEntry,
    SimulationMemoryReport,
    SimulationPlanApprovalRequest,
    SimulationPlanReview,
    SimulationPlanRevisionRequest,
    SimulationProgress,
    SimulationRunReport,
    SimulationTimingEntry,
)


def _pending_plan_review(plan: LoadedSimulationPlan) -> SimulationPlanReview:
    """把尚未发布的 MinIO 候选计划转换为页面审核摘要。"""

    decomposition = plan.stored.decomposition
    revision = (
        decomposition.subtasks[0].decomposition_revision
        if decomposition.subtasks
        else decomposition.task_spec.revision
    )
    return SimulationPlanReview(
        decomposition_revision=revision,
        status="PENDING",
        task_intent=decomposition.task_intent.value,
        business_goal=decomposition.task_spec.business_goal,
        subtask_count=len(decomposition.subtasks),
    )


def _approved_plan_review(snapshot: DemoTaskSnapshot) -> SimulationPlanReview:
    """把已发布的当前拆解转换为可恢复的审核状态。

    候选计划发布后 MinIO 草稿会被标记为 PUBLISHED，因此页面刷新时
    不能再从“当前待审草稿”读到它。已持久化的 TaskSpec 和子任务才是
    此后的事实来源；把它们还原为 APPROVED 摘要，可让页面继续显示
    “继续执行已批准计划”，而不是误判为尚未审核或隐藏恢复入口。
    """

    decomposition = snapshot.controller_result.decomposition
    revision = (
        decomposition.subtasks[0].decomposition_revision
        if decomposition.subtasks
        else decomposition.task_spec.revision
    )
    return SimulationPlanReview(
        decomposition_revision=revision,
        status="APPROVED",
        task_intent=decomposition.task_intent.value,
        business_goal=decomposition.task_spec.business_goal,
        subtask_count=len(decomposition.subtasks),
    )


class PublishedAssetRenameRequest(BaseModel):
    """测试端目录显示名称请求，与正式端使用相同业务限制。"""

    title: str = Field(min_length=1, max_length=120)


class InputDecision:
    """记录真实上传文件或纯文本任务的输入事实，用于公开执行日志。"""

    def __init__(self, *, mode: str, reason: str, matched_evidence: str | None = None) -> None:
        self.mode = mode
        self.reason = reason
        self.matched_evidence = matched_evidence


def _dependency_order_violations(
    subtasks: list[AgentSubTask], ordered_subtask_ids: list[UUID]
) -> list[str]:
    """返回审核顺序中违反计划内前置依赖的子任务说明。

    计划审核允许用户调整同一可执行层级的优先级，但不能把依赖方排在其前置任务之前。
    只检查本次候选计划内部的依赖；指向历史已完成任务的依赖不在此处重复要求排序。
    """

    positions = {subtask_id: index for index, subtask_id in enumerate(ordered_subtask_ids)}
    names = {item.subtask_id: item.name for item in subtasks}
    violations: list[str] = []
    for item in subtasks:
        for dependency_id in item.dependency_subtask_ids:
            if dependency_id not in positions or dependency_id not in names:
                continue
            if positions[dependency_id] >= positions[item.subtask_id]:
                violations.append(f"“{item.name}”必须排在前置任务“{names[dependency_id]}”之后")
    return violations


def _real_document_input_decision(files: list[UploadFile]) -> InputDecision:
    """为用户明确选择的真实业务文件生成可观察输入决策。"""

    filenames = [item.filename or "unnamed" for item in files]
    return InputDecision(
        mode="NEW_REAL_DOCUMENT",
        reason="用户通过测试端业务文档窗口上传了真实文件。",
        matched_evidence="、".join(filenames),
    )


async def _read_business_uploads(files: list[UploadFile]) -> list[UploadedFile]:
    """读取测试端业务文件，并把受支持扩展名规范为稳定 MIME。"""

    canonical_media_types = {
        ".pdf": "application/pdf",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".tif": "image/tiff",
        ".tiff": "image/tiff",
        ".webp": "image/webp",
    }
    return [
        UploadedFile(
            filename=item.filename or "unnamed",
            media_type=canonical_media_types.get(
                Path(item.filename or "").suffix.lower(),
                item.content_type or "application/octet-stream",
            ),
            content=await item.read(),
        )
        for item in files
    ]


async def _selected_published_assets(
    service: DemoOrchestrationService,
    *,
    ontology_artifact_id: UUID | None,
    graph_artifact_id: UUID | None,
) -> list[UploadedFile]:
    """从测试端独立资产库导入已发布本体或图谱增量基线。"""

    query = PlatformQueryService(service.database, service.artifacts)
    selected: list[UploadedFile] = []
    if ontology_artifact_id is not None:
        record = await query.artifact(ontology_artifact_id)
        if record is None or record.ref.artifact_type != "PublishedOntology":
            raise HTTPException(status_code=422, detail="选择的本体不是可用 PublishedOntology")
        selected.append(
            UploadedFile(
                filename=f"published-ontology-{ontology_artifact_id}.json",
                media_type="application/json",
                content=await query.artifact_content(ontology_artifact_id),
                artifact_type="PublishedOntology",
                source_artifact_id=ontology_artifact_id,
            )
        )
    if graph_artifact_id is not None:
        record = await query.artifact(graph_artifact_id)
        if record is None or record.ref.artifact_type != "PublishedGraph":
            raise HTTPException(status_code=422, detail="选择的图谱不是可用 PublishedGraph")
        payload = json.loads(await query.artifact_content(graph_artifact_id))
        for key in ("graph_id", "graph_version", "approval_artifact_id"):
            payload.pop(key, None)
        selected.append(
            UploadedFile(
                filename=f"published-graph-{graph_artifact_id}.json",
                media_type="application/json",
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                artifact_type="CandidateGraph",
                source_artifact_id=graph_artifact_id,
            )
        )
    return selected


def _read_simulation_bool(name: str, *, default: bool) -> bool:
    """读取实验室布尔配置，拒绝可能造成环境隔离误判的含糊值。"""

    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    normalized = raw_value.strip().lower()
    if normalized in {"true", "1", "yes"}:
        return True
    if normalized in {"false", "0", "no"}:
        return False
    raise ConfigurationError(f"{name} 必须是 true 或 false")


def _build_simulation_storage(settings: Settings) -> ObjectStorage:
    """构建测试端专属 MinIO 适配器，禁止与正式 Artifact Bucket 混用。

    连接地址和凭据默认复用当前部署的 MinIO 服务，但 Bucket 必须通过
    ``SIMULATION_MINIO_BUCKET`` 独立命名。这样可以避免增加第二套对象存储服务，
    同时保证 8002 进程重启后历史 Artifact 内容仍可供同一 MasterTask 复用。

    Args:
        settings: 已完成基础类型校验的服务端配置。

    Returns:
        尚未发起网络请求的测试端 MinIO 适配器。

    Raises:
        ConfigurationError: 缺少测试端可用的 MinIO 地址或凭据，或者误用了正式 Bucket。
    """

    local_data_dir = os.getenv("SIMULATION_LOCAL_UV_DATA_DIR", "").strip()
    if local_data_dir:
        return LocalFileObjectStorage(
            Path(local_data_dir).expanduser().resolve() / "objects",
            bucket="simulation-artifacts",
        )
    endpoint = (os.getenv("SIMULATION_MINIO_ENDPOINT") or settings.minio_endpoint or "").strip()
    access_key = (
        os.getenv("SIMULATION_MINIO_ACCESS_KEY") or settings.minio_access_key or ""
    ).strip()
    secret_key = (
        os.getenv("SIMULATION_MINIO_SECRET_KEY") or settings.minio_secret_key or ""
    ).strip()
    bucket = os.getenv("SIMULATION_MINIO_BUCKET", "kg-agent-simulation-artifacts").strip()
    secure = _read_simulation_bool(
        "SIMULATION_MINIO_SECURE",
        default=settings.minio_secure,
    )
    if not endpoint:
        raise ConfigurationError("测试端持久化 Artifact 前必须配置 SIMULATION_MINIO_ENDPOINT")
    if not access_key:
        raise ConfigurationError("测试端持久化 Artifact 前必须配置 MinIO Access Key")
    if not secret_key:
        raise ConfigurationError("测试端持久化 Artifact 前必须配置 MinIO Secret Key")
    if not bucket:
        raise ConfigurationError("SIMULATION_MINIO_BUCKET 不能为空")
    if bucket == settings.minio_bucket:
        raise ConfigurationError("测试端 SIMULATION_MINIO_BUCKET 不能与正式 Bucket 相同")
    return MinioObjectStorage(
        endpoint=endpoint,
        access_key=access_key,
        secret_key=secret_key,
        bucket=bucket,
        secure=secure,
    )


def _build_simulation_memory(settings: Settings) -> LongTermMemoryService:
    """为 8002 装配独立 Mem0 命名空间，不复用正式端的用户与应用标识。"""

    api_key = os.getenv("SIMULATION_MEM0_API_KEY") or settings.mem0_api_key
    enabled = _read_simulation_bool("SIMULATION_MEM0_ENABLED", default=bool(api_key))
    user_id = os.getenv("SIMULATION_MEM0_USER_ID", "kg-agent-v1-simulation-user").strip()
    app_id = os.getenv("SIMULATION_MEM0_APP_ID", "kg-agent-platform-simulation").strip()
    base_url = os.getenv("SIMULATION_MEM0_BASE_URL", settings.mem0_base_url).strip()
    if not user_id:
        raise ConfigurationError("SIMULATION_MEM0_USER_ID 不能为空")
    if not app_id:
        raise ConfigurationError("SIMULATION_MEM0_APP_ID 不能为空")
    if enabled and not api_key:
        raise ConfigurationError("SIMULATION_MEM0_ENABLED=true 时必须配置 Mem0 API Key")

    store: LongTermMemoryStore
    if enabled:
        store = Mem0CloudMemoryStore(
            api_key=api_key or "",
            base_url=base_url,
            timeout_seconds=settings.mem0_timeout_seconds,
        )
    else:
        store = DisabledLongTermMemoryStore()
    return LongTermMemoryService(
        store,
        user_id=user_id,
        app_id=app_id,
        default_top_k=settings.mem0_top_k,
        candidate_top_k=settings.mem0_candidate_top_k,
        default_threshold=settings.mem0_threshold,
        dedup_threshold=settings.mem0_dedup_threshold,
        merge_threshold=settings.mem0_merge_threshold,
        default_rerank=settings.mem0_rerank,
    )


def _collect_text_preview(value: Any) -> list[str]:
    """递归收集 Artifact 中的文字字段，方便页面观察真实解析结果。"""

    result: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key == "text" and isinstance(child, str) and child.strip():
                result.append(child)
            else:
                result.extend(_collect_text_preview(child))
    elif isinstance(value, list):
        for child in value:
            result.extend(_collect_text_preview(child))
    return result


async def _present_snapshot(
    service: DemoOrchestrationService, snapshot: DemoTaskSnapshot
) -> DemoRunResponse:
    """把正式领域对象转换为不包含隐藏推理的公开观察结果。"""

    # 多轮任务保留全部历史，但本轮路由和进度只展示最近一个实际发布了子任务的
    # decomposition revision，防止用户把旧计划误认为本轮总控新拆出的任务。
    latest_revision = max(
        (item.decomposition_revision for item in snapshot.subtasks),
        default=0,
    )
    current_subtasks = sorted(
        [item for item in snapshot.subtasks if item.decomposition_revision == latest_revision],
        key=lambda item: (-item.priority, item.created_at, str(item.subtask_id)),
    )
    current_subtask_ids = {item.subtask_id for item in current_subtasks}
    current_assignments = [
        item for item in snapshot.assignments if item.subtask_id in current_subtask_ids
    ]
    current_assignment_ids = {item.assignment_id for item in current_assignments}
    current_runs = [item for item in snapshot.runs if item.assignment_id in current_assignment_ids]
    run_views: list[DemoWorkerRunView] = []
    for run in current_runs:
        skill_views: list[DemoSkillExecutionView] = []
        for skill_name in run.skills_used:
            outputs = [
                record.ref
                for record in snapshot.artifacts
                if record.ref.producer_run_id == run.run_id
                and record.ref.producer_skill == skill_name
            ]
            skill_views.append(
                DemoSkillExecutionView(
                    skill_name=skill_name,
                    duration_ms=float(run.metrics.get(f"skill.{skill_name}.duration_ms", 0.0)),
                    output_artifact_ids=[item.artifact_id for item in outputs],
                    output_artifact_types=[item.artifact_type for item in outputs],
                )
            )
        run_views.append(
            DemoWorkerRunView(
                run_id=run.run_id,
                assignment_id=run.assignment_id,
                agent_type=run.agent_name,
                status=run.status.value,
                skills=skill_views,
                blocking_issues=run.blocking_issues,
            )
        )

    artifact_views: list[DemoArtifactView] = []
    text_preview: list[str] = []
    for record in snapshot.artifacts:
        content = await service.artifact_preview(record)
        if record.ref.artifact_type in {"ParsedDocument", "SemanticDocument"}:
            for item in _collect_text_preview(content):
                if item not in text_preview:
                    text_preview.append(item)
        artifact_views.append(
            DemoArtifactView(
                artifact_id=record.ref.artifact_id,
                artifact_type=record.ref.artifact_type,
                producer_agent=record.ref.producer_agent,
                producer_skill=record.ref.producer_skill,
                parent_artifact_ids=record.ref.parent_artifact_ids,
                content_summary=record.ref.content_summary,
                checksum=record.ref.checksum,
                is_mock_output=False,
                content_preview=content,
            )
        )

    decomposition = snapshot.controller_result.decomposition
    return DemoRunResponse(
        task_id=snapshot.task_id,
        conversation_id=snapshot.conversation_id,
        task_intent=decomposition.task_intent.value,
        requires_clarification=decomposition.requires_clarification,
        clarification_question=decomposition.clarification_question,
        task_spec=decomposition.task_spec.model_dump(mode="json"),
        subtasks=[
            DemoSubTaskView(
                subtask_id=item.subtask_id,
                decomposition_revision=item.decomposition_revision,
                name=item.name,
                subtask_type=item.subtask_type,
                assigned_agent_type=item.assigned_agent_type,
                status=item.status.value,
                dependency_subtask_ids=item.dependency_subtask_ids,
                input_artifact_ids=item.input_artifact_ids,
                expected_output_types=item.expected_output_types,
            )
            for item in current_subtasks
        ],
        assignments=[
            DemoAssignmentView(
                assignment_id=item.assignment_id,
                subtask_id=item.subtask_id,
                assigned_agent_type=item.assigned_agent_type,
                claimed_by=item.claimed_by,
                status=item.status.value,
                attempt=item.attempt,
            )
            for item in current_assignments
        ],
        worker_runs=run_views,
        mock_parsed_text_preview=text_preview,
        artifacts=artifact_views,
    )


def _build_execution_logs(
    observed: DemoRunResponse,
    memory: SimulationMemoryReport,
    input_decision: InputDecision,
    coordination_actions: list[str],
    timings: list[SimulationTimingEntry],
    plan_review: SimulationPlanReview | None = None,
) -> list[SimulationLogEntry]:
    """把公开领域结果展开为便于人工检查的连续顺序日志。"""

    logs: list[SimulationLogEntry] = []

    def append(
        phase: str,
        actor: str,
        action: str,
        status: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        """根据插入顺序生成 sequence，不伪造模型内部推理步骤。"""

        logs.append(
            SimulationLogEntry.model_validate(
                {
                    "sequence": len(logs) + 1,
                    "phase": phase,
                    "actor": actor,
                    "action": action,
                    "status": status,
                    "message": message,
                    "details": details or {},
                }
            )
        )

    external_ontologies = [
        item for item in observed.artifacts if item.artifact_type == "ExternalOntology"
    ]
    if external_ontologies:
        append(
            "INPUT",
            "simulation_lab",
            "REGISTER_EXTERNAL_ONTOLOGY",
            "SUCCESS",
            "已校验并登记用户上传的外部本体 JSON；后续仍需按本体审核门禁处理。",
            {
                "artifact_ids": [str(item.artifact_id) for item in external_ontologies],
                "ontologies": [item.content_summary for item in external_ontologies],
            },
        )
    raw_documents = [item for item in observed.artifacts if item.artifact_type == "RawDocument"]
    if input_decision.mode == "NEW_REAL_DOCUMENT":
        document_action = "REGISTER_REAL_DOCUMENT"
        document_message = "已将用户选择的真实业务文件登记为隔离 RawDocument。"
    elif input_decision.mode == "REUSE_EXISTING_INPUT":
        document_action = "REUSE_EXISTING_INPUT"
        document_message = "当前输入未表达新增文件，本轮只复用任务中已有 Artifact。"
    elif input_decision.mode == "PLAN_APPROVED":
        document_action = "APPROVE_AGENT_PLAN"
        document_message = "用户已批准当前意图和子任务计划，本轮开始执行既有 Assignment。"
    elif input_decision.mode == "PLAN_REVISED":
        document_action = "REVISE_AGENT_PLAN"
        document_message = "用户已提交计划修改意见；总控已发布新的意图与子任务 revision。"
    elif input_decision.mode == "RECOVERED":
        document_action = "RECOVER_TASK_SNAPSHOT"
        document_message = "页面刷新后已从持久化存储恢复当前任务，不创建新的输入 Artifact。"
    elif external_ontologies:
        document_action = "USE_NO_RAW_DOCUMENT"
        document_message = "当前输入未创建 RawDocument；本轮使用已上传的 ExternalOntology。"
    else:
        document_action = "USE_TEXT_ONLY_INPUT"
        document_message = "当前输入未明确提到已有或上传文档，本轮按纯文本任务交给总控 Agent。"
    append(
        "INPUT",
        "simulation_lab",
        document_action,
        ("SUCCESS" if input_decision.mode == "NEW_REAL_DOCUMENT" else "INFO"),
        document_message,
        {
            "decision_mode": input_decision.mode,
            "decision_reason": input_decision.reason,
            "matched_user_evidence": input_decision.matched_evidence,
            "available_raw_artifact_ids": [str(item.artifact_id) for item in raw_documents],
            "available_raw_documents": [item.content_summary for item in raw_documents],
        },
    )
    append(
        "MEMORY",
        "memory_service",
        "RECALL_LONG_TERM_MEMORY",
        (
            "SUCCESS"
            if memory.recall_status == "SUCCEEDED" and memory.recalled_memories
            else "INFO"
            if memory.recall_status == "SUCCEEDED"
            else "WARNING"
        ),
        (
            f"已从测试端独立命名空间召回 {len(memory.recalled_memories)} 条长期记忆。"
            if memory.recall_status == "SUCCEEDED"
            else "测试端 Mem0 召回失败，本轮已按无长期记忆继续。"
            if memory.recall_status == "FAILED"
            else "测试端长期记忆未启用，本轮按无长期记忆继续。"
        ),
        {
            "provider": memory.provider,
            "app_id": memory.app_id,
            "recall_status": memory.recall_status,
            "recalled_count": len(memory.recalled_memories),
            "memories": [
                {
                    "memory_id": str(item.memory_id),
                    "memory_type": item.memory_type,
                    "summary": item.summary,
                    "source_refs": item.source_refs,
                }
                for item in memory.recalled_memories
            ],
        },
    )
    append(
        "INTENT",
        "controller_agent",
        "RECOGNIZE_INTENT",
        "SUCCESS",
        f"总控 Agent 输出顶层意图：{observed.task_intent}",
        {
            "task_intent": observed.task_intent,
            "requires_clarification": observed.requires_clarification,
            "clarification_question": observed.clarification_question,
        },
    )
    append(
        "TASK_SPEC",
        "controller_agent",
        "BUILD_TASK_SPEC",
        "SUCCESS",
        f"总控 Agent 形成任务目标：{observed.task_spec.get('business_goal', '')}",
        {
            "revision": observed.task_spec.get("revision"),
            "domain": observed.task_spec.get("domain"),
            "target_output_types": observed.task_spec.get("target_output_types", []),
            "ontology_strategy": observed.task_spec.get("ontology_strategy", {}),
        },
    )

    assignments = {item.subtask_id: item for item in observed.assignments}
    runs = {item.assignment_id: item for item in observed.worker_runs}
    ordered_subtasks = sorted(
        observed.subtasks,
        key=lambda item: (item.decomposition_revision, str(item.subtask_id)),
    )
    plan_is_unpublished = plan_review is not None and plan_review.status == "PENDING"
    # 先完整展示总控生成并校验的候选 AgentSubTask。待审核计划尚未写入 SharedContext；
    # 只有已批准快照才使用“发布”措辞并继续展示 Assignment/Worker 事实。
    for subtask_index, subtask in enumerate(ordered_subtasks, start=1):
        append(
            "DECOMPOSITION",
            "controller_agent",
            "DRAFT_SUBTASK" if plan_is_unpublished else "PUBLISH_SUBTASK",
            "SUCCESS",
            (
                f"候选子任务 {subtask_index}：{subtask.name}（尚未发布）"
                if plan_is_unpublished
                else f"子任务 {subtask_index}：{subtask.name}"
            ),
            {
                "subtask_id": str(subtask.subtask_id),
                "decomposition_revision": subtask.decomposition_revision,
                "subtask_type": subtask.subtask_type,
                "assigned_agent_type": subtask.assigned_agent_type,
                "dependency_subtask_ids": [str(item) for item in subtask.dependency_subtask_ids],
                "input_artifact_ids": [str(item) for item in subtask.input_artifact_ids],
                "expected_output_types": subtask.expected_output_types,
            },
        )
    if plan_review is not None:
        append(
            "PLAN_REVIEW",
            "simulation_plan_review",
            "REVIEW_AGENT_PLAN",
            "WARNING" if plan_review.status == "PENDING" else "SUCCESS",
            (
                "总控已生成并校验候选意图和全部子任务；用户批准前不会发布子任务。"
                if plan_review.status == "PENDING"
                else "意图和子任务计划已由用户批准，专业 Agent 可以领取任务。"
            ),
            plan_review.model_dump(mode="json"),
        )

    # 候选计划获批并正式发布后，才展示 Assignment、认领、Skill 与 Artifact 事实。
    for subtask_index, subtask in enumerate(ordered_subtasks, start=1):
        if plan_is_unpublished:
            continue
        assignment = assignments.get(subtask.subtask_id)
        if assignment is None:
            append(
                "ASSIGNMENT",
                "assignment_service",
                "CREATE_ASSIGNMENT",
                "WARNING",
                f"子任务 {subtask_index} 尚未生成 Assignment。",
            )
            continue
        append(
            "ASSIGNMENT",
            "assignment_service",
            "CREATE_ASSIGNMENT",
            "SUCCESS",
            f"将“{subtask.name}”分配给 {assignment.assigned_agent_type}。",
            {
                "assignment_id": str(assignment.assignment_id),
                "subtask_id": str(assignment.subtask_id),
                "agent_type": assignment.assigned_agent_type,
                "attempt": assignment.attempt,
            },
        )
        append(
            "CLAIM",
            assignment.claimed_by or assignment.assigned_agent_type,
            "CLAIM_ASSIGNMENT",
            "SUCCESS" if assignment.claimed_by else "WARNING",
            (
                f"{assignment.claimed_by} 已认领子任务。"
                if assignment.claimed_by
                else "Assignment 尚未被 Worker 认领。"
            ),
            {"assignment_status": assignment.status},
        )
        run = runs.get(assignment.assignment_id)
        if run is None:
            append(
                "AGENT_RUN",
                assignment.assigned_agent_type,
                "RUN_SUBTASK",
                "WARNING",
                "专业 Agent 尚未产生 AgentRunResult。",
            )
            continue
        run_log_status = (
            "SUCCESS"
            if run.status == "SUCCESS"
            else "WARNING"
            if run.status == "PARTIAL_SUCCESS"
            else "FAILED"
        )
        append(
            "AGENT_RUN",
            run.agent_type,
            "RUN_SUBTASK",
            run_log_status,
            f"{run.agent_type} 完成子任务，AgentRun 状态为 {run.status}。",
            {
                "run_id": str(run.run_id),
                "assignment_id": str(run.assignment_id),
                "blocking_issues": run.blocking_issues,
            },
        )
        for skill_index, skill in enumerate(run.skills, start=1):
            skill_log_status = "SUCCESS" if skill.output_artifact_ids else "WARNING"
            append(
                "SKILL",
                run.agent_type,
                "INVOKE_SKILL",
                skill_log_status,
                f"Skill 调用顺序 {skill_index}：{skill.skill_name}。",
                {
                    "skill_order": skill_index,
                    "skill_name": skill.skill_name,
                    "duration_ms": skill.duration_ms,
                    "output_artifact_types": skill.output_artifact_types,
                    "output_artifact_ids": [str(item) for item in skill.output_artifact_ids],
                },
            )

    action_messages = {
        "CONTROLLER_REPLANNED": "完整计划执行后验收未通过，总控 Agent 已发布必要修复任务。",
        "WAITING_ASSIGNMENTS": "完整计划中仍有可执行 Assignment，由专业 Agent 继续领取。",
        "WAITING_DEPENDENCIES": "下游子任务正在等待前置子任务 Artifact 或人工审核结果。",
        "TASK_COMPLETED": "总控确认业务语义已满足，MasterTask 已完成。",
        "WAITING_HUMAN": "任务已到达人工审核门禁。",
        "REVIEW_CREATED": "候选产物与质量报告齐备，已创建人工审核请求。",
        "WAITING_USER": "总控需要用户补充信息后才能继续。",
        "FINAL_CHECK_FAILED": "总控最终目标检查失败；已保留全部产物并暂停，可根据日志修复后重试。",
        "REVISION_LIMIT_REACHED": "任务达到拆解修订安全上限，已暂停。",
        "WORKER_RUN_LIMIT_REACHED": "本次运行达到 Worker 执行次数上限。",
        "NOOP": "任务已经终止，无需继续推进。",
    }
    for wave_index, action in enumerate(coordination_actions, start=1):
        append(
            "COORDINATION",
            "task_coordinator",
            action,
            (
                "FAILED"
                if action in {"REVISION_LIMIT_REACHED", "FINAL_CHECK_FAILED"}
                else "WARNING"
                if action in {"WAITING_USER", "WORKER_RUN_LIMIT_REACHED"}
                else "SUCCESS"
            ),
            action_messages.get(action, f"协调器返回动作：{action}"),
            {"coordination_step": wave_index, "action": action},
        )

    for timing in timings:
        append(
            "TIMING",
            timing.actor,
            "MEASURE_DURATION",
            "INFO",
            f"{timing.operation}耗时 {_format_duration(timing.duration_ms)}。",
            timing.model_dump(mode="json"),
        )

    for artifact in observed.artifacts:
        if artifact.artifact_type == "RawDocument":
            continue
        append(
            "ARTIFACT",
            artifact.producer_agent,
            "CREATE_ARTIFACT",
            "SUCCESS",
            f"{artifact.producer_skill} 生成 {artifact.artifact_type}。",
            {
                "artifact_id": str(artifact.artifact_id),
                "artifact_type": artifact.artifact_type,
                "producer_skill": artifact.producer_skill,
                "parent_artifact_ids": [str(item) for item in artifact.parent_artifact_ids],
                "content_preview": artifact.content_preview,
            },
        )
    receipt = memory.write_receipt
    append(
        "MEMORY",
        "memory_service",
        "WRITE_TASK_TURN_MEMORY",
        (
            "SUCCESS"
            if receipt is not None and receipt.status.upper() in {"PENDING", "SUCCEEDED"}
            else "WARNING"
        ),
        (
            f"已向测试端 Mem0 提交本轮公开消息与任务摘要，状态：{receipt.status}。"
            if receipt is not None and memory.enabled
            else "本轮未向测试端 Mem0 写入长期记忆。"
        ),
        {
            "provider": memory.provider,
            "app_id": memory.app_id,
            "event_id": receipt.event_id if receipt is not None else None,
            "write_status": receipt.status if receipt is not None else "NOT_WRITTEN",
        },
    )
    return logs


def _format_duration(duration_ms: float) -> str:
    """把毫秒转换成适合测试工作台阅读的耗时文本。"""

    if duration_ms >= 1000:
        return f"{duration_ms / 1000:.2f} 秒"
    return f"{duration_ms:.2f} 毫秒"


async def _load_timing_entries(
    service: DemoOrchestrationService,
    task_id: UUID,
) -> list[SimulationTimingEntry]:
    """从测试端独立 JSONL 中提取控制器、子任务、Skill 与总耗时。"""

    raw = await service.run_logs.read(task_id)
    if raw is None:
        return []
    timings: list[SimulationTimingEntry] = []

    def add(
        *,
        category: str,
        actor: str,
        operation: str,
        duration_ms: object,
        subtask_id: str | None = None,
        run_id: str | None = None,
    ) -> None:
        """只接收合法的非负数值，避免诊断日志异常破坏主响应。"""

        if not isinstance(duration_ms, int | float) or duration_ms < 0:
            return
        timings.append(
            SimulationTimingEntry.model_validate(
                {
                    "sequence": len(timings) + 1,
                    "category": category,
                    "actor": actor,
                    "operation": operation,
                    "duration_ms": duration_ms,
                    "subtask_id": subtask_id,
                    "run_id": run_id,
                }
            )
        )

    controller_index = 0
    coordination_index = 0
    for line in raw.decode("utf-8").splitlines():
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(record, dict) or not isinstance(record.get("payload"), dict):
            continue
        payload = record["payload"]
        record_type = record.get("record_type")
        actor = str(record.get("actor") or "unknown")
        if record_type == "MEMORY_RECALL":
            add(
                category="MEMORY",
                actor=actor,
                operation="长期记忆召回",
                duration_ms=payload.get("duration_ms"),
            )
        elif record_type == "CONTROLLER_RUN":
            controller_index += 1
            add(
                category="CONTROLLER",
                actor=actor,
                operation=f"总控意图识别与任务拆解（第 {controller_index} 次）",
                duration_ms=payload.get("duration_ms"),
            )
        elif record_type == "MODEL_CALL":
            component = str(payload.get("component") or "MODEL")
            skill_name = payload.get("skill_name")
            operation = (
                f"Agent 加载 Skill 提示词后调用模型：{skill_name}"
                if component == "SKILL" and skill_name
                else f"模型调用：{component}"
            )
            add(
                category="MODEL",
                actor=actor,
                operation=operation,
                duration_ms=payload.get("duration_ms"),
                subtask_id=str(payload.get("subtask_id")) if payload.get("subtask_id") else None,
            )
        elif record_type == "AGENT_RUN":
            metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
            subtask_id = str(payload.get("subtask_id")) if payload.get("subtask_id") else None
            run_id = str(payload.get("run_id")) if payload.get("run_id") else None
            add(
                category="AGENT",
                actor=actor,
                operation=f"专业 Agent 子任务：{subtask_id or '未知'}",
                duration_ms=metrics.get(
                    "agent_total_duration_ms", payload.get("worker_total_duration_ms")
                ),
                subtask_id=subtask_id,
                run_id=run_id,
            )
            for key, value in metrics.items():
                if key.startswith("skill.") and key.endswith(".duration_ms"):
                    skill_name = key.removeprefix("skill.").removesuffix(".duration_ms")
                    add(
                        category="SKILL",
                        actor=actor,
                        operation=f"Skill：{skill_name}",
                        duration_ms=value,
                        subtask_id=subtask_id,
                        run_id=run_id,
                    )
        elif record_type == "COORDINATION":
            coordination_index += 1
            add(
                category="COORDINATION",
                actor=actor,
                operation=f"计划依赖与终态检查（第 {coordination_index} 次）",
                duration_ms=payload.get("duration_ms"),
            )
        elif record_type == "ORCHESTRATION_RUN" and not payload.get("internal_continuation"):
            add(
                category="TOTAL",
                actor=actor,
                operation="本次用户请求端到端执行",
                duration_ms=payload.get("duration_ms"),
            )
    return timings


def _calculate_progress(
    observed: DemoRunResponse,
    coordination_actions: list[str],
    plan_review: SimulationPlanReview | None = None,
    master_task_status: MasterTaskStatus | None = None,
) -> SimulationProgress:
    """根据持久化任务终态、完整计划和协调动作生成任务进度。

    ``MasterTask.status`` 可跨刷新、SSE 断线及服务重启稳定恢复，优先级必须高于
    单次请求内临时收集的 ``coordination_actions``。
    """

    total = len(observed.subtasks)
    completed = sum(item.status == "SUCCEEDED" for item in observed.subtasks)
    if master_task_status is MasterTaskStatus.SUCCEEDED:
        return SimulationProgress(
            percent=100,
            stage="目标已完成",
            completed_subtasks=completed,
            total_subtasks=total,
        )
    if master_task_status is MasterTaskStatus.FAILED:
        return SimulationProgress(
            percent=min(95, round(20 + (completed / total * 70 if total else 0))),
            stage="任务已失败",
            completed_subtasks=completed,
            total_subtasks=total,
        )
    if master_task_status is MasterTaskStatus.CANCELLED:
        return SimulationProgress(
            percent=min(95, round(20 + (completed / total * 70 if total else 0))),
            stage="任务已取消",
            completed_subtasks=completed,
            total_subtasks=total,
        )
    if observed.requires_clarification:
        return SimulationProgress(
            percent=15,
            stage="等待用户补充信息",
            completed_subtasks=completed,
            total_subtasks=total,
        )
    if plan_review is not None and plan_review.status == "PENDING":
        return SimulationProgress(
            percent=20,
            stage="等待意图与子任务审核",
            completed_subtasks=completed,
            total_subtasks=total,
        )
    final_action = coordination_actions[-1] if coordination_actions else None
    if final_action == "TASK_COMPLETED":
        return SimulationProgress(
            percent=100,
            stage="目标已完成",
            completed_subtasks=completed,
            total_subtasks=total,
        )
    if final_action in {"WAITING_HUMAN", "REVIEW_CREATED"}:
        return SimulationProgress(
            percent=min(90, round(20 + (completed / total * 70 if total else 0))),
            stage="等待人工审核",
            completed_subtasks=completed,
            total_subtasks=total,
        )
    if final_action in {
        "WAITING_USER",
        "FINAL_CHECK_FAILED",
        "REVISION_LIMIT_REACHED",
        "WORKER_RUN_LIMIT_REACHED",
    }:
        return SimulationProgress(
            percent=min(90, round(20 + (completed / total * 70 if total else 0))),
            stage="自动推进已暂停",
            completed_subtasks=completed,
            total_subtasks=total,
        )
    if total == 0:
        return SimulationProgress(
            percent=20,
            stage="总控未发布子任务",
            completed_subtasks=0,
            total_subtasks=0,
        )
    return SimulationProgress(
        percent=min(95, round(20 + completed / total * 70)),
        stage="等待协调器收口" if completed == total else "存在未完成子任务",
        completed_subtasks=completed,
        total_subtasks=total,
    )


def create_app(
    service: DemoOrchestrationService | None = None,
    *,
    database: Database | None = None,
    manage_database: bool = True,
    project_root: Path | None = None,
) -> FastAPI:
    """创建隔离日志应用；测试可以注入 FakeModel 组成的确定性服务。"""

    root = project_root or Path(__file__).resolve().parents[1]
    owned_database = database
    owned_storage: ObjectStorage | None = None
    extension_registry = ArtifactRegistry.v1()
    extension_agents = AgentRegistry.from_directory(root / "configs" / "agents")
    extension_skills = SkillRegistry.from_directory(
        root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=extension_registry,
    )
    extension_service: CustomExtensionService | None = None
    if service is None:
        settings = Settings.from_env()
        database_url = os.getenv("SIMULATION_DATABASE_URL", "sqlite+aiosqlite:///:memory:")
        owned_database = Database(database_url)
        run_logs = RunLogService(
            Path(
                os.getenv(
                    "SIMULATION_RUN_LOG_DIR",
                    str(root / "logs" / "simulation"),
                )
            )
        )
        model_gateway = DeepSeekModelAdapter(settings, observer=RunLogModelCallObserver(run_logs))
        extension_service = CustomExtensionService(
            owned_database.session_factory,
            artifact_registry=extension_registry,
            builtin_agent_keys=[item.agent_type for item in extension_agents.list_all()],
            builtin_skill_keys=[item.name for item in extension_skills.list_all()],
            model_gateway=model_gateway,
        )
        owned_storage = _build_simulation_storage(settings)
        # 测试端也走正式的多轮接入服务：公开用户消息保存在隔离数据库，下一轮总控
        # 会读取最近历史。测试数据库和 Mem0 命名空间仍与正式端完全隔离。
        service = TaskOrchestrationService(
            database=owned_database,
            storage=owned_storage,
            model_gateway=model_gateway,
            project_root=root,
            max_repairs=settings.controller_max_repairs,
            lease_seconds=settings.assignment_lease_seconds,
            heartbeat_seconds=settings.assignment_heartbeat_seconds,
            max_file_bytes=settings.artifact_max_file_bytes,
            max_files_per_task=settings.artifact_max_files_per_task,
            max_total_bytes_per_task=settings.artifact_max_total_bytes_per_task,
            memory_service=_build_simulation_memory(settings),
            auto_remember_long_term=True,
            run_log_service=run_logs,
            custom_extensions=extension_service,
        )
    if extension_service is None:
        # 注入测试 Service 时仍提供隔离定义包 API；该分支主要服务测试，不改变注入的
        # Service 既有运行链路。
        extension_service = CustomExtensionService(
            service.database.session_factory,
            artifact_registry=extension_registry,
            builtin_agent_keys=[item.agent_type for item in extension_agents.list_all()],
            builtin_skill_keys=[item.name for item in extension_skills.list_all()],
        )
    plan_drafts = SimulationPlanDraftStore(service.storage, service.run_logs)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """初始化实验室专属数据库与 Bucket，并在退出时释放数据库连接。"""

        if manage_database and owned_database is not None:
            await owned_database.create_schema()
        if manage_database and owned_storage is not None:
            await owned_storage.ensure_bucket()
        try:
            yield
        finally:
            if manage_database and owned_database is not None:
                await owned_database.dispose()

    app = FastAPI(
        title="Agent 人工观察实验室",
        version="1.0.0",
        description="隔离运行真实 Agent 与 LLM/确定性业务 Skills，并输出人工检查日志。",
        lifespan=lifespan,
    )
    web_root = Path(__file__).resolve().parent / "web"
    app.mount("/assets", StaticFiles(directory=web_root), name="simulation-assets")
    # 复用正式端的纯静态扩展管理页面。页面请求仍指向本测试端的 /api/v1/extensions，
    # 因而草稿、发布记录与 8000 正式端完全隔离。
    app.mount(
        "/workbench-assets",
        StaticFiles(directory=root / "src" / "kg_agent" / "web"),
        name="simulation-extension-assets",
    )
    # 测试端复用正式端的纯静态图谱渲染组件；该挂载不暴露 API、数据库或生产任务数据。
    app.mount(
        "/shared-assets",
        StaticFiles(directory=root / "src" / "kg_agent" / "web"),
        name="simulation-shared-assets",
    )

    @app.exception_handler(KGAgentError)
    async def business_error(_: Request, exc: KGAgentError) -> JSONResponse:
        """把可预期领域错误转换成不泄露堆栈的结构化响应。"""

        payload = ErrorResponse(error_code=type(exc).__name__, message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.get("/", include_in_schema=False)
    async def laboratory() -> FileResponse:
        """返回独立的 Agent 人工日志观察前端。"""

        # 测试端前端迭代频繁，入口页必须重新校验；否则浏览器可能继续引用旧版脚本，
        # 把已经正常的后端模式误显示为“模式异常”。静态资源本身再由版本参数区分。
        return FileResponse(
            web_root / "index.html",
            media_type="text/html; charset=utf-8",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    @app.get("/extensions", include_in_schema=False)
    async def extension_workbench() -> FileResponse:
        """返回测试端隔离的自定义 Agent 与 Skill 管理页面。"""

        return FileResponse(
            root / "src" / "kg_agent" / "web" / "extensions.html",
            media_type="text/html; charset=utf-8",
        )

    @app.get("/published-assets", include_in_schema=False)
    async def simulation_published_assets_workbench() -> FileResponse:
        """返回测试端隔离的已发布资产管理页。"""

        return FileResponse(
            root / "src" / "kg_agent" / "web" / "published_assets.html",
            media_type="text/html; charset=utf-8",
        )

    @app.get("/workbench", include_in_schema=False)
    async def simulation_extension_return() -> RedirectResponse:
        """兼容共享扩展页面的返回链接，回到 8002 的实验任务观察页。"""

        return RedirectResponse(url="/", status_code=307)

    @app.get("/health", tags=["simulation"])
    async def health() -> dict[str, str | bool]:
        """返回实验室进程状态及隔离模式标识。"""

        return {
            "status": "ok",
            "mode": "isolated-agent-validation",
            "artifact_storage": (
                "local-file"
                if isinstance(owned_storage, LocalFileObjectStorage)
                else "minio"
                if owned_storage is not None
                else "injected"
            ),
            "artifact_bucket": owned_storage.bucket if owned_storage is not None else "injected",
            "memory_enabled": service.memory_service.enabled,
            "memory_provider": service.memory_service.provider_name,
            "memory_app_id": service.memory_service.app_id,
        }

    @app.get("/api/v1/extensions/skills", tags=["extensions"])
    async def list_custom_skills() -> list[CustomSkillDetail]:
        """列出仅属于 8002 测试端数据库的自定义 Skill。"""

        assert extension_service is not None
        return await extension_service.list_skills()

    @app.get(
        "/api/v1/extensions/skill-options",
        response_model=list[AvailableSkillOption],
        tags=["extensions"],
    )
    async def available_custom_agent_skills() -> list[AvailableSkillOption]:
        """返回测试端可授权给自定义 Agent 的 Skill 多选目录。"""

        assert extension_service is not None
        return await extension_service.list_active_skill_options(extension_skills.list_all())

    @app.post(
        "/api/v1/extensions/business-skills/proposals",
        response_model=SkillContractProposal,
        tags=["extensions"],
    )
    async def propose_business_skill_contract(
        payload: BusinessSkillDraftInput,
    ) -> SkillContractProposal:
        """理解测试端用户描述并从受控模板中推荐 Skill 使用方式。"""

        assert extension_service is not None
        return await extension_service.propose_business_skill_contract(payload)

    @app.get(
        "/api/v1/extensions/business-skills/contract-options",
        tags=["extensions"],
    )
    async def business_skill_contract_options() -> Any:
        """返回测试端可确认的业务能力使用方式。"""

        assert extension_service is not None
        return extension_service.list_business_skill_contracts()

    @app.post(
        "/api/v1/extensions/business-skills/drafts",
        response_model=CustomSkillDetail,
        status_code=201,
        tags=["extensions"],
    )
    async def create_business_skill_draft(
        payload: BusinessSkillDraftInput,
    ) -> CustomSkillDetail:
        """按非开发者业务描述创建测试端 Skill 草稿。"""

        assert extension_service is not None
        return await extension_service.create_business_skill(payload)

    @app.put(
        "/api/v1/extensions/business-skills/{skill_id}/draft",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def update_business_skill_draft(
        skill_id: UUID, payload: BusinessSkillDraftInput
    ) -> CustomSkillDetail:
        """以业务语言创建测试端 Skill 的新草稿，发布前不改变现有运行版本。"""

        assert extension_service is not None
        return await extension_service.update_business_skill(skill_id, payload)

    @app.post(
        "/api/v1/extensions/business-agents/drafts",
        response_model=CustomAgentDetail,
        status_code=201,
        tags=["extensions"],
    )
    async def create_business_agent_draft(
        payload: BusinessAgentDraftInput,
    ) -> CustomAgentDetail:
        """按业务职责创建测试端 Agent 草稿，并从当前 Skill 目录推导契约。"""

        assert extension_service is not None
        options = await extension_service.list_active_skill_options(extension_skills.list_all())
        return await extension_service.create_business_agent(payload, available_skills=options)

    @app.put(
        "/api/v1/extensions/business-agents/{agent_id}/draft",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def update_business_agent_draft(
        agent_id: UUID, payload: BusinessAgentDraftInput
    ) -> CustomAgentDetail:
        """以业务职责和当前 Skill 目录创建测试端 Agent 的新草稿。"""

        assert extension_service is not None
        options = await extension_service.list_active_skill_options(extension_skills.list_all())
        return await extension_service.update_business_agent(
            agent_id, payload, available_skills=options
        )

    @app.post(
        "/api/v1/extensions/skills",
        response_model=CustomSkillDetail,
        status_code=201,
        tags=["extensions"],
    )
    async def create_custom_skill(payload: CustomSkillDefinitionInput) -> CustomSkillDetail:
        """在测试端创建 Skill 草稿；执行实现由已上传资源自动推导。"""

        assert extension_service is not None
        return await extension_service.create_skill(payload)

    @app.get(
        "/api/v1/extensions/skills/{skill_id}",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def get_custom_skill(skill_id: UUID) -> CustomSkillDetail:
        """读取测试端 Skill 的可编辑当前定义。"""

        assert extension_service is not None
        return await extension_service.get_skill(skill_id, include_definition=True)

    @app.put(
        "/api/v1/extensions/skills/{skill_id}",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def update_custom_skill(
        skill_id: UUID, payload: CustomSkillDefinitionInput
    ) -> CustomSkillDetail:
        """为测试端 Skill 创建新的草稿修订。"""

        assert extension_service is not None
        return await extension_service.update_skill(skill_id, payload)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/resources",
        response_model=CustomResourceSummary,
        status_code=201,
        tags=["extensions"],
    )
    async def add_custom_skill_resource(
        skill_id: UUID,
        relative_path: Annotated[str, Form()],
        resource: Annotated[UploadFile, File(description="测试端 Skill 的捆绑资源")],
    ) -> CustomResourceSummary:
        """把脚本或参考文件保存到测试端当前 Skill 草稿修订。"""

        assert extension_service is not None
        return await extension_service.add_skill_resource(
            skill_id,
            relative_path=relative_path,
            media_type=resource.content_type or "application/octet-stream",
            content=await resource.read(),
        )

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/contract-checks",
        response_model=CustomSkillContractCheck,
        tags=["extensions"],
    )
    async def check_custom_skill_contract(skill_id: UUID) -> CustomSkillContractCheck:
        """在测试端同一隔离 Runner 中检查脚本依赖和 Artifact 契约。"""

        assert extension_service is not None
        return await extension_service.check_skill_contract(skill_id)

    @app.post("/api/v1/extensions/skills/{skill_id}/function-adapter-proposal", tags=["extensions"])
    async def propose_function_adapter(skill_id: UUID) -> dict[str, object]:
        """在测试端静态展示脚本函数与标准资料字段。"""

        assert extension_service is not None
        return await extension_service.propose_script_function_adapter(skill_id)

    @app.put(
        "/api/v1/extensions/skills/{skill_id}/function-adapter",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def confirm_function_adapter(
        skill_id: UUID, payload: ScriptFunctionAdapter
    ) -> CustomSkillDetail:
        """保存测试端用户确认的函数适配，后续试运行与正式运行共用入口。"""

        assert extension_service is not None
        return await extension_service.confirm_script_function_adapter(skill_id, payload)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/output-mapping-proposals",
        response_model=ScriptOutputMappingProposal,
        tags=["extensions"],
    )
    async def propose_custom_skill_output_mapping(
        skill_id: UUID,
    ) -> ScriptOutputMappingProposal:
        """从测试端最近试运行观测返回字段并列出标准目标字段。"""

        assert extension_service is not None
        return await extension_service.propose_script_output_mapping(skill_id)

    @app.put(
        "/api/v1/extensions/skills/{skill_id}/output-bindings",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def confirm_custom_skill_output_mapping(
        skill_id: UUID, payload: list[ScriptOutputFieldBinding]
    ) -> CustomSkillDetail:
        """保存用户确认的测试端输出映射，并要求重新试运行。"""

        assert extension_service is not None
        return await extension_service.confirm_script_output_mapping(skill_id, payload)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/publish",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def publish_custom_skill(skill_id: UUID) -> CustomSkillDetail:
        """发布测试端 Skill，供测试端下一轮任务的动态 Registry 使用。"""

        assert extension_service is not None
        return await extension_service.publish_skill(skill_id)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/disable",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def disable_custom_skill(skill_id: UUID) -> CustomSkillDetail:
        """停用测试端 Skill，不影响正式端或历史任务。"""

        assert extension_service is not None
        return await extension_service.disable_skill(skill_id)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/enable",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def enable_custom_skill(skill_id: UUID) -> CustomSkillDetail:
        """重新启用测试端未修改的已发布 Skill。"""

        assert extension_service is not None
        return await extension_service.enable_skill(skill_id)

    @app.delete(
        "/api/v1/extensions/skills/{skill_id}",
        status_code=204,
        response_class=Response,
        tags=["extensions"],
    )
    async def delete_custom_skill(skill_id: UUID) -> Response:
        """删除已停用测试 Skill 的定义包，不影响测试端历史运行记录。"""

        assert extension_service is not None
        await extension_service.delete_skill(skill_id)
        return Response(status_code=204)

    @app.get("/api/v1/extensions/agents", tags=["extensions"])
    async def list_custom_agents() -> list[CustomAgentDetail]:
        """列出仅属于测试端数据库的自定义 Agent。"""

        assert extension_service is not None
        return await extension_service.list_agents()

    @app.post(
        "/api/v1/extensions/agents",
        response_model=CustomAgentDetail,
        status_code=201,
        tags=["extensions"],
    )
    async def create_custom_agent(payload: CustomAgentDefinitionInput) -> CustomAgentDetail:
        """创建测试端自定义专业 Agent 草稿及其唯一权限 YAML。"""

        assert extension_service is not None
        return await extension_service.create_agent(payload)

    @app.get(
        "/api/v1/extensions/agents/{agent_id}",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def get_custom_agent(agent_id: UUID) -> CustomAgentDetail:
        """读取测试端 Agent 草稿或已发布定义。"""

        assert extension_service is not None
        return await extension_service.get_agent(agent_id, include_definition=True)

    @app.put(
        "/api/v1/extensions/agents/{agent_id}",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def update_custom_agent(
        agent_id: UUID, payload: CustomAgentDefinitionInput
    ) -> CustomAgentDetail:
        """创建测试端 Agent 的新职责和权限草稿修订。"""

        assert extension_service is not None
        return await extension_service.update_agent(agent_id, payload)

    @app.post(
        "/api/v1/extensions/agents/{agent_id}/publish",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def publish_custom_agent(agent_id: UUID) -> CustomAgentDetail:
        """在引用的 Skill 都已发布后发布测试端 Agent。"""

        assert extension_service is not None
        return await extension_service.publish_agent(agent_id)

    @app.post(
        "/api/v1/extensions/agents/{agent_id}/disable",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def disable_custom_agent(agent_id: UUID) -> CustomAgentDetail:
        """停用测试端 Agent，使其不再参与后续测试任务路由。"""

        assert extension_service is not None
        return await extension_service.disable_agent(agent_id)

    @app.post(
        "/api/v1/extensions/agents/{agent_id}/enable",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def enable_custom_agent(agent_id: UUID) -> CustomAgentDetail:
        """重新启用测试端未修改的已发布 Agent。"""

        assert extension_service is not None
        return await extension_service.enable_agent(agent_id)

    @app.delete(
        "/api/v1/extensions/agents/{agent_id}",
        status_code=204,
        response_class=Response,
        tags=["extensions"],
    )
    async def delete_custom_agent(agent_id: UUID) -> Response:
        """删除已停用测试 Agent 的定义包，不影响测试端历史运行记录。"""

        assert extension_service is not None
        await extension_service.delete_agent(agent_id)
        return Response(status_code=204)

    @app.get("/api/v1/simulations/{task_id}/logs", tags=["simulation"])
    async def simulation_run_log(task_id: UUID) -> FileResponse:
        """下载隔离实验任务的公开 JSONL 日志，便于按子任务人工排查。"""

        path = service.run_logs.path_for(task_id)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="实验任务日志不存在")
        return FileResponse(
            path,
            media_type="application/x-ndjson; charset=utf-8",
            filename=path.name,
        )

    @app.get("/api/v1/published-assets/ontologies", tags=["published-assets"])
    async def simulation_published_ontologies(limit: int = 100) -> list[dict[str, Any]]:
        """列出测试环境中已由真实审核流程发布的本体。"""

        query = PlatformQueryService(service.database, service.artifacts)
        return await query.list_published_assets(
            artifact_type="PublishedOntology",
            limit=min(max(limit, 1), 500),
        )

    @app.get("/api/v1/published-assets/graphs", tags=["published-assets"])
    async def simulation_published_graphs(limit: int = 100) -> list[dict[str, Any]]:
        """列出测试环境中已由真实审核流程发布的图谱。"""

        query = PlatformQueryService(service.database, service.artifacts)
        return await query.list_published_assets(
            artifact_type="PublishedGraph",
            limit=min(max(limit, 1), 500),
        )

    @app.patch("/api/v1/published-assets/{artifact_id}", tags=["published-assets"])
    async def simulation_rename_published_asset(
        artifact_id: UUID, command: PublishedAssetRenameRequest
    ) -> dict[str, str]:
        """仅调整当前测试环境中的资产目录显示名称。"""

        manager = PublishedAssetManagementService(service.database.session_factory)
        try:
            return await manager.rename(artifact_id, command.title)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/v1/published-assets/{artifact_id}", tags=["published-assets"])
    async def simulation_retire_published_asset(artifact_id: UUID) -> dict[str, str]:
        """从测试环境资产目录移除版本，不影响正式环境。"""

        manager = PublishedAssetManagementService(service.database.session_factory)
        try:
            return await manager.retire(artifact_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/published-assets/{artifact_id}/content", tags=["published-assets"])
    async def simulation_published_asset_content(artifact_id: UUID) -> Response:
        """读取测试端真正发布的资产正文，不允许读取本轮导入副本。"""

        async with service.database.session_factory() as session:
            record = await SqlAlchemyArtifactRepository(session).get(artifact_id)
        if (
            record is None
            or record.ref.artifact_type not in {"PublishedOntology", "PublishedGraph"}
            or record.ref.producer_skill == "published_asset_import"
            or record.ref.status.value != "AVAILABLE"
            or not await PublishedAssetManagementService(
                service.database.session_factory
            ).is_listed(artifact_id)
        ):
            raise HTTPException(status_code=404, detail="已发布资产不存在")
        return Response(
            content=await service.artifacts.read_content(artifact_id), media_type=record.media_type
        )

    @app.get("/api/v1/published-assets/{artifact_id}/graph-preview", tags=["published-assets"])
    async def simulation_published_graph_preview(artifact_id: UUID) -> dict[str, Any]:
        """返回测试端资产库中 PublishedGraph 的只读交互预览投影。"""

        async with service.database.session_factory() as session:
            record = await SqlAlchemyArtifactRepository(session).get(artifact_id)
        if (
            record is None
            or record.ref.artifact_type != "PublishedGraph"
            or record.ref.producer_skill == "published_asset_import"
            or record.ref.status.value != "AVAILABLE"
            or not await PublishedAssetManagementService(
                service.database.session_factory
            ).is_listed(artifact_id)
        ):
            raise HTTPException(status_code=404, detail="已发布图谱不存在")
        return (await GraphPreviewService(service.artifacts).build(record)).model_dump(mode="json")

    @app.get(
        "/api/v1/simulations/{task_id}",
        response_model=SimulationRunReport,
        tags=["simulation"],
    )
    async def simulation_snapshot(task_id: UUID) -> SimulationRunReport:
        """恢复隔离任务的持久化快照，供页面刷新后继续查看和审核。

        该接口仅读取已提交的任务事实；不会重新执行总控、Worker 或任何业务 Skill。
        """

        pending_plan = await plan_drafts.load_current(task_id)
        if pending_plan is not None:
            stored = pending_plan.stored
            persisted = (
                await service.snapshot_task(task_id) if await service.task_exists(task_id) else None
            )
            snapshot = DemoTaskSnapshot(
                task_id=stored.task_id,
                conversation_id=stored.conversation_id,
                controller_result=ControllerRunResult(
                    decomposition=stored.decomposition,
                    shared_context=None,
                ),
                subtasks=stored.decomposition.subtasks,
                assignments=persisted.assignments if persisted is not None else [],
                runs=persisted.runs if persisted is not None else [],
                artifacts=persisted.artifacts if persisted is not None else [],
                master_task_status=(
                    persisted.master_task_status if persisted is not None else None
                ),
            )
        else:
            if not await service.task_exists(task_id):
                raise HTTPException(status_code=404, detail="实验任务不存在")
            snapshot = await service.snapshot_task(task_id)
        observed = await _present_snapshot(service, snapshot)
        timings = await _load_timing_entries(service, task_id)
        memory = SimulationMemoryReport(
            enabled=service.memory_service.enabled,
            provider=service.memory_service.provider_name,
            app_id=service.memory_service.app_id,
            recall_status="DISABLED",
        )
        input_decision = InputDecision(
            mode="RECOVERED",
            reason="页面刷新后从持久化存储恢复任务快照。",
        )
        plan_review = (
            _pending_plan_review(pending_plan)
            if pending_plan is not None
            else _approved_plan_review(snapshot)
        )
        return SimulationRunReport(
            observed=observed,
            memory=memory,
            timings=timings,
            reviews=[
                item.model_dump(mode="json") for item in await service.reviews.list_reviews(task_id)
            ],
            plan_review=plan_review,
            logs=_build_execution_logs(
                observed,
                memory,
                input_decision,
                snapshot.coordination_actions,
                timings,
                plan_review,
            ),
            progress=_calculate_progress(
                observed,
                snapshot.coordination_actions,
                plan_review,
                master_task_status=snapshot.master_task_status,
            ),
        )

    @app.get("/api/v1/simulations/{task_id}/events/stream", tags=["simulation"])
    async def simulation_event_stream(
        task_id: UUID,
        request: Request,
        after: int = 0,
        tail: bool = False,
        follow: bool = True,
    ) -> StreamingResponse:
        """通过 SSE 实时推送测试任务的公开 JSONL 执行记录。

        ``tail=true`` 适合在提交新一轮任务前订阅，只接收订阅后新增的记录；浏览器
        断线重连时则优先使用 ``Last-Event-ID``，确保不会漏掉断线期间的日志。
        ``follow=false`` 只返回当前已有记录，主要用于诊断和自动化测试。
        """

        last_event_id = request.headers.get("last-event-id", "").strip()
        try:
            resumed_cursor = int(last_event_id) if last_event_id else None
        except ValueError:
            resumed_cursor = None
        if resumed_cursor is not None:
            initial_cursor = max(resumed_cursor, 0)
        elif tail:
            initial_cursor = await service.run_logs.count(task_id)
        else:
            initial_cursor = max(after, 0)

        async def generate() -> AsyncIterator[str]:
            cursor = initial_cursor
            idle_rounds = 0
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                records, latest_cursor = await service.run_logs.read_after(
                    task_id,
                    after=cursor,
                )
                for line_number, record in records:
                    encoded = json.dumps(record, ensure_ascii=False)
                    yield (f"id: {line_number}\nevent: run_log\ndata: {encoded}\n\n")
                cursor = max(cursor, latest_cursor)
                if not follow:
                    return
                if records:
                    idle_rounds = 0
                else:
                    idle_rounds += 1
                    if idle_rounds >= 40:
                        idle_rounds = 0
                        yield "event: heartbeat\ndata: {}\n\n"
                await asyncio.sleep(0.25)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.post(
        "/api/v1/simulations/run",
        response_model=SimulationRunReport,
        tags=["simulation"],
    )
    async def run_simulation(
        question: Annotated[str, Form(min_length=1)],
        files: Annotated[
            list[UploadFile] | None,
            File(
                description=(
                    "可选真实业务文档；支持 PDF、DOCX、XLSX、PPTX、JPEG、PNG、"
                    "TIFF 和 WebP，只写入隔离实验环境。"
                )
            ),
        ] = None,
        ontology_file: Annotated[
            UploadFile | None,
            File(description="可选真实外部本体；只写入隔离实验环境。"),
        ] = None,
        published_ontology_artifact_id: Annotated[UUID | None, Form()] = None,
        published_graph_artifact_id: Annotated[UUID | None, Form()] = None,
        task_id: Annotated[UUID | None, Form()] = None,
        conversation_id: Annotated[UUID | None, Form()] = None,
        request_id: Annotated[UUID | None, Form()] = None,
        max_worker_runs: Annotated[int, Form(ge=0, le=100)] = 30,
    ) -> SimulationRunReport:
        """接收真实文档或纯文本任务，运行隔离 Agent 流程。"""

        resolved_task_id = task_id or uuid4()
        resolved_conversation_id = conversation_id or uuid4()
        resolved_request_id = request_id or uuid4()
        continuing_task = await service.task_exists(resolved_task_id)
        pending_review = await plan_drafts.load_current(resolved_task_id)
        if pending_review is not None:
            raise HTTPException(
                status_code=409,
                detail="当前候选计划尚未审核，请在审核窗口批准或提交修改意见",
            )
        selected_files = files or []
        # 后续无论是否上传业务文档，均可能追加外部本体或已发布资产；因此必须先
        # 初始化上传列表。此前纯文本新任务会跳过赋值，随后 uploaded.extend(...) 触发
        # UnboundLocalError，导致请求在进入总控 Agent 前直接返回 HTTP 500。
        uploaded: list[UploadedFile] = []
        if selected_files:
            input_decision = _real_document_input_decision(selected_files)
            uploaded = await _read_business_uploads(selected_files)
        else:
            input_decision = InputDecision(
                mode="REUSE_EXISTING_INPUT" if continuing_task else "TEXT_ONLY",
                reason=(
                    "当前输入没有新增真实文件，继续任务复用 SharedContext 中已有 Artifact。"
                    if continuing_task
                    else "当前输入没有上传真实文件，按纯文本任务交给总控 Agent。"
                ),
            )
        if ontology_file is not None:
            if published_ontology_artifact_id is not None:
                raise HTTPException(status_code=422, detail="外部本体与已发布本体只能选择一种")
            uploaded.append(
                UploadedFile(
                    filename=ontology_file.filename or "external-ontology.json",
                    media_type="application/json",
                    content=await ontology_file.read(),
                    artifact_type="ExternalOntology",
                )
            )
        uploaded.extend(
            await _selected_published_assets(
                service,
                ontology_artifact_id=published_ontology_artifact_id,
                graph_artifact_id=published_graph_artifact_id,
            )
        )
        snapshot = await service.submit_and_run(
            question=question,
            files=uploaded,
            task_id=resolved_task_id,
            conversation_id=resolved_conversation_id,
            request_id=resolved_request_id,
            # 总控只生成并校验候选计划。用户批准前不写 AgentSubTask/Assignment。
            execute_workers=False,
            max_worker_runs=max_worker_runs,
            publish_decomposition=False,
        )
        observed = await _present_snapshot(service, snapshot)
        timings = await _load_timing_entries(service, snapshot.task_id)
        memory = SimulationMemoryReport(
            enabled=snapshot.memory_enabled,
            provider=snapshot.memory_provider,
            app_id=snapshot.memory_app_id,
            recall_status=snapshot.memory_recall_status,
            recalled_memories=snapshot.recalled_memories,
            write_receipt=snapshot.memory_write_receipt,
        )
        if snapshot.controller_result.decomposition.requires_clarification:
            plan_review = None
        else:
            await plan_drafts.save(
                task_id=snapshot.task_id,
                conversation_id=snapshot.conversation_id,
                request_id=resolved_request_id,
                question=question,
                decomposition=snapshot.controller_result.decomposition,
                files=uploaded,
            )
            await service.run_logs.append(
                task_id=snapshot.task_id,
                record_type="PLAN_REVIEW_REQUESTED",
                actor="simulation_plan_review",
                payload={
                    "request_id": str(resolved_request_id),
                    "decomposition_revision": (
                        snapshot.controller_result.decomposition.subtasks[0].decomposition_revision
                        if snapshot.controller_result.decomposition.subtasks
                        else snapshot.controller_result.decomposition.task_spec.revision
                    ),
                    "subtask_count": len(snapshot.controller_result.decomposition.subtasks),
                    "publication_status": "NOT_PUBLISHED",
                },
            )
            loaded_plan = await plan_drafts.load_current(snapshot.task_id)
            assert loaded_plan is not None
            plan_review = _pending_plan_review(loaded_plan)
        return SimulationRunReport(
            observed=observed,
            memory=memory,
            timings=timings,
            reviews=[
                item.model_dump(mode="json")
                for item in await service.reviews.list_reviews(snapshot.task_id)
            ],
            plan_review=plan_review,
            logs=_build_execution_logs(
                observed,
                memory,
                input_decision,
                snapshot.coordination_actions,
                timings,
                plan_review,
            ),
            progress=_calculate_progress(
                observed,
                snapshot.coordination_actions,
                plan_review,
                master_task_status=snapshot.master_task_status,
            ),
        )

    @app.get(
        "/api/v1/simulations/{task_id}/reviews/{review_id}/materials",
        tags=["simulation"],
    )
    async def simulation_review_materials(task_id: UUID, review_id: UUID) -> dict[str, Any]:
        """返回隔离候选本体/图谱的审核引用和可直接预览的目标内容。"""

        materials = await service.reviews.get_review_materials(review_id)
        if materials.review.task_id != task_id:
            raise HTTPException(status_code=404, detail="实验审核不属于当前任务")
        async with service.database.session_factory() as session:
            records = await SqlAlchemyArtifactRepository(session).list_for_task(task_id)
        target_record = next(
            (item for item in records if item.ref.artifact_id == materials.target.artifact_id),
            None,
        )
        if target_record is None:
            raise HTTPException(status_code=404, detail="实验审核目标不存在")
        return {
            "review": materials.review.model_dump(mode="json"),
            "target": materials.target.model_dump(mode="json"),
            "target_content": await service.artifact_preview(target_record),
            "quality_reports": [item.model_dump(mode="json") for item in materials.quality_reports],
            "conflict_reports": [
                item.model_dump(mode="json") for item in materials.conflict_reports
            ],
            "evidence_artifacts": [
                item.model_dump(mode="json") for item in materials.evidence_artifacts
            ],
        }

    @app.get(
        "/api/v1/simulations/{task_id}/artifacts/{artifact_id}/graph-preview",
        tags=["simulation"],
    )
    async def simulation_graph_preview(task_id: UUID, artifact_id: UUID) -> dict[str, Any]:
        """返回测试任务内候选/已发布图谱的只读节点—边预览。"""

        async with service.database.session_factory() as session:
            record = await SqlAlchemyArtifactRepository(session).get(artifact_id)
        if record is None or record.ref.task_id != task_id:
            raise HTTPException(status_code=404, detail="测试任务内不存在该 Artifact")
        return (await GraphPreviewService(service.artifacts).build(record)).model_dump(mode="json")

    @app.post(
        "/api/v1/simulations/{task_id}/plan-review/approve",
        tags=["simulation"],
    )
    async def approve_simulation_plan(
        task_id: UUID,
        payload: SimulationPlanApprovalRequest,
    ) -> dict[str, Any]:
        """批准候选计划和用户顺序，然后才原子发布子任务并执行。"""

        pending = await plan_drafts.load_current(task_id)
        if pending is None:
            raise HTTPException(status_code=409, detail="当前没有待审核的候选计划")
        decomposition = pending.stored.decomposition
        original_ids = [item.subtask_id for item in decomposition.subtasks]
        if len(payload.ordered_subtask_ids) != len(set(payload.ordered_subtask_ids)):
            raise HTTPException(status_code=422, detail="子任务顺序不能包含重复 ID")
        if set(payload.ordered_subtask_ids) != set(original_ids):
            raise HTTPException(status_code=422, detail="子任务顺序必须完整包含当前候选计划")
        dependency_violations = _dependency_order_violations(
            decomposition.subtasks,
            payload.ordered_subtask_ids,
        )
        if dependency_violations:
            raise HTTPException(
                status_code=422,
                detail=(
                    "审核顺序必须遵守前置依赖；只能调整同一执行阶段内的优先级："
                    + "；".join(dependency_violations)
                ),
            )
        by_id = {item.subtask_id: item for item in decomposition.subtasks}
        ordered_subtasks = [
            by_id[subtask_id].model_copy(
                update={"priority": len(payload.ordered_subtask_ids) - index}
            )
            for index, subtask_id in enumerate(payload.ordered_subtask_ids)
        ]
        approved_decomposition = decomposition.model_copy(update={"subtasks": ordered_subtasks})
        await service.publish_planned_decomposition(
            decomposition=approved_decomposition,
            conversation_id=pending.stored.conversation_id,
            request_id=pending.stored.request_id,
            files=pending.files,
        )
        await service.record_approved_plan_message(
            task_id=task_id,
            conversation_id=pending.stored.conversation_id,
            message=pending.stored.question,
            decomposition=approved_decomposition,
        )
        await plan_drafts.mark(task_id, pending.stored.request_id, "PUBLISHED")
        await service.run_logs.append(
            task_id=task_id,
            record_type="PLAN_REVIEW_APPROVED",
            actor=payload.reviewer_id,
            payload={
                "request_id": str(pending.stored.request_id),
                "decomposition_revision": (
                    approved_decomposition.subtasks[0].decomposition_revision
                    if approved_decomposition.subtasks
                    else approved_decomposition.task_spec.revision
                ),
                "ordered_subtask_ids": [str(item) for item in payload.ordered_subtask_ids],
                "publication_status": "PUBLISHED",
            },
        )
        plan_review = SimulationPlanReview(
            decomposition_revision=(
                approved_decomposition.subtasks[0].decomposition_revision
                if approved_decomposition.subtasks
                else approved_decomposition.task_spec.revision
            ),
            status="APPROVED",
            task_intent=approved_decomposition.task_intent.value,
            business_goal=approved_decomposition.task_spec.business_goal,
            subtask_count=len(approved_decomposition.subtasks),
        )
        snapshot = await service.resume_ready_workers(
            task_id=task_id,
            max_worker_runs=30,
        )
        observed = await _present_snapshot(service, snapshot)
        timings = await _load_timing_entries(service, task_id)
        memory = SimulationMemoryReport(
            enabled=service.memory_service.enabled,
            provider=service.memory_service.provider_name,
            app_id=service.memory_service.app_id,
            recall_status="DISABLED",
        )
        input_decision = InputDecision(
            mode="PLAN_APPROVED",
            reason="用户已批准总控意图和当前子任务计划。",
        )
        progress = _calculate_progress(
            observed,
            snapshot.coordination_actions,
            plan_review,
            master_task_status=snapshot.master_task_status,
        )
        return {
            "next_action": (
                snapshot.coordination_actions[-1] if snapshot.coordination_actions else "NOOP"
            ),
            "plan_review": plan_review.model_dump(mode="json"),
            "observed": observed.model_dump(mode="json"),
            "reviews": [
                item.model_dump(mode="json") for item in await service.reviews.list_reviews(task_id)
            ],
            "timings": [item.model_dump(mode="json") for item in timings],
            "logs": [
                item.model_dump(mode="json")
                for item in _build_execution_logs(
                    observed,
                    memory,
                    input_decision,
                    snapshot.coordination_actions,
                    timings,
                    plan_review,
                )
            ],
            "progress": progress.model_dump(mode="json"),
        }

    @app.post(
        "/api/v1/simulations/{task_id}/plan-review/revise",
        response_model=SimulationRunReport,
        tags=["simulation"],
    )
    async def revise_simulation_plan(
        task_id: UUID,
        payload: SimulationPlanRevisionRequest,
    ) -> SimulationRunReport:
        """把用户修改意见作为同一 MasterTask 的新一轮对话交回总控重新规划。

        本入口不会直接篡改已发布 AgentSubTask。总控仍是唯一能够发布正式子任务的
        Agent；新 revision 发布后再次创建人工计划审核，专业 Agent 仍不会提前执行。
        """

        current_plan = await plan_drafts.load_current(task_id)
        if current_plan is None:
            raise HTTPException(status_code=409, detail="当前没有可修改的待审核计划")
        combined_feedback = (
            f"原始任务：{current_plan.stored.question}\n"
            f"用户对候选计划的修改意见：{payload.feedback}"
        )
        task_exists = await service.task_exists(task_id)
        files_for_revision = [] if task_exists else current_plan.files
        new_request_id = uuid4()
        snapshot = await service.submit_and_run(
            question=combined_feedback,
            files=files_for_revision,
            task_id=task_id,
            conversation_id=current_plan.stored.conversation_id,
            request_id=new_request_id,
            execute_workers=False,
            max_worker_runs=0,
            publish_decomposition=False,
        )
        await plan_drafts.save(
            task_id=task_id,
            conversation_id=current_plan.stored.conversation_id,
            request_id=new_request_id,
            question=combined_feedback,
            decomposition=snapshot.controller_result.decomposition,
            files=current_plan.files if not task_exists else [],
        )
        await plan_drafts.mark(task_id, current_plan.stored.request_id, "SUPERSEDED")
        await service.run_logs.append(
            task_id=task_id,
            record_type="PLAN_REVIEW_REVISION_REQUESTED",
            actor=payload.reviewer_id,
            payload={
                "superseded_request_id": str(current_plan.stored.request_id),
                "new_request_id": str(new_request_id),
            },
        )
        loaded_plan = await plan_drafts.load_current(task_id)
        assert loaded_plan is not None
        plan_review = _pending_plan_review(loaded_plan)
        observed = await _present_snapshot(service, snapshot)
        timings = await _load_timing_entries(service, task_id)
        memory = SimulationMemoryReport(
            enabled=snapshot.memory_enabled,
            provider=snapshot.memory_provider,
            app_id=snapshot.memory_app_id,
            recall_status=snapshot.memory_recall_status,
            recalled_memories=snapshot.recalled_memories,
            write_receipt=snapshot.memory_write_receipt,
        )
        input_decision = InputDecision(
            mode="PLAN_REVISED",
            reason="用户在计划审核窗口提交修改意见，总控已重新生成当前计划。",
            matched_evidence=payload.feedback,
        )
        return SimulationRunReport(
            observed=observed,
            memory=memory,
            timings=timings,
            reviews=[
                item.model_dump(mode="json") for item in await service.reviews.list_reviews(task_id)
            ],
            plan_review=plan_review,
            logs=_build_execution_logs(
                observed,
                memory,
                input_decision,
                snapshot.coordination_actions,
                timings,
                plan_review,
            ),
            progress=_calculate_progress(
                observed,
                snapshot.coordination_actions,
                plan_review,
                master_task_status=snapshot.master_task_status,
            ),
        )

    @app.post(
        "/api/v1/simulations/{task_id}/resume",
        tags=["simulation"],
    )
    async def resume_simulation_task(
        task_id: UUID,
    ) -> dict[str, Any]:
        """在页面刷新、旧请求中断或审核已提交后继续现有 Assignment。

        本入口不重新调用总控拆解计划，只恢复当前任务已经发布且满足依赖/审核门禁的
        Assignment。全部计划终止后，既有 TaskCoordinator 仍会执行最终目标检查。
        """

        if not await service.task_exists(task_id):
            raise HTTPException(status_code=404, detail="实验任务不存在")
        pending_plan = await plan_drafts.load_current(task_id)
        if pending_plan is not None:
            raise HTTPException(
                status_code=409,
                detail="请先在审核窗口批准候选意图和子任务，再执行专业 Agent",
            )
        plan_review = None
        await service.run_logs.append(
            task_id=task_id,
            record_type="MANUAL_RESUME_STARTED",
            actor="simulation-user",
            payload={},
        )
        snapshot = await service.resume_ready_workers(
            task_id=task_id,
            max_worker_runs=30,
        )
        observed = await _present_snapshot(service, snapshot)
        timings = await _load_timing_entries(service, task_id)
        return {
            "next_action": (
                snapshot.coordination_actions[-1] if snapshot.coordination_actions else "NOOP"
            ),
            "observed": observed.model_dump(mode="json"),
            "reviews": [
                item.model_dump(mode="json") for item in await service.reviews.list_reviews(task_id)
            ],
            "timings": [item.model_dump(mode="json") for item in timings],
            "plan_review": (
                plan_review.model_dump(mode="json") if plan_review is not None else None
            ),
            "progress": _calculate_progress(
                observed,
                snapshot.coordination_actions,
                plan_review,
                master_task_status=snapshot.master_task_status,
            ).model_dump(mode="json"),
        }

    @app.post(
        "/api/v1/simulations/{task_id}/reviews/{review_id}/resolve",
        tags=["simulation"],
    )
    async def resolve_simulation_review(
        task_id: UUID,
        review_id: UUID,
        payload: ResolveHumanReviewCommand,
    ) -> dict[str, Any]:
        """记录测试用户对候选本体或图谱的批准、拒绝或结构化修改。"""

        if payload.review_id != review_id:
            raise HTTPException(status_code=422, detail="路径 review_id 与请求体不一致")
        review = await service.reviews.get_review(review_id)
        if review is None or review.task_id != task_id:
            raise HTTPException(status_code=404, detail="实验审核不存在")
        resolved = await service.reviews.resolve_review(payload)
        await service.run_logs.append(
            task_id=task_id,
            record_type="HUMAN_REVIEW_RESOLVED",
            actor=payload.reviewer_id,
            payload={
                "review_id": str(review_id),
                "target_type": resolved.target_type.value,
                "target_artifact_id": str(resolved.target_artifact_id),
                "status": resolved.status.value,
                "action": payload.action.value,
            },
        )
        if payload.action.value in {"SUBMIT_CHANGES", "REJECT"}:
            snapshot = await service.submit_and_run(
                question=(
                    "用户已提交候选本体或候选图谱的审核修改/拒绝。请读取最新 "
                    "ChangeSet 与审核反馈，只规划应用修改、重新送审或必要返工任务；"
                    "不得沿用旧 Approval。"
                ),
                files=[],
                task_id=task_id,
                request_id=uuid4(),
                execute_workers=True,
                max_worker_runs=30,
                internal_continuation=True,
            )
            action = "CONTROLLER_REPLANNED"
        else:
            await service.run_logs.append(
                task_id=task_id,
                record_type="REVIEW_RESUME_STARTED",
                actor="task_coordinator",
                payload={"review_id": str(review_id)},
            )
            snapshot = await service.resume_ready_workers(
                task_id=task_id,
                max_worker_runs=30,
            )
            action = (
                snapshot.coordination_actions[-1]
                if snapshot.coordination_actions
                else await TaskCoordinator(service).advance(task_id)  # type: ignore[arg-type]
            )
        observed = await _present_snapshot(service, snapshot)
        timings = await _load_timing_entries(service, task_id)
        return {
            "review": resolved.model_dump(mode="json"),
            "next_action": action,
            "observed": observed.model_dump(mode="json"),
            "reviews": [
                item.model_dump(mode="json") for item in await service.reviews.list_reviews(task_id)
            ],
            "timings": [item.model_dump(mode="json") for item in timings],
            "progress": _calculate_progress(
                observed,
                snapshot.coordination_actions,
                master_task_status=snapshot.master_task_status,
            ).model_dump(mode="json"),
        }

    return app
