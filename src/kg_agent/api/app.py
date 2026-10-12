"""创建可上传文件并观察 Agent 路由的 FastAPI 应用。

当前应用是第七阶段最小演示入口，不包含登录、前端、生产 Worker 常驻进程或真实解析算法。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Any
from uuid import UUID

from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import JSONResponse

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
from kg_agent.exceptions import KGAgentError
from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.model_gateway.deepseek import DeepSeekModelAdapter
from kg_agent.services.demo_service import DemoOrchestrationService, UploadedFile


def _normalize_swagger_file_schema(value: Any) -> None:
    """把 Pydantic 3.1 文件标记转换为 Swagger UI 可识别的 binary 格式。"""

    if isinstance(value, dict):
        if (
            value.get("type") == "string"
            and value.get("contentMediaType") == "application/octet-stream"
        ):
            value.pop("contentMediaType")
            value["format"] = "binary"
        for child in value.values():
            _normalize_swagger_file_schema(child)
    elif isinstance(value, list):
        for child in value:
            _normalize_swagger_file_schema(child)


def _collect_text_preview(value: Any) -> list[str]:
    """从 Artifact JSON 中递归收集便于用户观察的 text 字段。"""

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


def create_demo_app(
    service: DemoOrchestrationService | None = None,
    *,
    database: Database | None = None,
    manage_database: bool = True,
) -> FastAPI:
    """创建 FastAPI 应用；测试可注入 FakeModel 组成的演示服务。"""

    owned_database = database
    if service is None:
        settings = Settings.from_env()
        owned_database = Database("sqlite+aiosqlite:///:memory:")
        service = DemoOrchestrationService(
            database=owned_database,
            storage=InMemoryObjectStorage(),
            model_gateway=DeepSeekModelAdapter(settings),
            project_root=Path(__file__).resolve().parents[3],
            max_repairs=settings.controller_max_repairs,
            lease_seconds=settings.assignment_lease_seconds,
            heartbeat_seconds=settings.assignment_heartbeat_seconds,
            max_file_bytes=settings.artifact_max_file_bytes,
            max_files_per_task=settings.artifact_max_files_per_task,
            max_total_bytes_per_task=settings.artifact_max_total_bytes_per_task,
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        """在本地演示启动时创建临时 Schema，并在关闭时释放连接。"""

        if manage_database and owned_database is not None:
            await owned_database.create_schema()
        try:
            yield
        finally:
            if manage_database and owned_database is not None:
                await owned_database.dispose()

    app = FastAPI(
        title="知识图谱构建 Agent 平台 V1",
        version="0.1.0",
        description="上传文件并观察总控路由、Worker、专业 Agent 与 LLM Skill。",
        lifespan=lifespan,
    )

    @app.exception_handler(KGAgentError)
    async def handle_business_error(_: Request, exc: KGAgentError) -> JSONResponse:
        """把可预期领域异常转换为不含内部细节的 409 响应。"""

        payload = ErrorResponse(error_code=type(exc).__name__, message=str(exc))
        return JSONResponse(status_code=409, content=payload.model_dump(mode="json"))

    @app.get("/health")
    async def health() -> dict[str, str]:
        """返回进程级健康状态；不触发 DeepSeek 或业务任务。"""

        return {"status": "ok", "mode": "local-demo"}

    async def execute_demo(
        *,
        question: str,
        uploaded: list[UploadedFile],
        task_id: UUID | None,
        conversation_id: UUID | None,
        request_id: UUID | None,
        execute_workers: bool,
        max_worker_runs: int,
    ) -> DemoRunResponse:
        """复用同一条应用服务链路执行演示并组装可观察响应。"""

        snapshot = await service.submit_and_run(
            question=question,
            files=uploaded,
            task_id=task_id,
            conversation_id=conversation_id,
            request_id=request_id,
            execute_workers=execute_workers,
            max_worker_runs=max_worker_runs,
        )
        result = snapshot.controller_result
        decomposition = result.decomposition
        runs = []
        for run in snapshot.runs:
            skills = []
            for skill_name in run.skills_used:
                outputs = [
                    record.ref
                    for record in snapshot.artifacts
                    if record.ref.producer_run_id == run.run_id
                    and record.ref.producer_skill == skill_name
                ]
                skills.append(
                    DemoSkillExecutionView(
                        skill_name=skill_name,
                        duration_ms=float(run.metrics.get(f"skill.{skill_name}.duration_ms", 0.0)),
                        output_artifact_ids=[item.artifact_id for item in outputs],
                        output_artifact_types=[item.artifact_type for item in outputs],
                    )
                )
            runs.append(
                DemoWorkerRunView(
                    run_id=run.run_id,
                    assignment_id=run.assignment_id,
                    agent_type=run.agent_name,
                    status=run.status.value,
                    skills=skills,
                    blocking_issues=run.blocking_issues,
                )
            )
        artifact_views = []
        parsed_text_preview: list[str] = []
        for record in snapshot.artifacts:
            content_preview = await service.artifact_preview(record)
            if record.ref.artifact_type in {"ParsedDocument", "SemanticDocument"}:
                for parsed_text in _collect_text_preview(content_preview):
                    if parsed_text not in parsed_text_preview:
                        parsed_text_preview.append(parsed_text)
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
                    content_preview=content_preview,
                )
            )
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
                    input_artifact_ids=item.input_artifact_ids,
                    expected_output_types=item.expected_output_types,
                )
                for item in snapshot.subtasks
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
                for item in snapshot.assignments
            ],
            worker_runs=runs,
            mock_parsed_text_preview=parsed_text_preview,
            artifacts=artifact_views,
        )

    @app.post(
        "/api/v1/demo/upload-runs",
        response_model=DemoRunResponse,
        summary="上传文件并输入问题",
    )
    async def run_uploaded_demo(
        question: Annotated[
            str,
            Form(
                min_length=1,
                description="用户针对所上传文件提出的知识图谱构建任务。",
            ),
        ],
        files: Annotated[
            list[UploadFile],
            File(description="必须选择一个或多个真实文件。"),
        ],
        task_id: Annotated[UUID | None, Form()] = None,
        conversation_id: Annotated[UUID | None, Form()] = None,
        request_id: Annotated[UUID | None, Form()] = None,
        execute_workers: Annotated[bool, Form()] = True,
        max_worker_runs: Annotated[int, Form(ge=0, le=100)] = 10,
    ) -> DemoRunResponse:
        """接收真实上传文件和用户问题，执行相同的 Agent 与业务 Skill 链路。"""

        uploaded = [
            UploadedFile(
                filename=item.filename or "unnamed",
                media_type=item.content_type or "application/octet-stream",
                content=await item.read(),
            )
            for item in files
        ]
        return await execute_demo(
            question=question,
            uploaded=uploaded,
            task_id=task_id,
            conversation_id=conversation_id,
            request_id=request_id,
            execute_workers=execute_workers,
            max_worker_runs=max_worker_runs,
        )

    original_openapi = app.openapi

    def swagger_compatible_openapi() -> dict[str, Any]:
        """生成能让当前 Swagger UI 正确提交 multipart 文件的 OpenAPI Schema。"""

        schema = original_openapi()
        _normalize_swagger_file_schema(schema)
        return schema

    # FastAPI 官方支持覆盖 openapi 生成函数；这里只修正展示兼容性，不改变接口契约。
    app.openapi = swagger_compatible_openapi  # type: ignore[method-assign]
    return app


# 默认导出正式生产应用；演示工厂仅供历史回归测试显式使用。
from kg_agent.api.production import create_app as create_app  # noqa: E402
