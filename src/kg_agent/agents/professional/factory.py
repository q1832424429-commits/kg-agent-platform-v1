"""根据 Registry 为五类已确认角色组装真实专业 Agent。"""

from kg_agent.agents.professional.agent import ArtifactWriter, BaseProfessionalAgent
from kg_agent.agents.professional.prompts import ProfessionalPromptRepository
from kg_agent.agents.professional.registry import ProfessionalAgentPool
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry


def build_professional_agent_pool(
    *,
    model_gateway: ModelGateway,
    agent_registry: AgentRegistry,
    skill_registry: SkillRegistry,
    artifact_registry: ArtifactRegistry,
    artifact_writer: ArtifactWriter,
    prompt_repository: ProfessionalPromptRepository,
    max_repairs: int = 2,
) -> ProfessionalAgentPool:
    """为每个注册角色创建运行骨架，并绑定当前 Registry 中的 Skill。"""

    skill_registry.validate_against_agents(agent_registry)
    return ProfessionalAgentPool(
        [
            BaseProfessionalAgent(
                registration=registration,
                model_gateway=model_gateway,
                agent_registry=agent_registry,
                skill_registry=skill_registry,
                artifact_registry=artifact_registry,
                artifact_writer=artifact_writer,
                prompt_repository=prompt_repository,
                max_repairs=max_repairs,
            )
            for registration in agent_registry.list_all()
        ]
    )
