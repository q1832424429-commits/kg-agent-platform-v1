"""图谱发布 Skill 的独立运行实现。"""

from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.skills.base import Skill
from kg_agent.skills.graph_publish import GraphPublishSkill
from kg_agent.skills.llm import ArtifactContentReader


def build_skill(
    registration: SkillRegistration,
    *,
    model_gateway: ModelGateway,
    artifact_reader: ArtifactContentReader,
    artifact_registry: ArtifactRegistry,
    skill_prompt: str,
) -> Skill:
    """构造确定性的图谱发布器；PublishGuard 仍由 Registry 在调用前校验。"""

    del model_gateway, artifact_registry, skill_prompt
    return GraphPublishSkill(registration, artifact_reader=artifact_reader)
