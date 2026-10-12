"""组装使用 DeepSeek、PostgreSQL 和正式 Registry 的生产总控 Agent。

工厂只连接已有适配器，不创建 API、Worker 或专业 Agent。
"""

from pathlib import Path

from kg_agent.agents.controller.agent import ControllerAgent
from kg_agent.agents.controller.prompts import PromptRepository
from kg_agent.config import Settings
from kg_agent.infrastructure.database.session import Database
from kg_agent.model_gateway.deepseek import DeepSeekModelAdapter
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.postgres_artifact_reuse_service import PostgresArtifactReuseService
from kg_agent.services.postgres_context_service import PostgresContextService


def build_deepseek_controller(
    *,
    settings: Settings,
    database: Database,
    project_root: Path,
) -> ControllerAgent:
    """创建第四阶段真实总控 Agent 的生产依赖组合。

    构造时只校验配置和读取 Registry/Prompt；数据库与 DeepSeek 网络连接都在运行时建立。
    """

    artifact_registry = ArtifactRegistry.v1()
    agent_registry = AgentRegistry.from_directory(project_root / "configs" / "agents")
    skill_registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifact_registry,
    )
    skill_registry.validate_against_agents(agent_registry)
    return ControllerAgent(
        model_gateway=DeepSeekModelAdapter(settings),
        agent_registry=agent_registry,
        context_service=PostgresContextService(database.session_factory),
        prompt_repository=PromptRepository(project_root / "configs" / "prompts" / "controller"),
        max_repairs=settings.controller_max_repairs,
        artifact_registry=artifact_registry,
        skill_registry=skill_registry,
        artifact_reuse_evaluator=PostgresArtifactReuseService(database.session_factory),
    )
