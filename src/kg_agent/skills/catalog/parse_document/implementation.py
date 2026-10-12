"""文档解析 Skill 的独立运行实现。"""

from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.skills.base import Skill
from kg_agent.skills.catalog._llm import build_llm_skill
from kg_agent.skills.document_parse import DocxAwareParseDocumentSkill
from kg_agent.skills.llm import ArtifactContentReader


def build_skill(
    registration: SkillRegistration,
    *,
    model_gateway: ModelGateway,
    artifact_reader: ArtifactContentReader,
    artifact_registry: ArtifactRegistry,
    skill_prompt: str,
) -> Skill:
    """构造 DOCX 优先、LLM 回退的文档解析实现。"""

    fallback = build_llm_skill(
        registration,
        model_gateway=model_gateway,
        artifact_reader=artifact_reader,
        artifact_registry=artifact_registry,
        skill_prompt=skill_prompt,
    )
    return DocxAwareParseDocumentSkill(
        registration, artifact_reader=artifact_reader, fallback=fallback
    )
