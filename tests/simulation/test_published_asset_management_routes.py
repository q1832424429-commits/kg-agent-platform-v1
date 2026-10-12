"""保证隔离测试端复用资产管理页面时也提供对应写接口。"""

from pathlib import Path
from types import SimpleNamespace

from kg_agent.infrastructure.database.session import Database
from kg_agent.infrastructure.object_storage.memory import InMemoryObjectStorage
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.services.postgres_artifact_service import PostgresArtifactService
from kg_agent.services.run_log_service import RunLogService
from simulation_lab.app import create_app


def test_simulation_published_assets_management_routes(project_root: Path) -> None:
    """测试端不能显示“修改/删除”按钮却只提供只读资产 API。"""

    database = Database("sqlite+aiosqlite:///:memory:")
    storage = InMemoryObjectStorage()
    service = SimpleNamespace(
        database=database,
        storage=storage,
        artifacts=PostgresArtifactService(
            database.session_factory, storage=storage, registry=ArtifactRegistry.v1()
        ),
        run_logs=RunLogService(None),
    )
    app = create_app(service=service, manage_database=False, project_root=project_root)
    paths = app.openapi()["paths"]
    assert "patch" in paths["/api/v1/published-assets/{artifact_id}"]
    assert "delete" in paths["/api/v1/published-assets/{artifact_id}"]
