"""组装生产环境依赖，统一供正式 API 与独立 Worker 使用。"""

import os
from dataclasses import dataclass
from pathlib import Path

from kg_agent.config import Settings
from kg_agent.extensions.service import CustomExtensionService
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
from kg_agent.services.model_call_observer import RunLogModelCallObserver
from kg_agent.services.postgres_artifact_service import PostgresArtifactService
from kg_agent.services.postgres_human_review_service import PostgresHumanReviewService
from kg_agent.services.postgres_publish_guard import PostgresPublishGuard
from kg_agent.services.run_log_service import RunLogService
from kg_agent.services.task_orchestration_service import TaskOrchestrationService


@dataclass(slots=True)
class RuntimeContainer:
    """保存一个进程内共享的正式基础设施和应用服务。"""

    settings: Settings
    project_root: Path
    database: Database
    storage: ObjectStorage
    artifact_registry: ArtifactRegistry
    agent_registry: AgentRegistry
    skill_registry: SkillRegistry
    orchestration: TaskOrchestrationService
    artifacts: PostgresArtifactService
    reviews: PostgresHumanReviewService
    memory: LongTermMemoryService
    run_logs: RunLogService
    extensions: CustomExtensionService

    async def startup(self) -> None:
        """验证对象存储并确保受控 Bucket 已创建。"""
        if isinstance(self.storage, LocalFileObjectStorage):
            # 仅本机 uv 模式使用 create_all；正式 PostgreSQL 必须继续由 Alembic 管理。
            await self.database.create_schema()
        await self.storage.ensure_bucket()

    async def shutdown(self) -> None:
        """释放数据库连接池。"""
        await self.database.dispose()


def build_runtime(
    settings: Settings | None = None, *, project_root: Path | None = None
) -> RuntimeContainer:
    """根据环境变量构建生产运行容器，缺少必需配置时立即失败。"""
    resolved = settings or Settings.from_env()
    root = project_root or Path(__file__).resolve().parents[2]
    local_data_dir = os.getenv("LOCAL_UV_DATA_DIR", "").strip()
    if local_data_dir:
        local_root = Path(local_data_dir).expanduser().resolve()
        local_root.mkdir(parents=True, exist_ok=True)
        database = Database(f"sqlite+aiosqlite:///{local_root / 'formal.db'}")
        storage: ObjectStorage = LocalFileObjectStorage(
            local_root / "objects", bucket="formal-artifacts"
        )
    else:
        endpoint, access, secret, bucket, secure = resolved.require_minio()
        database = Database(resolved.require_database())
        storage = MinioObjectStorage(
            endpoint=endpoint,
            access_key=access,
            secret_key=secret,
            bucket=bucket,
            secure=secure,
        )
    # 模型级性能指标沿用既有任务 JSONL，而不是引入第二套观测存储。
    run_log_directory = Path(os.getenv("RUN_LOG_DIR", str(root / "logs" / "formal")))
    run_logs = RunLogService(run_log_directory)
    model = DeepSeekModelAdapter(resolved, observer=RunLogModelCallObserver(run_logs))
    if resolved.mem0_enabled:
        mem0_key, mem0_base_url, mem0_user_id, mem0_app_id, mem0_timeout = resolved.require_mem0()
        memory_store: LongTermMemoryStore = Mem0CloudMemoryStore(
            api_key=mem0_key,
            base_url=mem0_base_url,
            timeout_seconds=mem0_timeout,
        )
    else:
        mem0_user_id = resolved.mem0_user_id
        mem0_app_id = resolved.mem0_app_id
        memory_store = DisabledLongTermMemoryStore()
    memory = LongTermMemoryService(
        memory_store,
        user_id=mem0_user_id,
        app_id=mem0_app_id,
        default_top_k=resolved.mem0_top_k,
        candidate_top_k=resolved.mem0_candidate_top_k,
        default_threshold=resolved.mem0_threshold,
        dedup_threshold=resolved.mem0_dedup_threshold,
        merge_threshold=resolved.mem0_merge_threshold,
        default_rerank=resolved.mem0_rerank,
    )
    artifact_registry = ArtifactRegistry.v1()
    agent_registry = AgentRegistry.from_directory(root / "configs" / "agents")
    artifacts = PostgresArtifactService(
        database.session_factory,
        storage=storage,
        registry=artifact_registry,
        max_file_bytes=resolved.artifact_max_file_bytes,
        max_files_per_task=resolved.artifact_max_files_per_task,
        max_total_bytes_per_task=resolved.artifact_max_total_bytes_per_task,
    )
    skill_registry = SkillRegistry.from_directory(
        root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifact_registry,
        model_gateway=model,
        artifact_reader=artifacts,
        side_effect_authorizer=PostgresPublishGuard(
            database.session_factory, agents=agent_registry, storage=storage
        ),
    )
    skill_registry.validate_against_agents(agent_registry)
    extensions = CustomExtensionService(
        database.session_factory,
        artifact_registry=artifact_registry,
        builtin_agent_keys=[item.agent_type for item in agent_registry.list_all()],
        builtin_skill_keys=[item.name for item in skill_registry.list_all()],
        model_gateway=model,
    )
    orchestration = TaskOrchestrationService(
        database=database,
        storage=storage,
        model_gateway=model,
        project_root=root,
        max_repairs=resolved.controller_max_repairs,
        lease_seconds=resolved.assignment_lease_seconds,
        heartbeat_seconds=resolved.assignment_heartbeat_seconds,
        max_file_bytes=resolved.artifact_max_file_bytes,
        max_files_per_task=resolved.artifact_max_files_per_task,
        max_total_bytes_per_task=resolved.artifact_max_total_bytes_per_task,
        memory_service=memory,
        run_log_service=run_logs,
        custom_extensions=extensions,
    )
    reviews = PostgresHumanReviewService(
        database.session_factory,
        storage=storage,
        registry=artifact_registry,
        max_file_bytes=resolved.artifact_max_file_bytes,
        max_files_per_task=resolved.artifact_max_files_per_task,
        max_total_bytes_per_task=resolved.artifact_max_total_bytes_per_task,
    )
    return RuntimeContainer(
        resolved,
        root,
        database,
        storage,
        artifact_registry,
        agent_registry,
        skill_registry,
        orchestration,
        artifacts,
        reviews,
        memory,
        run_logs,
        extensions,
    )
