"""提供各 Skill 包共享的 LLM 运行构造函数。

这是公共基础设施，不是可注册 Skill；具体 Skill 的业务入口始终位于其自身目录。
"""

from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.skills.base import Skill
from kg_agent.skills.llm import ArtifactContentReader, LLMGeneratedSkill


def build_llm_skill(
    registration: SkillRegistration,
    *,
    model_gateway: ModelGateway,
    artifact_reader: ArtifactContentReader,
    artifact_registry: ArtifactRegistry,
    skill_prompt: str,
) -> Skill:
    """基于当前 Skill 自己的提示词构造标准 LLM 实现。"""

    return LLMGeneratedSkill(
        registration,
        model_gateway=model_gateway,
        artifact_reader=artifact_reader,
        artifact_registry=artifact_registry,
        skill_prompt=skill_prompt,
    )
