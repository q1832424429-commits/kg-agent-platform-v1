"""验证正式 API 的文件接入、幂等和持久化查询。"""

import hashlib
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi.testclient import TestClient

from kg_agent.api.production import _canonical_document_media_type, create_app
from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_content_models import (
    PublishedGraphContent,
    PublishedOntologyContent,
)
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.enums import ArtifactStatus, TaskIntent
from kg_agent.domain.task_models import OntologyStrategy, TaskSpec
from kg_agent.infrastructure.database.models import (
    ArtifactContentRow,
    ArtifactRefRow,
    PublishedGraphRow,
)
from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.memory.memory_service import LongTermMemoryService
from kg_agent.memory.stores import InMemoryLongTermMemoryStore
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.postgres_artifact_service import PostgresArtifactService
from kg_agent.services.postgres_human_review_service import PostgresHumanReviewService
from kg_agent.services.run_log_service import RunLogService
from kg_agent.services.task_orchestration_service import TaskOrchestrationService
from tests.factories import NOW


@pytest.mark.parametrize(
    ("filename", "declared", "expected"),
    [
        (
            "设备说明.DOCX",
            "application/octet-stream",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        (
            "产品规格.docx",
            "application/zip",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ),
        ("legacy.doc", "application/msword", "application/msword"),
    ],
)
def test_production_normalizes_supported_document_media_types(
    filename: str,
    declared: str,
    expected: str,
) -> None:
    """正式端应修正浏览器误报的 DOCX MIME，但不能把旧 DOC 伪装成 DOCX。"""

    assert _canonical_document_media_type(filename, declared) == expected


@pytest.mark.anyio
async def test_production_upload_is_idempotent_and_queryable(
    project_root: Path,
    tmp_path: Path,
) -> None:
    """同一请求重放不再调用模型，也不会产生第二份任务。"""
    task_id, second_task_id, conversation_id, request_id = uuid4(), uuid4(), uuid4(), uuid4()
    decomposition = TaskDecompositionResult(
        task_intent=TaskIntent.START,
        task_spec=TaskSpec(
            task_id=task_id,
            revision=1,
            task_intent=TaskIntent.START,
            business_goal="保存文档并等待进一步说明",
            source_artifact_ids=[],
            target_output_types=[],
            ontology_strategy=OntologyStrategy(mode="BUILD_FROM_DOCUMENTS"),
            created_at=NOW,
        ),
        subtasks=[],
    )
    second_decomposition = decomposition.model_copy(
        update={
            "task_spec": decomposition.task_spec.model_copy(
                update={"task_id": second_task_id, "business_goal": "继续在同一会话开始另一项任务"}
            )
        }
    )
    # 文件型 SQLite 允许接入服务的多个短事务 Session 使用独立连接；内存数据库
    # 的单连接池会让幂等记录事务与任务发布事务在同一连接上相互等待。
    database = Database(f"sqlite+aiosqlite:///{tmp_path / 'production-api.db'}")
    storage = InMemoryObjectStorage()
    artifacts_registry = ArtifactRegistry.v1()
    agents = AgentRegistry.from_directory(project_root / "configs" / "agents")
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts_registry,
    )
    artifacts = PostgresArtifactService(
        database.session_factory, storage=storage, registry=artifacts_registry
    )
    reviews = PostgresHumanReviewService(
        database.session_factory, storage=storage, registry=artifacts_registry
    )
    orchestration = TaskOrchestrationService(
        database=database,
        storage=storage,
        model_gateway=FakeModelAdapter([decomposition, second_decomposition]),
        project_root=project_root,
    )
    runtime = SimpleNamespace(
        database=database,
        storage=storage,
        artifacts=artifacts,
        reviews=reviews,
        orchestration=orchestration,
        agent_registry=agents,
        skill_registry=skills,
        memory=LongTermMemoryService(InMemoryLongTermMemoryStore(), user_id="single-user"),
        run_logs=RunLogService(None),
    )
    app = create_app(runtime=runtime, manage_lifespan=False)  # type: ignore[arg-type]

    # create_schema 只用于测试；正式部署由 Alembic migrate 服务负责。API 与 SQLite
    # 必须运行在同一事件循环内，避免 TestClient 的线程门户与异步连接相互等待。
    await database.create_schema()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        data = {
            "message": "接收这个设备文档。",
            "task_id": str(task_id),
            "conversation_id": str(conversation_id),
            "request_id": str(request_id),
        }
        files = {"files": ("equipment.pdf", b"pdf bytes", "application/pdf")}
        first = await client.post("/api/v1/tasks/upload", data=data, files=files)
        second = await client.post("/api/v1/tasks/upload", data=data, files=files)
        another_task = await client.post(
            "/api/v1/tasks",
            json={
                "message": "在同一会话开始另一项任务",
                "task_id": str(second_task_id),
                "conversation_id": str(conversation_id),
            },
        )
        # 发布资产和普通文档同属被删除会话；删除后仅前者应继续可读、可复用。
        published_id = uuid4()
        content = (
            PublishedOntologyContent(
                name="设备本体",
                ontology_id=uuid4(),
                ontology_version="1.0.0",
                approval_artifact_id=uuid4(),
                publication_title="已发布设备本体",
            )
            .model_dump_json()
            .encode("utf-8")
        )
        object_key = f"published-test/{published_id}.json"
        content_uri = await storage.put_bytes(object_key, content, "application/json")
        published_ref = ArtifactRef(
            artifact_id=published_id,
            artifact_type="PublishedOntology",
            schema_version="1.0.0",
            task_id=task_id,
            task_spec_revision=1,
            producer_agent="graph_management_agent",
            producer_skill="publish_ontology",
            producer_run_id=uuid4(),
            publication_title="已发布设备本体",
            content_uri=content_uri,
            content_summary="设备本体",
            checksum=f"sha256:{hashlib.sha256(content).hexdigest()}",
            status=ArtifactStatus.AVAILABLE,
            created_at=NOW,
        )
        async with database.session_factory() as session, session.begin():
            session.add(
                ArtifactRefRow(
                    artifact_id=published_id,
                    task_id=task_id,
                    artifact_type="PublishedOntology",
                    status="AVAILABLE",
                    payload=published_ref.model_dump(mode="json"),
                    created_at=NOW,
                )
            )
            await session.flush()
            session.add(
                ArtifactContentRow(
                    artifact_id=published_id,
                    object_key=object_key,
                    media_type="application/json",
                    size_bytes=len(content),
                    created_at=NOW,
                )
            )
        published_graph_id, graph_id = uuid4(), uuid4()
        graph_content = (
            PublishedGraphContent(
                graph_id=graph_id,
                graph_version="1.0.0",
                approval_artifact_id=uuid4(),
                publication_title="已发布设备图谱",
            )
            .model_dump_json()
            .encode("utf-8")
        )
        graph_object_key = f"published-test/{published_graph_id}.json"
        graph_uri = await storage.put_bytes(graph_object_key, graph_content, "application/json")
        graph_ref = published_ref.model_copy(
            update={
                "artifact_id": published_graph_id,
                "artifact_type": "PublishedGraph",
                "producer_skill": "publish_graph",
                "publication_title": "已发布设备图谱",
                "content_uri": graph_uri,
                "checksum": f"sha256:{hashlib.sha256(graph_content).hexdigest()}",
            }
        )
        async with database.session_factory() as session, session.begin():
            session.add(
                ArtifactRefRow(
                    artifact_id=published_graph_id,
                    task_id=task_id,
                    artifact_type="PublishedGraph",
                    status="AVAILABLE",
                    payload=graph_ref.model_dump(mode="json"),
                    created_at=NOW,
                )
            )
            await session.flush()
            session.add_all(
                [
                    ArtifactContentRow(
                        artifact_id=published_graph_id,
                        object_key=graph_object_key,
                        media_type="application/json",
                        size_bytes=len(graph_content),
                        created_at=NOW,
                    ),
                    PublishedGraphRow(
                        graph_id=graph_id,
                        artifact_id=published_graph_id,
                        task_id=task_id,
                        graph_version="1.0.0",
                        source_candidate_artifact_id=None,
                        approval_artifact_id=None,
                        created_at=NOW,
                    ),
                ]
            )
        queried = await client.get(f"/api/v1/tasks/{task_id}")
        before_delete = await client.get("/api/v1/tasks")
        deleted = await client.delete(f"/api/v1/tasks/{task_id}")
        after_delete = await client.get("/api/v1/tasks")
        retained = await client.get(f"/api/v1/tasks/{task_id}")
        conversations = await client.get("/api/v1/conversations")
        conversation = await client.get(f"/api/v1/conversations/{conversation_id}")
        renamed = await client.patch(
            f"/api/v1/conversations/{conversation_id}", json={"title": "设备资料会话"}
        )
        deleted_conversation = await client.delete(f"/api/v1/conversations/{conversation_id}")
        removed_task = await client.get(f"/api/v1/tasks/{task_id}")
        removed_second_task = await client.get(f"/api/v1/tasks/{second_task_id}")
        remaining_conversations = await client.get("/api/v1/conversations")
        retained_assets = await client.get("/api/v1/published-assets/ontologies")
        retained_content = await client.get(f"/api/v1/published-assets/{published_id}/content")
        retained_graphs = await client.get("/api/v1/published-assets/graphs")
        retained_graph = await client.get(
            f"/api/v1/published-assets/{published_graph_id}/graph-preview"
        )
        asset_renamed = await client.patch(
            f"/api/v1/published-assets/{published_id}",
            json={"title": "设备本体 · 目录名称"},
        )
        renamed_assets = await client.get("/api/v1/published-assets/ontologies")
        unchanged_content = await client.get(f"/api/v1/published-assets/{published_id}/content")
        asset_removed = await client.delete(f"/api/v1/published-assets/{published_id}")
        removed_from_catalog = await client.get("/api/v1/published-assets/ontologies")
        removed_content = await client.get(f"/api/v1/published-assets/{published_id}/content")
        async with database.session_factory() as session:
            retained_row = await session.get(ArtifactRefRow, published_id)
            retained_artifact_status = retained_row.status if retained_row else None
        openapi = (await client.get("/openapi.json")).json()
    await database.dispose()

    assert first.status_code == 202, first.text
    assert second.status_code == 202, second.text
    assert first.json() == second.json()
    assert another_task.status_code == 202, another_task.text
    assert another_task.json()["conversation_id"] == str(conversation_id)
    assert queried.status_code == 200
    assert len(queried.json()["artifacts"]) == 3
    assert before_delete.status_code == 200
    assert any(item["task_id"] == str(task_id) for item in before_delete.json())
    assert deleted.status_code == 200, deleted.text
    assert deleted.json()["archived"] is True
    assert all(item["task_id"] != str(task_id) for item in after_delete.json())
    assert retained.status_code == 200
    assert retained.json()["master_task"]["status"] == "CANCELLED"
    assert conversations.status_code == 200
    assert len(conversations.json()) == 1
    assert conversation.status_code == 200
    assert len(conversation.json()["messages"]) == 4
    assert len(conversation.json()["tasks"]) == 2
    assert renamed.json()["title"] == "设备资料会话"
    assert deleted_conversation.status_code == 200, deleted_conversation.text
    assert deleted_conversation.json()["deleted_task_count"] == 2
    assert deleted_conversation.json()["retained_published_asset_count"] == 2
    assert removed_task.status_code == 404
    assert removed_second_task.status_code == 404
    assert remaining_conversations.json() == []
    assert [item["artifact_id"] for item in retained_assets.json()] == [str(published_id)]
    assert retained_content.status_code == 200
    assert retained_content.content == content
    assert [item["artifact_id"] for item in retained_graphs.json()] == [str(published_graph_id)]
    assert retained_graph.status_code == 200
    assert asset_renamed.status_code == 200, asset_renamed.text
    assert renamed_assets.json()[0]["publication_title"] == "设备本体 · 目录名称"
    assert unchanged_content.content == content
    assert asset_removed.status_code == 200, asset_removed.text
    assert asset_removed.json()["catalog_status"] == "REMOVED"
    assert removed_from_catalog.json() == []
    assert removed_content.status_code == 404
    assert retained_artifact_status == "AVAILABLE"
    assert "/api/v1/demo/runs" not in openapi["paths"]


