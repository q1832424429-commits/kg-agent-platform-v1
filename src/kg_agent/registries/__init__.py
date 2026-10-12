"""导出 Agent、Artifact 与 Skill 三类注册中心。"""

from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry

__all__ = ["AgentRegistry", "ArtifactRegistry", "SkillRegistry"]
