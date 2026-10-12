"""管理五类专业 Agent 运行实例，并按注册角色确定性查找。"""

from kg_agent.agents.professional.agent import BaseProfessionalAgent
from kg_agent.exceptions import RegistryValidationError


class ProfessionalAgentPool:
    """保存已组装的真实专业 Agent；不负责 Assignment 决策或认领。"""

    def __init__(self, agents: list[BaseProfessionalAgent]) -> None:
        """按 agent_type 建立索引并拒绝重复角色。"""

        self._agents: dict[str, BaseProfessionalAgent] = {}
        for agent in agents:
            name = agent.registration.agent_type
            if name in self._agents:
                raise RegistryValidationError(f"专业 Agent 运行实例重复：{name}")
            self._agents[name] = agent

    def require(self, agent_type: str) -> BaseProfessionalAgent:
        """返回指定角色实例；未组装的角色不能由 Worker 执行。"""

        try:
            return self._agents[agent_type]
        except KeyError as exc:
            raise RegistryValidationError(f"专业 Agent 尚未组装：{agent_type}") from exc

    def list_types(self) -> list[str]:
        """稳定返回 Worker 可运行的全部角色。"""

        return sorted(self._agents)
