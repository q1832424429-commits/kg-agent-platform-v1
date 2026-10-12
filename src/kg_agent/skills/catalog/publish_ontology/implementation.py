"""提供 publish_ontology 的确定性发布运行实现。"""

from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.skills.base import Skill
from kg_agent.skills.llm import ArtifactContentReader
from kg_agent.skills.ontology_publish import OntologyPublishSkill


def build_skill(
    registration: SkillRegistration,
    *,
    model_gateway: ModelGateway,
    artifact_reader: ArtifactContentReader,
    artifact_registry: ArtifactRegistry,
    skill_prompt: str,
) -> Skill:
    """构造确定性本体发布器；模型与提示词不参与已批准事实的复制。"""

    del model_gateway, artifact_registry, skill_prompt
    return OntologyPublishSkill(registration, artifact_reader=artifact_reader)
