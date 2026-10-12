"""加载并校验五个专业 Agent 的最小能力目录。

Registry 只声明专业 Agent 能力，不创建或运行专业 Agent，也不把能力硬编码到总控逻辑。
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import ValidationError

from kg_agent.domain.registry_models import AgentRegistration
from kg_agent.exceptions import RegistryValidationError


class AgentRegistry:
    """以内存索引管理经过契约校验的专业 Agent 注册信息。"""

    def __init__(self, registrations: list[AgentRegistration] | None = None) -> None:
        """创建 Registry 并注册初始能力声明。"""

        self._registrations: dict[str, AgentRegistration] = {}
        for registration in registrations or []:
            self.register(registration)

    @classmethod
    def from_directory(cls, directory: Path) -> AgentRegistry:
        """从目录中按文件名顺序读取 YAML 能力声明。

        Args:
            directory: 包含专业 Agent YAML 文件的目录。

        Returns:
            完成 Pydantic 校验和重复检查的 Registry。

        Raises:
            RegistryValidationError: 文件无法解析或注册内容不合法时抛出。
        """

        registry = cls()
        for path in sorted(directory.glob("*.yaml")):
            try:
                payload = yaml.safe_load(path.read_text(encoding="utf-8"))
                registration = AgentRegistration.model_validate(payload)
                registry.register(registration)
            except (OSError, yaml.YAMLError, ValidationError) as exc:
                raise RegistryValidationError(f"无法加载 Agent 配置：{path}") from exc
        if not registry._registrations:
            raise RegistryValidationError(f"Agent 配置目录为空：{directory}")
        return registry

    def register(self, registration: AgentRegistration) -> None:
        """注册一个专业 Agent 能力声明。

        Raises:
            RegistryValidationError: 同一 agent_type 已经存在时抛出。
        """

        if registration.agent_type in self._registrations:
            raise RegistryValidationError(f"Agent 类型重复注册：{registration.agent_type}")
        self._registrations[registration.agent_type] = registration

    def get(self, agent_type: str) -> AgentRegistration | None:
        """按逻辑 Agent 类型返回注册信息；不存在时返回 None。"""

        return self._registrations.get(agent_type)

    def require(self, agent_type: str) -> AgentRegistration:
        """返回注册信息，不存在时抛出结构化 Registry 异常。"""

        registration = self.get(agent_type)
        if registration is None:
            raise RegistryValidationError(f"未知 Agent 类型：{agent_type}")
        return registration

    def list_all(self) -> list[AgentRegistration]:
        """按 agent_type 稳定排序返回全部注册信息。"""

        return [self._registrations[key] for key in sorted(self._registrations)]
