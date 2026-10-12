"""提供第一阶段测试共用的 Agent Registry 和 Prompt 路径。"""

from pathlib import Path

import pytest

from kg_agent.registries.agent_registry import AgentRegistry


@pytest.fixture
def project_root() -> Path:
    """返回当前项目根目录。"""

    return Path(__file__).resolve().parents[1]


@pytest.fixture
def agent_registry(project_root: Path) -> AgentRegistry:
    """加载仓库内五个专业 Agent 的最小能力目录。"""

    return AgentRegistry.from_directory(project_root / "configs" / "agents")
