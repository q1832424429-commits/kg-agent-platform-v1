"""统一知识抽取 Skill 的独立运行入口。"""

from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.skills.base import Skill
from kg_agent.skills.knowledge_extraction import KnowledgeExtractionSkill
from kg_agent.skills.llm import ArtifactContentReader


def build_skill(
    registration: SkillRegistration,
    *,
    model_gateway: ModelGateway,
    artifact_reader: ArtifactContentReader,
    artifact_registry: ArtifactRegistry,
    skill_prompt: str,
) -> Skill:
    """构造带跨产物语义完整性校验的统一知识抽取 Skill。"""

    return KnowledgeExtractionSkill(
        registration,
        model_gateway=model_gateway,
        artifact_reader=artifact_reader,
        artifact_registry=artifact_registry,
        skill_prompt=skill_prompt,
    )
