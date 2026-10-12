"""把已发布自定义定义包合并为一次任务可用的运行期 Registry 快照。

内置 Registry 仍来自仓库配置。本模块只在进程内派生组合视图：Agent YAML 是唯一的
授权事实，Skill 的 allowed_agents 仅为执行校验临时计算，绝不写回 PostgreSQL。
"""

from __future__ import annotations

from kg_agent.extensions.script_skill import ScriptSkill
from kg_agent.extensions.service import CustomExtensionService
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.skills.base import Skill
from kg_agent.skills.llm import ArtifactContentReader, LLMGeneratedSkill


async def build_composite_registries(
    *,
    builtin_agents: AgentRegistry,
    builtin_skills: SkillRegistry,
    extensions: CustomExtensionService | None,
    model_gateway: ModelGateway,
    artifact_reader: ArtifactContentReader,
    artifact_registry: ArtifactRegistry,
) -> tuple[AgentRegistry, SkillRegistry]:
    """返回本轮唯一的 Agent / Skill 组合 Registry 快照。

    如果未配置扩展服务，则原样返回内置 Registry，保持测试和现有部署路径完全兼容。
    """

    if extensions is None:
        return builtin_agents, builtin_skills
    custom_agents, custom_skills = await extensions.load_active_registrations()
    combined_agents = AgentRegistry([*builtin_agents.list_all(), *custom_agents])
    permissions: dict[str, list[str]] = {}
    for agent in custom_agents:
        for skill_name in agent.allowed_skills:
            permissions.setdefault(skill_name, []).append(agent.agent_type)
    combined_skills = builtin_skills.clone_with_permission_extensions(permissions)
    for registration, prompt, has_script in custom_skills:
        runtime: Skill
        if has_script:
            runtime = ScriptSkill(
                registration,
                extensions=extensions,
                artifact_reader=artifact_reader,
                artifact_registry=artifact_registry,
            )
        else:
            runtime = LLMGeneratedSkill(
                registration,
                model_gateway=model_gateway,
                artifact_reader=artifact_reader,
                artifact_registry=artifact_registry,
                skill_prompt=prompt,
            )
        combined_skills.register(registration, runtime)
    combined_skills.validate_against_agents(combined_agents)
    return combined_agents, combined_skills
