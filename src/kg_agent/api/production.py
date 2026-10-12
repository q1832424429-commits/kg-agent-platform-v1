"""知识图谱构建 Agent 平台 V1 的正式 HTTP API。"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, cast
from uuid import UUID, uuid4

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import text

from kg_agent.domain.enums import MasterTaskStatus, ReviewOption, TaskEventType
from kg_agent.domain.memory_models import MemoryCreateRequest, MemorySearchRequest
from kg_agent.exceptions import KGAgentError
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
    EntityRecordAdapter,
    ScriptFunctionAdapter,
    ScriptInputFieldBinding,
    ScriptInputMappingProposal,
    ScriptOutputFieldBinding,
    ScriptOutputMappingProposal,
)
from kg_agent.infrastructure.database.repositories import (
    SqlAlchemyAssignmentRepository,
    SqlAlchemyContextRepository,
    SqlAlchemyEventRepository,
    SqlAlchemyTaskRepository,
)
from kg_agent.runtime import RuntimeContainer, build_runtime
from kg_agent.services.conversation_service import ConversationService
from kg_agent.services.graph_preview_service import GraphPreviewService
from kg_agent.services.platform_query_service import PlatformQueryService
from kg_agent.services.published_asset_management_service import PublishedAssetManagementService
from kg_agent.services.review_commands import CreateHumanReviewCommand, ResolveHumanReviewCommand
from kg_agent.services.task_coordinator import TaskCoordinator
from kg_agent.services.task_orchestration_service import IncomingFile
from kg_agent.services.task_state_service import TaskStateService


class TaskMessageRequest(BaseModel):
    """接收新建、继续、修改或重试任务的用户自然语言。"""

    message: str = Field(min_length=1)
    task_id: UUID | None = None
    conversation_id: UUID | None = None
    request_id: UUID = Field(default_factory=uuid4)
    remember_long_term: bool = Field(
        default=False,
        description="用户是否明确授权把本轮公开内容沉淀到 Mem0 长期记忆。",
    )


class TaskAcceptedResponse(BaseModel):
    """返回总控本轮意图识别、任务拆解和持久化状态。"""

    task_id: UUID
    conversation_id: UUID
    task_intent: str
    requires_clarification: bool
    clarification_question: str | None = None
    task_spec: dict[str, Any]
    subtasks: list[dict[str, Any]] = Field(default_factory=list)


class ConversationRenameRequest(BaseModel):
    """接收侧栏会话标题修改，业务任务标题与 TaskSpec 保持原样。"""

    title: str = Field(min_length=1, max_length=120)


class PublishedAssetRenameRequest(BaseModel):
    """修改发布资产目录显示名称；不变更已审核 JSON 正文。"""

    title: str = Field(min_length=1, max_length=120)


def _container(request: Request) -> RuntimeContainer:
    """取得应用启动时已完成校验的生产运行容器。"""
    return cast(RuntimeContainer, request.app.state.runtime)


def _conversation_service(request: Request) -> ConversationService:
    """使用当前环境的隔离数据库、对象存储和 Mem0 命名空间构造会话服务。"""

    container = _container(request)
    return ConversationService(
        container.database.session_factory,
        storage=container.storage,
        memory=container.memory,
        run_logs=container.run_logs,
    )


def _accepted(snapshot: Any) -> TaskAcceptedResponse:
    """将内部提交快照转换为稳定的正式 API 结果。"""
    result = snapshot.controller_result
    return TaskAcceptedResponse(
        task_id=snapshot.task_id,
        conversation_id=snapshot.conversation_id,
        task_intent=result.decomposition.task_intent.value,
        requires_clarification=result.decomposition.requires_clarification,
        clarification_question=result.decomposition.clarification_question,
        task_spec=result.decomposition.task_spec.model_dump(mode="json"),
        subtasks=[item.model_dump(mode="json") for item in result.decomposition.subtasks],
    )


_DOCUMENT_MEDIA_TYPES_BY_SUFFIX = {
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


def _canonical_document_media_type(filename: str, declared_media_type: str | None) -> str:
    """按 V1 支持的文件扩展名规范浏览器可能误报的普通文档 MIME。

    Windows 浏览器或代理可能把 DOCX、XLSX、PPTX 声明成
    ``application/octet-stream`` 或 ``application/zip``。这些 Office Open XML 文件
    本质上是 ZIP 容器，但业务类型应由受支持的扩展名确定。未知扩展名仍保留客户端
    声明，随后由 ArtifactService 的白名单统一拒绝，避免把旧版 DOC 误当成 DOCX。
    """

    suffix = Path(filename).suffix.lower()
    return _DOCUMENT_MEDIA_TYPES_BY_SUFFIX.get(
        suffix,
        declared_media_type or "application/octet-stream",
    )


async def _uploads(files: list[UploadFile]) -> list[IncomingFile]:
    """读取普通文件，并在进入 Artifact 白名单前规范受支持格式的 MIME。"""
    return [
        IncomingFile(
            filename=item.filename or "unnamed",
            media_type=_canonical_document_media_type(
                item.filename or "unnamed",
                item.content_type,
            ),
            content=await item.read(),
        )
        for item in files
    ]


async def _ontology_upload(file: UploadFile | None) -> list[IncomingFile]:
    """把专用本体窗口中的单个 JSON 文件标记为 ExternalOntology 输入。

    具体 JSON 结构由 Artifact Registry 在接入事务前统一校验；API 层只保留来源文件
    和明确业务类型，避免把外部本体误当作待解析的普通文档。
    """

    if file is None:
        return []
    return [
        IncomingFile(
            filename=file.filename or "external-ontology.json",
            media_type="application/json",
            content=await file.read(),
            artifact_type="ExternalOntology",
        )
    ]


async def _published_asset_inputs(
    container: RuntimeContainer,
    *,
    ontology_artifact_id: UUID | None,
    graph_artifact_id: UUID | None,
) -> list[IncomingFile]:
    """把用户选择的已发布资产转换为当前任务的受控输入副本。

    已发布本体保持原内容与版本；已发布图谱去掉发布元数据后成为新的 CandidateGraph
    基线。两者都携带原 Artifact ID，真正落库时由 ArtifactService 校验来源内容。
    """

    query = PlatformQueryService(container.database, container.artifacts)
    selected: list[IncomingFile] = []
    if ontology_artifact_id is not None:
        record = await query.artifact(ontology_artifact_id)
        if record is None or record.ref.artifact_type != "PublishedOntology":
            raise HTTPException(status_code=422, detail="选择的本体不是可用 PublishedOntology")
        selected.append(
            IncomingFile(
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
            IncomingFile(
                filename=f"published-graph-{graph_artifact_id}.json",
                media_type="application/json",
                content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                artifact_type="CandidateGraph",
                source_artifact_id=graph_artifact_id,
            )
        )
    return selected


def create_app(runtime: RuntimeContainer | None = None, *, manage_lifespan: bool = True) -> FastAPI:
    """创建正式应用；默认严格要求 PostgreSQL、MinIO 与 DeepSeek 配置。"""

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        resolved = runtime or build_runtime()
        app.state.runtime = resolved
        if manage_lifespan:
            await resolved.startup()
        try:
            yield
        finally:
            if manage_lifespan:
                await resolved.shutdown()

    app = FastAPI(title="知识图谱构建 Agent 平台", version="1.0.0", lifespan=lifespan)
    if runtime is not None:
        app.state.runtime = runtime

    # 工作台是正式 API 的轻量同源客户端，不引入第二套前端运行时或业务状态。
    web_root = Path(__file__).resolve().parents[1] / "web"
    app.mount(
        "/workbench-assets",
        StaticFiles(directory=web_root),
        name="workbench-assets",
    )

    @app.get("/", include_in_schema=False)
    @app.get("/workbench", include_in_schema=False)
    async def workbench() -> FileResponse:
        """返回简易 Agent 工作台；全部业务操作仍由正式 API 和领域服务完成。"""
        return FileResponse(web_root / "index.html", media_type="text/html; charset=utf-8")

    @app.get("/extensions", include_in_schema=False)
    async def extension_workbench() -> FileResponse:
        """返回自定义 Agent 与 Skill 定义包的独立管理页面。"""

        return FileResponse(web_root / "extensions.html", media_type="text/html; charset=utf-8")

    @app.get("/published-assets", include_in_schema=False)
    async def published_assets_workbench() -> FileResponse:
        """返回已发布本体和图谱的用户侧资产管理页。"""

        return FileResponse(
            web_root / "published_assets.html", media_type="text/html; charset=utf-8"
        )

    @app.exception_handler(KGAgentError)
    async def business_error(_: Request, exc: KGAgentError) -> JSONResponse:
        """将可预期业务失败转换为结构化冲突响应。"""
        return JSONResponse(
            status_code=409, content={"error_code": type(exc).__name__, "message": str(exc)}
        )

    @app.get("/health/live", tags=["health"])
    async def live() -> dict[str, str]:
        """进程存活探针，不访问外部依赖。"""
        return {"status": "ok"}

    @app.get("/health/ready", tags=["health"])
    async def ready(request: Request) -> dict[str, str]:
        """数据库和启动期对象存储检查均通过后返回就绪。"""
        async with _container(request).database.session_factory() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ready"}

    @app.post("/api/v1/tasks", response_model=TaskAcceptedResponse, status_code=202, tags=["tasks"])
    async def create_task(payload: TaskMessageRequest, request: Request) -> TaskAcceptedResponse:
        """仅凭用户问题新建任务；若信息不足，总控会返回澄清问题。"""
        snapshot = await _container(request).orchestration.submit_and_run(
            question=payload.message,
            files=[],
            task_id=payload.task_id,
            conversation_id=payload.conversation_id,
            request_id=payload.request_id,
            execute_workers=False,
            remember_long_term=payload.remember_long_term,
        )
        return _accepted(snapshot)

    @app.post(
        "/api/v1/tasks/upload", response_model=TaskAcceptedResponse, status_code=202, tags=["tasks"]
    )
    async def create_task_with_files(
        request: Request,
        message: Annotated[str, Form()],
        files: Annotated[
            list[UploadFile] | None,
            File(description="可选普通业务文档；每个文件登记为 RawDocument。"),
        ] = None,
        ontology_file: Annotated[
            UploadFile | None,
            File(description="可选外部本体；只接收一个符合 V1 契约的 JSON 文件。"),
        ] = None,
        published_ontology_artifact_id: Annotated[UUID | None, Form()] = None,
        published_graph_artifact_id: Annotated[UUID | None, Form()] = None,
        task_id: Annotated[UUID | None, Form()] = None,
        conversation_id: Annotated[UUID | None, Form()] = None,
        request_id: Annotated[UUID | None, Form()] = None,
        remember_long_term: Annotated[bool, Form()] = False,
    ) -> TaskAcceptedResponse:
        """使用自然语言、可选原始文档和可选外部本体新建任务。"""
        container = _container(request)
        if ontology_file is not None and published_ontology_artifact_id is not None:
            raise HTTPException(status_code=422, detail="外部本体与已发布本体只能选择一种")
        snapshot = await container.orchestration.submit_and_run(
            question=message,
            files=(
                (await _uploads(files or []))
                + (await _ontology_upload(ontology_file))
                + await _published_asset_inputs(
                    container,
                    ontology_artifact_id=published_ontology_artifact_id,
                    graph_artifact_id=published_graph_artifact_id,
                )
            ),
            task_id=task_id,
            conversation_id=conversation_id,
            request_id=request_id,
            execute_workers=False,
            remember_long_term=remember_long_term,
        )
        return _accepted(snapshot)

    @app.post(
        "/api/v1/tasks/{task_id}/messages",
        response_model=TaskAcceptedResponse,
        status_code=202,
        tags=["tasks"],
    )
    async def continue_task(
        task_id: UUID, payload: TaskMessageRequest, request: Request
    ) -> TaskAcceptedResponse:
        """在同一 SharedContext 中继续或修改已有任务。"""
        snapshot = await _container(request).orchestration.submit_and_run(
            question=payload.message,
            files=[],
            task_id=task_id,
            conversation_id=payload.conversation_id,
            request_id=payload.request_id,
            execute_workers=False,
            remember_long_term=payload.remember_long_term,
        )
        return _accepted(snapshot)

    @app.post(
        "/api/v1/tasks/{task_id}/messages/upload",
        response_model=TaskAcceptedResponse,
        status_code=202,
        tags=["tasks"],
    )
    async def continue_task_with_files(
        task_id: UUID,
        request: Request,
        message: Annotated[str, Form()],
        files: Annotated[
            list[UploadFile] | None,
            File(description="可选追加业务文档；每个文件登记为 RawDocument。"),
        ] = None,
        ontology_file: Annotated[
            UploadFile | None,
            File(description="可选追加外部本体；只接收一个符合 V1 契约的 JSON 文件。"),
        ] = None,
        published_ontology_artifact_id: Annotated[UUID | None, Form()] = None,
        published_graph_artifact_id: Annotated[UUID | None, Form()] = None,
        conversation_id: Annotated[UUID | None, Form()] = None,
        request_id: Annotated[UUID | None, Form()] = None,
        remember_long_term: Annotated[bool, Form()] = False,
    ) -> TaskAcceptedResponse:
        """在已有任务中追加文档或外部本体，并由总控判断继续还是修改。"""
        container = _container(request)
        if ontology_file is not None and published_ontology_artifact_id is not None:
            raise HTTPException(status_code=422, detail="外部本体与已发布本体只能选择一种")
        snapshot = await container.orchestration.submit_and_run(
            question=message,
            files=(
                (await _uploads(files or []))
                + (await _ontology_upload(ontology_file))
                + await _published_asset_inputs(
                    container,
                    ontology_artifact_id=published_ontology_artifact_id,
                    graph_artifact_id=published_graph_artifact_id,
                )
            ),
            task_id=task_id,
            conversation_id=conversation_id,
            request_id=request_id,
            execute_workers=False,
            remember_long_term=remember_long_term,
        )
        return _accepted(snapshot)

    @app.get("/api/v1/tasks", tags=["tasks"])
    async def list_tasks(request: Request, limit: int = 100) -> Any:
        """查询最近任务摘要。"""
        return jsonable_encoder(
            await PlatformQueryService(
                _container(request).database, _container(request).artifacts
            ).list_tasks(limit=min(max(limit, 1), 500))
        )

    @app.get("/api/v1/conversations", tags=["conversations"])
    async def list_conversations(request: Request, limit: int = 100) -> Any:
        """按会话返回侧栏目录，一个会话可以包含多个 MasterTask。"""

        return jsonable_encoder(
            await _conversation_service(request).list_conversations(limit=min(max(limit, 1), 500))
        )

    @app.get("/api/v1/conversations/{conversation_id}", tags=["conversations"])
    async def get_conversation(conversation_id: UUID, request: Request) -> Any:
        """读取会话公开消息和任务摘要，供刷新工作台后恢复聊天。"""

        snapshot = await _conversation_service(request).conversation_snapshot(conversation_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail="会话不存在")
        return jsonable_encoder(snapshot)

    @app.patch("/api/v1/conversations/{conversation_id}", tags=["conversations"])
    async def rename_conversation(
        conversation_id: UUID, command: ConversationRenameRequest, request: Request
    ) -> Any:
        """重命名会话；不修改其中任务的业务目标或历史消息。"""

        try:
            result = await _conversation_service(request).rename(conversation_id, command.title)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        return jsonable_encoder(result)

    @app.delete("/api/v1/conversations/{conversation_id}", tags=["conversations"])
    async def delete_conversation(conversation_id: UUID, request: Request) -> Any:
        """按 V1 固定用户边界删除私有会话，保留正式发布资产和共享记忆。"""

        try:
            result = await _conversation_service(request).delete(
                conversation_id, actor_user_id="default-user"
            )
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        return jsonable_encoder(result)

    @app.get("/api/v1/conversations/{conversation_id}/events/stream", tags=["conversations"])
    async def conversation_event_stream(
        conversation_id: UUID, request: Request, after: int = 0
    ) -> StreamingResponse:
        """连续推送会话内全部任务事件，浏览器按全局事件序号断线续传。"""

        service = _conversation_service(request)
        if await service.conversation_snapshot(conversation_id) is None:
            raise HTTPException(status_code=404, detail="会话不存在")

        async def generate() -> AsyncIterator[str]:
            last_event_id = request.headers.get("last-event-id", "").strip()
            try:
                resumed_cursor = int(last_event_id) if last_event_id else after
            except ValueError:
                resumed_cursor = after
            cursor = max(after, resumed_cursor, 0)
            idle_rounds = 0
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                events = await service.events_after(conversation_id, after=cursor)
                for event in events:
                    cursor = max(cursor, int(event["sequence"]))
                    encoded = json.dumps(jsonable_encoder(event), ensure_ascii=False)
                    yield (
                        f"id: {event['sequence']}\n"
                        f"event: {event['event_type']}\n"
                        f"data: {encoded}\n\n"
                    )
                idle_rounds = 0 if events else idle_rounds + 1
                if idle_rounds >= 20:
                    idle_rounds = 0
                    yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/v1/published-assets/ontologies", tags=["published-assets"])
    async def list_published_ontologies(request: Request, limit: int = 100) -> Any:
        """返回已发布本体的全局资产库目录及其预览所需 Artifact ID。"""

        return jsonable_encoder(
            await PlatformQueryService(
                _container(request).database, _container(request).artifacts
            ).list_published_assets(
                artifact_type="PublishedOntology", limit=min(max(limit, 1), 500)
            )
        )

    @app.get("/api/v1/published-assets/graphs", tags=["published-assets"])
    async def list_published_graphs(request: Request, limit: int = 100) -> Any:
        """返回已发布图谱的全局资产库目录及其预览所需 Artifact ID。"""

        return jsonable_encoder(
            await PlatformQueryService(
                _container(request).database, _container(request).artifacts
            ).list_published_assets(artifact_type="PublishedGraph", limit=min(max(limit, 1), 500))
        )

    @app.patch("/api/v1/published-assets/{artifact_id}", tags=["published-assets"])
    async def rename_published_asset(
        artifact_id: UUID, command: PublishedAssetRenameRequest, request: Request
    ) -> Any:
        """修改目录显示名称，发布版本内容与校验和不变。"""

        service = PublishedAssetManagementService(_container(request).database.session_factory)
        try:
            return await service.rename(artifact_id, command.title)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.delete("/api/v1/published-assets/{artifact_id}", tags=["published-assets"])
    async def retire_published_asset(artifact_id: UUID, request: Request) -> Any:
        """删除目录项并禁止后续新任务复用，保留既有引用的不可变正文。"""

        service = PublishedAssetManagementService(_container(request).database.session_factory)
        try:
            return await service.retire(artifact_id)
        except LookupError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.get("/api/v1/published-assets/{artifact_id}/content", tags=["published-assets"])
    async def get_published_asset_content(artifact_id: UUID, request: Request) -> Response:
        """读取正式发布资产的 JSON 正文，拒绝任务内导入副本和其他 Artifact。"""

        container = _container(request)
        query = PlatformQueryService(container.database, container.artifacts)
        record = await query.artifact(artifact_id)
        if (
            record is None
            or record.ref.artifact_type not in {"PublishedOntology", "PublishedGraph"}
            or record.ref.producer_skill == "published_asset_import"
            or record.ref.status.value != "AVAILABLE"
            or not await PublishedAssetManagementService(
                container.database.session_factory
            ).is_listed(artifact_id)
        ):
            raise HTTPException(status_code=404, detail="已发布资产不存在")
        return Response(
            content=await query.artifact_content(artifact_id), media_type=record.media_type
        )

    @app.get("/api/v1/published-assets/{artifact_id}/graph-preview", tags=["published-assets"])
    async def get_published_graph_preview(artifact_id: UUID, request: Request) -> Any:
        """返回资产库中正式 PublishedGraph 的只读交互预览投影。"""

        container = _container(request)
        record = await PlatformQueryService(container.database, container.artifacts).artifact(
            artifact_id
        )
        if (
            record is None
            or record.ref.artifact_type != "PublishedGraph"
            or record.ref.producer_skill == "published_asset_import"
            or record.ref.status.value != "AVAILABLE"
            or not await PublishedAssetManagementService(
                container.database.session_factory
            ).is_listed(artifact_id)
        ):
            raise HTTPException(status_code=404, detail="已发布图谱不存在")
        return jsonable_encoder(await GraphPreviewService(container.artifacts).build(record))

    @app.get("/api/v1/tasks/{task_id}", tags=["tasks"])
    async def get_task(task_id: UUID, request: Request) -> Any:
        """返回任务的完整公开状态、分配、运行结果、审核与事件。"""
        value = await PlatformQueryService(
            _container(request).database, _container(request).artifacts
        ).task_snapshot(task_id)
        if value is None:
            raise HTTPException(status_code=404, detail="任务不存在")
        return jsonable_encoder(value)

    @app.post("/api/v1/tasks/{task_id}/cancel", tags=["tasks"])
    async def cancel_task(task_id: UUID, request: Request) -> Any:
        """确定性取消尚未结束的 MasterTask。"""
        container = _container(request)
        async with container.database.session_factory() as session, session.begin():
            service = TaskStateService(
                task_repository=SqlAlchemyTaskRepository(session),
                assignment_repository=SqlAlchemyAssignmentRepository(session),
                context_repository=SqlAlchemyContextRepository(session),
                event_repository=SqlAlchemyEventRepository(session),
            )
            result = await service.transition_master_task(
                task_id=task_id,
                target=MasterTaskStatus.CANCELLED,
                actor_id="api_user",
                reason_code="USER_CANCELLED",
            )
        return jsonable_encoder(result)

    @app.delete("/api/v1/tasks/{task_id}", tags=["tasks"])
    async def archive_task(task_id: UUID, request: Request) -> Any:
        """停止未完成任务并从工作台列表移除；审计和已发布产物仍可追溯。"""

        container = _container(request)
        async with container.database.session_factory() as session, session.begin():
            tasks = SqlAlchemyTaskRepository(session)
            master = await tasks.get_master(task_id, for_update=True)
            if master is None:
                raise HTTPException(status_code=404, detail="任务不存在")
            events = SqlAlchemyEventRepository(session)
            if any(
                item.event_type is TaskEventType.TASK_ARCHIVED
                for item in await events.list_for_task(task_id)
            ):
                return {"task_id": str(task_id), "archived": True}
            if master.status not in {
                MasterTaskStatus.SUCCEEDED,
                MasterTaskStatus.FAILED,
                MasterTaskStatus.CANCELLED,
            }:
                await TaskStateService(
                    task_repository=tasks,
                    assignment_repository=SqlAlchemyAssignmentRepository(session),
                    context_repository=SqlAlchemyContextRepository(session),
                    event_repository=events,
                ).transition_master_task(
                    task_id=task_id,
                    target=MasterTaskStatus.CANCELLED,
                    actor_id="api_user",
                    reason_code="USER_ARCHIVED_TASK",
                )
            await events.append(
                task_id=task_id,
                event_type=TaskEventType.TASK_ARCHIVED,
                actor_type="USER",
                actor_id="api_user",
                occurred_at=datetime.now(UTC),
            )
        return {"task_id": str(task_id), "archived": True}

    @app.post(
        "/api/v1/tasks/{task_id}/retry",
        response_model=TaskAcceptedResponse,
        status_code=202,
        tags=["tasks"],
    )
    async def retry_task(task_id: UUID, request: Request) -> TaskAcceptedResponse:
        """让总控基于已有 Artifact 和失败轨迹创建新的拆解 revision。"""
        snapshot = await _container(request).orchestration.submit_and_run(
            question="重试当前失败任务，复用所有仍然有效的已有产物。",
            files=[],
            task_id=task_id,
            request_id=uuid4(),
            execute_workers=False,
        )
        return _accepted(snapshot)

    @app.get("/api/v1/tasks/{task_id}/events", tags=["events"])
    async def task_events(task_id: UUID, request: Request, after: int = 0) -> Any:
        """按 sequence 增量读取任务事件。"""
        return jsonable_encoder(
            await PlatformQueryService(
                _container(request).database, _container(request).artifacts
            ).events(task_id, after=after)
        )

    @app.get("/api/v1/tasks/{task_id}/logs", tags=["events"])
    async def task_run_log(task_id: UUID, request: Request) -> Response:
        """下载任务级公开 JSONL 排障日志；PostgreSQL 仍是权威事实来源。"""

        content = await _container(request).run_logs.read(task_id)
        if content is None:
            raise HTTPException(status_code=404, detail="任务日志不存在或文件日志未启用")
        return Response(
            content=content,
            media_type="application/x-ndjson; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="task-{task_id}.jsonl"'},
        )

    @app.get("/api/v1/tasks/{task_id}/events/stream", tags=["events"])
    async def task_event_stream(
        task_id: UUID, request: Request, after: int = 0
    ) -> StreamingResponse:
        """通过 SSE 推送公开任务事件，并支持浏览器按事件 ID 断线续传。"""
        query = PlatformQueryService(_container(request).database, _container(request).artifacts)

        async def generate() -> AsyncIterator[str]:
            # EventSource 重连时会自动携带 Last-Event-ID；显式 after 则用于首次订阅。
            last_event_id = request.headers.get("last-event-id", "").strip()
            try:
                resumed_cursor = int(last_event_id) if last_event_id else after
            except ValueError:
                resumed_cursor = after
            cursor = max(after, resumed_cursor, 0)
            idle_rounds = 0
            yield "retry: 2000\n\n"
            while not await request.is_disconnected():
                events = await query.events(task_id, after=cursor)
                for event in events:
                    cursor = max(cursor, event.sequence)
                    encoded = json.dumps(event.model_dump(mode="json"), ensure_ascii=False)
                    yield (
                        f"id: {event.sequence}\n"
                        f"event: {event.event_type.value}\n"
                        f"data: {encoded}\n\n"
                    )
                if events:
                    idle_rounds = 0
                else:
                    idle_rounds += 1
                    # 注释心跳不会触发业务事件监听器，只用于防止代理层关闭空闲连接。
                    if idle_rounds >= 20:
                        idle_rounds = 0
                        yield ": heartbeat\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            generate(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/v1/artifacts/{artifact_id}", tags=["artifacts"])
    async def get_artifact(artifact_id: UUID, request: Request) -> Any:
        """读取 Artifact 元数据和血缘。"""
        value = await PlatformQueryService(
            _container(request).database, _container(request).artifacts
        ).artifact(artifact_id)
        if value is None:
            raise HTTPException(status_code=404, detail="Artifact 不存在")
        return jsonable_encoder(value)

    @app.get("/api/v1/artifacts/{artifact_id}/content", tags=["artifacts"])
    async def get_artifact_content(artifact_id: UUID, request: Request) -> Response:
        """按需下载 Artifact 内容，不把大对象写进 SharedContext。"""
        query = PlatformQueryService(_container(request).database, _container(request).artifacts)
        record = await query.artifact(artifact_id)
        if record is None:
            raise HTTPException(status_code=404, detail="Artifact 不存在")
        return Response(
            content=await query.artifact_content(artifact_id), media_type=record.media_type
        )

    @app.get("/api/v1/artifacts/{artifact_id}/graph-preview", tags=["artifacts"])
    async def get_graph_preview(artifact_id: UUID, request: Request) -> Any:
        """返回候选或已发布图谱的只读节点—边投影，不修改任何 Artifact。"""

        container = _container(request)
        record = await PlatformQueryService(container.database, container.artifacts).artifact(
            artifact_id
        )
        if record is None:
            raise HTTPException(status_code=404, detail="Artifact 不存在")
        return jsonable_encoder(await GraphPreviewService(container.artifacts).build(record))

    @app.post("/api/v1/reviews", status_code=201, tags=["reviews"])
    async def create_review(payload: CreateHumanReviewCommand, request: Request) -> Any:
        """为候选本体或候选图谱创建强制人工审核门禁。"""
        return jsonable_encoder(await _container(request).reviews.request_review(payload))

    @app.get("/api/v1/tasks/{task_id}/reviews", tags=["reviews"])
    async def list_reviews(task_id: UUID, request: Request) -> Any:
        """返回任务的全部审核记录。"""
        return jsonable_encoder(await _container(request).reviews.list_reviews(task_id))

    @app.get("/api/v1/reviews/{review_id}", tags=["reviews"])
    async def get_review(review_id: UUID, request: Request) -> Any:
        """返回一个审核请求。"""
        value = await _container(request).reviews.get_review(review_id)
        if value is None:
            raise HTTPException(status_code=404, detail="审核请求不存在")
        return jsonable_encoder(value)

    @app.get("/api/v1/reviews/{review_id}/materials", tags=["reviews"])
    async def review_materials(review_id: UUID, request: Request) -> Any:
        """返回图谱预览/本体审核所需候选物、质量、冲突和证据引用。"""
        return jsonable_encoder(await _container(request).reviews.get_review_materials(review_id))

    @app.post("/api/v1/reviews/{review_id}/resolve", tags=["reviews"])
    async def resolve_review(
        review_id: UUID, payload: ResolveHumanReviewCommand, request: Request
    ) -> Any:
        """提交批准、拒绝或结构化 ChangeSet，Agent 无权调用此接口代替用户。"""
        if payload.review_id != review_id:
            raise HTTPException(status_code=422, detail="路径 review_id 与请求体不一致")
        container = _container(request)
        resolved = await container.reviews.resolve_review(payload)
        if payload.action in {ReviewOption.SUBMIT_CHANGES, ReviewOption.REJECT}:
            # 修改或拒绝会改变后续工作，必须交回唯一总控创建新的拆解 revision；领域
            # 服务不能越权自行创建 ontology_changeset_apply 等 AgentSubTask。
            await container.orchestration.submit_and_run(
                question=(
                    "用户已提交候选本体或候选图谱的审核修改/拒绝。请读取最新 "
                    "ChangeSet 与审核反馈，只规划应用修改、重新送审或必要返工任务；"
                    "不得沿用旧 Approval。"
                ),
                files=[],
                task_id=resolved.task_id,
                request_id=uuid4(),
                execute_workers=False,
                internal_continuation=True,
            )
            follow_up = "CONTROLLER_REPLANNED"
        else:
            # 批准只解锁原完整计划，不需要重新规划。
            follow_up = await TaskCoordinator(container).advance(resolved.task_id)
        return {"review": jsonable_encoder(resolved), "next_action": follow_up}

    @app.get("/api/v1/registry/agents", tags=["registry"])
    async def agents(request: Request) -> Any:
        """列出五个专业 Agent 的配置化能力声明。"""
        return jsonable_encoder(_container(request).agent_registry.list_all())

    @app.get("/api/v1/extensions/skills", tags=["extensions"])
    async def list_custom_skills(request: Request) -> list[CustomSkillDetail]:
        """列出用户自定义 Skill；历史内部修订不会出现在发现清单中。"""

        return await _container(request).extensions.list_skills()

    @app.get(
        "/api/v1/extensions/skill-options",
        response_model=list[AvailableSkillOption],
        tags=["extensions"],
    )
    async def available_custom_agent_skills(request: Request) -> list[AvailableSkillOption]:
        """返回内置及已发布自定义 Skill，供自定义 Agent 权限多选。"""

        container = _container(request)
        return await container.extensions.list_active_skill_options(
            container.skill_registry.list_all()
        )

    @app.post(
        "/api/v1/extensions/business-skills/proposals",
        response_model=SkillContractProposal,
        tags=["extensions"],
    )
    async def propose_business_skill_contract(
        payload: BusinessSkillDraftInput, request: Request
    ) -> SkillContractProposal:
        """调用模型理解业务用途，并只从代码注册的合法契约模板中推荐一项。"""

        return await _container(request).extensions.propose_business_skill_contract(payload)

    @app.get(
        "/api/v1/extensions/business-skills/contract-options",
        tags=["extensions"],
    )
    async def business_skill_contract_options(request: Request) -> Any:
        """返回业务化能力使用方式，不向前端公开内部 Artifact 名称。"""

        return _container(request).extensions.list_business_skill_contracts()

    @app.post(
        "/api/v1/extensions/business-skills/drafts",
        response_model=CustomSkillDetail,
        status_code=201,
        tags=["extensions"],
    )
    async def create_business_skill_draft(
        payload: BusinessSkillDraftInput, request: Request
    ) -> CustomSkillDetail:
        """按业务语言创建 Skill 草稿，不要求前端提交内部 Artifact 契约。"""

        return await _container(request).extensions.create_business_skill(payload)

    @app.put(
        "/api/v1/extensions/business-skills/{skill_id}/draft",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def update_business_skill_draft(
        skill_id: UUID, payload: BusinessSkillDraftInput, request: Request
    ) -> CustomSkillDetail:
        """以业务表单生成已发布或停用 Skill 的新草稿，不暴露内部契约。"""

        return await _container(request).extensions.update_business_skill(skill_id, payload)

    @app.post(
        "/api/v1/extensions/business-agents/drafts",
        response_model=CustomAgentDetail,
        status_code=201,
        tags=["extensions"],
    )
    async def create_business_agent_draft(
        payload: BusinessAgentDraftInput, request: Request
    ) -> CustomAgentDetail:
        """按业务职责创建 Agent 草稿，并由服务端从目录推导技术契约。"""

        container = _container(request)
        options = await container.extensions.list_active_skill_options(
            container.skill_registry.list_all()
        )
        return await container.extensions.create_business_agent(payload, available_skills=options)

    @app.put(
        "/api/v1/extensions/business-agents/{agent_id}/draft",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def update_business_agent_draft(
        agent_id: UUID, payload: BusinessAgentDraftInput, request: Request
    ) -> CustomAgentDetail:
        """以职责和已选 Skill 生成 Agent 新草稿，旧发布版本在确认前继续生效。"""

        container = _container(request)
        options = await container.extensions.list_active_skill_options(
            container.skill_registry.list_all()
        )
        return await container.extensions.update_business_agent(
            agent_id, payload, available_skills=options
        )

    @app.post(
        "/api/v1/extensions/skills",
        response_model=CustomSkillDetail,
        status_code=201,
        tags=["extensions"],
    )
    async def create_custom_skill(
        payload: CustomSkillDefinitionInput, request: Request
    ) -> CustomSkillDetail:
        """创建统一 Skill 草稿；发布时根据是否上传脚本自动选择执行实现。"""

        return await _container(request).extensions.create_skill(payload)

    @app.get(
        "/api/v1/extensions/skills/{skill_id}",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def get_custom_skill(skill_id: UUID, request: Request) -> CustomSkillDetail:
        """读取 Skill 的当前定义或待发布草稿，供前端编辑器回显。"""

        return await _container(request).extensions.get_skill(skill_id, include_definition=True)

    @app.put(
        "/api/v1/extensions/skills/{skill_id}",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def update_custom_skill(
        skill_id: UUID, payload: CustomSkillDefinitionInput, request: Request
    ) -> CustomSkillDetail:
        """创建新 Skill 草稿修订，不会覆写任何已经发布的历史定义。"""

        return await _container(request).extensions.update_skill(skill_id, payload)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/resources",
        response_model=CustomResourceSummary,
        status_code=201,
        tags=["extensions"],
    )
    async def add_custom_skill_resource(
        skill_id: UUID,
        request: Request,
        relative_path: Annotated[str, Form()],
        resource: Annotated[UploadFile, File(description="Skill 的脚本、参考资料或其他捆绑资源")],
    ) -> CustomResourceSummary:
        """将资源保存到当前草稿修订；资源只会在未来 Skill 执行时按需读取。"""

        return await _container(request).extensions.add_skill_resource(
            skill_id,
            relative_path=relative_path,
            media_type=resource.content_type or "application/octet-stream",
            content=await resource.read(),
        )

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/input-mapping-proposals",
        response_model=ScriptInputMappingProposal,
        tags=["extensions"],
    )
    async def propose_custom_skill_input_mapping(
        skill_id: UUID, request: Request
    ) -> ScriptInputMappingProposal:
        """读取草稿脚本字段并返回模型建议，只有用户确认后才保存映射。"""

        return await _container(request).extensions.propose_script_input_mapping(skill_id)

    @app.put(
        "/api/v1/extensions/skills/{skill_id}/input-bindings",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def confirm_custom_skill_input_mapping(
        skill_id: UUID, payload: list[ScriptInputFieldBinding], request: Request
    ) -> CustomSkillDetail:
        """把用户确认的字段来源保存为新草稿修订，旧检查结果不得复用。"""

        return await _container(request).extensions.confirm_script_input_mapping(skill_id, payload)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/entity-adapter-proposal",
        tags=["extensions"],
    )
    async def propose_entity_adapter(skill_id: UUID, request: Request) -> dict[str, object]:
        """展示实体逐项脚本可选函数与字段，用户确认前不发布配置。"""

        return await _container(request).extensions.propose_entity_record_adapter(skill_id)

    @app.post("/api/v1/extensions/skills/{skill_id}/function-adapter-proposal", tags=["extensions"])
    async def propose_function_adapter(skill_id: UUID, request: Request) -> dict[str, object]:
        """静态展示脚本函数及可用 Artifact 字段，供用户确认。"""

        return await _container(request).extensions.propose_script_function_adapter(skill_id)

    @app.put(
        "/api/v1/extensions/skills/{skill_id}/function-adapter",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def confirm_function_adapter(
        skill_id: UUID, payload: ScriptFunctionAdapter, request: Request
    ) -> CustomSkillDetail:
        """生成所选函数的标准入口并保存为新草稿。"""

        return await _container(request).extensions.confirm_script_function_adapter(
            skill_id, payload
        )

    @app.put(
        "/api/v1/extensions/skills/{skill_id}/entity-adapter",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def confirm_entity_adapter(
        skill_id: UUID, payload: EntityRecordAdapter, request: Request
    ) -> CustomSkillDetail:
        """保存受限的逐项适配配置，并要求在新修订上重新试运行。"""

        return await _container(request).extensions.confirm_entity_record_adapter(skill_id, payload)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/output-mapping-proposals",
        response_model=ScriptOutputMappingProposal,
        tags=["extensions"],
    )
    async def propose_custom_skill_output_mapping(
        skill_id: UUID, request: Request
    ) -> ScriptOutputMappingProposal:
        """从沙箱试运行提取字段名，只推荐已注册标准内容字段。"""

        return await _container(request).extensions.propose_script_output_mapping(skill_id)

    @app.put(
        "/api/v1/extensions/skills/{skill_id}/output-bindings",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def confirm_custom_skill_output_mapping(
        skill_id: UUID, payload: list[ScriptOutputFieldBinding], request: Request
    ) -> CustomSkillDetail:
        """保存用户确认的输出字段映射；发布前必须按新修订重新试运行。"""

        return await _container(request).extensions.confirm_script_output_mapping(skill_id, payload)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/contract-checks",
        response_model=CustomSkillContractCheck,
        tags=["extensions"],
    )
    async def check_custom_skill_contract(
        skill_id: UUID, request: Request
    ) -> CustomSkillContractCheck:
        """在无网络 Runner 中使用标准样例检查脚本依赖及输入输出契约。"""

        return await _container(request).extensions.check_skill_contract(skill_id)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/publish",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def publish_custom_skill(skill_id: UUID, request: Request) -> CustomSkillDetail:
        """发布当前草稿，使其可被后续动态 Registry 发现。"""

        return await _container(request).extensions.publish_skill(skill_id)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/disable",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def disable_custom_skill(skill_id: UUID, request: Request) -> CustomSkillDetail:
        """停用 Skill，保留不可变定义与历史任务追溯。"""

        return await _container(request).extensions.disable_skill(skill_id)

    @app.post(
        "/api/v1/extensions/skills/{skill_id}/enable",
        response_model=CustomSkillDetail,
        tags=["extensions"],
    )
    async def enable_custom_skill(skill_id: UUID, request: Request) -> CustomSkillDetail:
        """重新启用未修改的已发布 Skill；若已有草稿则需发布该草稿。"""

        return await _container(request).extensions.enable_skill(skill_id)

    @app.delete(
        "/api/v1/extensions/skills/{skill_id}",
        status_code=204,
        response_class=Response,
        tags=["extensions"],
    )
    async def delete_custom_skill(skill_id: UUID, request: Request) -> Response:
        """删除已停用 Skill 的定义包；历史任务、日志和 Artifact 不受影响。"""

        await _container(request).extensions.delete_skill(skill_id)
        return Response(status_code=204)

    @app.get("/api/v1/extensions/agents", tags=["extensions"])
    async def list_custom_agents(request: Request) -> list[CustomAgentDetail]:
        """列出用户创建的专业 Agent 身份与当前修订摘要。"""

        return await _container(request).extensions.list_agents()

    @app.post(
        "/api/v1/extensions/agents",
        response_model=CustomAgentDetail,
        status_code=201,
        tags=["extensions"],
    )
    async def create_custom_agent(
        payload: CustomAgentDefinitionInput, request: Request
    ) -> CustomAgentDetail:
        """创建自定义专业 Agent 草稿及其唯一 permissions.yaml 来源。"""

        return await _container(request).extensions.create_agent(payload)

    @app.get(
        "/api/v1/extensions/agents/{agent_id}",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def get_custom_agent(agent_id: UUID, request: Request) -> CustomAgentDetail:
        """读取 Agent 草稿或已发布职责正文及权限 YAML。"""

        return await _container(request).extensions.get_agent(agent_id, include_definition=True)

    @app.put(
        "/api/v1/extensions/agents/{agent_id}",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def update_custom_agent(
        agent_id: UUID, payload: CustomAgentDefinitionInput, request: Request
    ) -> CustomAgentDetail:
        """以新修订更新职责或权限，旧发布版继续保持可追溯。"""

        return await _container(request).extensions.update_agent(agent_id, payload)

    @app.post(
        "/api/v1/extensions/agents/{agent_id}/publish",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def publish_custom_agent(agent_id: UUID, request: Request) -> CustomAgentDetail:
        """静态校验 YAML 中引用的 Skill 后发布 Agent。"""

        return await _container(request).extensions.publish_agent(agent_id)

    @app.post(
        "/api/v1/extensions/agents/{agent_id}/disable",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def disable_custom_agent(agent_id: UUID, request: Request) -> CustomAgentDetail:
        """停用 Agent，使其不再进入之后任务的候选路由范围。"""

        return await _container(request).extensions.disable_agent(agent_id)

    @app.post(
        "/api/v1/extensions/agents/{agent_id}/enable",
        response_model=CustomAgentDetail,
        tags=["extensions"],
    )
    async def enable_custom_agent(agent_id: UUID, request: Request) -> CustomAgentDetail:
        """重新启用未修改的已发布 Agent；若已有草稿则需发布该草稿。"""

        return await _container(request).extensions.enable_agent(agent_id)

    @app.delete(
        "/api/v1/extensions/agents/{agent_id}",
        status_code=204,
        response_class=Response,
        tags=["extensions"],
    )
    async def delete_custom_agent(agent_id: UUID, request: Request) -> Response:
        """删除已停用 Agent 的定义包；历史任务、日志和 Artifact 不受影响。"""

        await _container(request).extensions.delete_agent(agent_id)
        return Response(status_code=204)

    @app.get("/api/v1/registry/skills", tags=["registry"])
    async def skills(request: Request) -> Any:
        """列出 V1 Skill 的稳定能力、权限及输入输出注册事实。"""
        registry = _container(request).skill_registry
        return jsonable_encoder(registry.list_all())

    @app.get("/api/v1/memories/status", tags=["memories"])
    async def memory_status(request: Request) -> dict[str, str | bool]:
        """返回长期记忆是否启用及供应商名称，不暴露 API Key 或用户标识。"""

        memory = _container(request).memory
        return {"enabled": memory.enabled, "provider": memory.provider_name}

    @app.post("/api/v1/memories", status_code=202, tags=["memories"])
    async def create_memory(payload: MemoryCreateRequest, request: Request) -> Any:
        """只在用户显式授权后，把稳定偏好或业务知识提交给 Mem0 Cloud。"""

        if not payload.authorized:
            raise HTTPException(status_code=422, detail="authorized 必须明确设为 true")
        receipt = await _container(request).memory.create_authorized_memory(
            text=payload.text,
            source_refs=payload.source_refs,
            infer=payload.infer,
        )
        return jsonable_encoder(receipt)

    @app.post("/api/v1/memories/search", tags=["memories"])
    async def search_memories(payload: MemorySearchRequest, request: Request) -> Any:
        """按自然语言查询当前单用户命名空间中的 Mem0 长期记忆。"""

        results = await _container(request).memory.search(
            payload.query,
            top_k=payload.top_k,
            threshold=payload.threshold,
            rerank=payload.rerank,
        )
        return {
            "enabled": _container(request).memory.enabled,
            "provider": _container(request).memory.provider_name,
            "results": jsonable_encoder(results),
        }

    return app