def test_production_serves_agent_workbench(project_root: Path) -> None:
    """正式应用应同源提供工作台页面和静态资源，且页面不依赖运行时查询。"""

    # 静态页面测试不触发生命周期，因此无需构造数据库和模型网关。
    app = create_app(runtime=SimpleNamespace(), manage_lifespan=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        root = client.get("/")
        workbench = client.get("/workbench")
        script = client.get("/workbench-assets/app.js")
        stylesheet = client.get("/workbench-assets/app.css")
        graph_component = client.get("/workbench-assets/graph_view.js")

    assert root.status_code == 200
    assert workbench.status_code == 200
    assert 'id="welcome-card"' in workbench.text
    assert "app.js?v=20260929-4" in workbench.text
    assert "app.css?v=20260929-5" in workbench.text
    assert 'id="artifact-dialog"' in workbench.text
    assert 'id="artifact-dialog-content"' in workbench.text
    submit_script = script.text.split("async function submitTask(event)", 1)[1]
    assert submit_script.index("dom.welcomeCard.hidden = true;") < submit_script.index(
        "const accepted = await api(path, options);"
    )
    assert "function routeStages(subtasks)" in script.text
    assert "dependency_subtask_ids" in script.text
    assert "Number(right.priority)" in script.text
    assert ".welcome-card[hidden]" in stylesheet.text
    assert "图谱 Agent 工作台" in workbench.text
    assert 'accept=".pdf,.docx,.xlsx,.pptx,.jpg,.jpeg,.png,.tif,.tiff,.webp"' in workbench.text
    assert 'id="ontology-input"' in workbench.text
    assert 'accept=".json,application/json"' in workbench.text
    assert 'id="published-ontology-select"' in workbench.text
    assert 'id="published-graph-select"' in workbench.text
    assert "submitTask" in script.text
    assert 'body.append("ontology_file", ontology)' in script.text
    assert 'body.append("published_ontology_artifact_id"' in script.text
    assert 'body.append("published_graph_artifact_id"' in script.text
    assert "/api/v1/published-assets/ontologies" in script.text
    assert "/api/v1/published-assets/graphs" in script.text
    assert "查看候选内容" in script.text
    assert 'id="review-actions"' in workbench.text
    assert "new EventSource" in script.text
    assert "/events/stream?after=" in script.text
    assert "window.setInterval(updateElapsedIndicators, 1000)" in script.text
    assert "setInterval(loadSnapshot" not in script.text
    assert "const followLatest = !sameConversation" in script.text
    assert (
        "dom.conversation.scrollTop = followLatest ? dom.conversation.scrollHeight : oldScrollTop"
        in script.text
    )
    assert "min-height: 0" in stylesheet.text
    assert ".message-user { margin-left: auto; margin-right: 0; }" in stylesheet.text
    assert ".composer:is(:hover, :focus-within) textarea { height: 88px; }" in stylesheet.text
    assert (
        ".composer:is(:hover, :focus-within) .published-asset-row { display: flex; }"
        in stylesheet.text
    )
    assert "workspace-shell" in stylesheet.text
    assert graph_component.status_code == 200
    assert "KGGraphView" in graph_component.text
    assert "graph-preview" in script.text
    assert "const ref = item.ref || item;" in script.text
    assert 'dom.conversation.addEventListener("click"' in script.text
    assert "ontologySummary(targetContent)" in script.text


def test_production_serves_custom_extension_workbench() -> None:
    """自定义定义包管理页应与正式 API 同源提供，避免引入第二套前端服务。"""

    app = create_app(runtime=SimpleNamespace(), manage_lifespan=False)  # type: ignore[arg-type]
    with TestClient(app) as client:
        page = client.get("/extensions")
        script = client.get("/workbench-assets/extensions.js")
        stylesheet = client.get("/workbench-assets/extensions.css")
        creation_script = client.get("/workbench-assets/extension_dialogs.js")

    assert page.status_code == 200
    assert "自定义 Agent 与 Skill" in page.text
    assert 'id="skills-list"' in page.text
    assert 'id="agents-list"' in page.text
    assert script.status_code == 200
    assert "/api/v1/extensions/skills" in script.text
    assert stylesheet.status_code == 200
    assert "input-mapping-proposals" in script.text
    assert "输入字段映射" in script.text
    assert "输出字段映射" in script.text
    assert "修改输入字段映射" in script.text
    assert "修改输出字段映射" in script.text
    assert "extension-mapping-table" in stylesheet.text
    assert creation_script.status_code == 200
    assert "输入字段映射" in creation_script.text
    assert "输出字段映射" in creation_script.text
    assert "/api/v1/extensions/agents" in script.text


def test_production_openapi_exposes_dedicated_ontology_upload() -> None:
    """正式新建和续接 multipart 接口都应公开独立本体文件字段。"""

    app = create_app(runtime=SimpleNamespace(), manage_lifespan=False)  # type: ignore[arg-type]
    schema = app.openapi()
    request_schemas = schema["components"]["schemas"]
    new_request_ref = schema["paths"]["/api/v1/tasks/upload"]["post"]["requestBody"]["content"][
        "multipart/form-data"
    ]["schema"]["$ref"]
    continue_request_ref = schema["paths"]["/api/v1/tasks/{task_id}/messages/upload"]["post"][
        "requestBody"
    ]["content"]["multipart/form-data"]["schema"]["$ref"]

    for reference in (new_request_ref, continue_request_ref):
        name = reference.rsplit("/", 1)[-1]
        properties = request_schemas[name]["properties"]
        assert "ontology_file" in properties
        assert "files" in properties
        assert "published_ontology_artifact_id" in properties
        assert "published_graph_artifact_id" in properties

    assert "/api/v1/published-assets/ontologies" in schema["paths"]
    assert "/api/v1/published-assets/graphs" in schema["paths"]


@pytest.mark.asyncio
async def test_memory_api_requires_consent_and_supports_recall() -> None:
    """长期记忆写入必须显式授权，授权后可通过公开接口检索。"""

    memory = LongTermMemoryService(InMemoryLongTermMemoryStore(), user_id="single-user")
    app = create_app(
        runtime=SimpleNamespace(memory=memory),  # type: ignore[arg-type]
        manage_lifespan=False,
    )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    ) as client:
        rejected = await client.post(
            "/api/v1/memories",
            json={"text": "用户偏好设备故障本体", "authorized": False},
        )
        accepted = await client.post(
            "/api/v1/memories",
            json={"text": "用户偏好设备故障本体", "authorized": True},
        )
        searched = await client.post(
            "/api/v1/memories/search",
            json={"query": "设备故障", "top_k": 5, "threshold": 0.1},
        )

    assert rejected.status_code == 422
    assert accepted.status_code == 202
    assert searched.status_code == 200
    assert searched.json()["provider"] == "in-memory"
    assert "设备故障本体" in searched.json()["results"][0]["memory"]


def test_production_openapi_exposes_script_mapping_confirmation() -> None:
    """正式与测试端复用的自定义能力 API 必须公开建议和人工确认两步。"""

    app = create_app(runtime=SimpleNamespace(), manage_lifespan=False)  # type: ignore[arg-type]
    paths = app.openapi()["paths"]

    assert "post" in paths["/api/v1/extensions/skills/{skill_id}/entity-adapter-proposal"]
    assert "put" in paths["/api/v1/extensions/skills/{skill_id}/entity-adapter"]
    assert "post" in paths["/api/v1/extensions/skills/{skill_id}/input-mapping-proposals"]
    assert "put" in paths["/api/v1/extensions/skills/{skill_id}/input-bindings"]
    assert "post" in paths["/api/v1/extensions/skills/{skill_id}/output-mapping-proposals"]
    assert "put" in paths["/api/v1/extensions/skills/{skill_id}/output-bindings"]
    assert "/api/v1/artifacts/{artifact_id}/extensions" not in paths
