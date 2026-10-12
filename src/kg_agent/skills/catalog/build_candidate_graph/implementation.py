"""候选图谱构建 Skill 的独立运行实现。"""

from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.skills.base import Skill
from kg_agent.skills.graph_build import GraphBuildSkill
from kg_agent.skills.llm import ArtifactContentReader


def build_skill(
    registration: SkillRegistration,
    *,
    model_gateway: ModelGateway,
    artifact_reader: ArtifactContentReader,
    artifact_registry: ArtifactRegistry,
    skill_prompt: str,
) -> Skill:
    """构造确定性的候选图谱组装器；其他参数保留统一工厂接口。"""

    del model_gateway, artifact_registry, skill_prompt
    return GraphBuildSkill(registration, artifact_reader=artifact_reader)
