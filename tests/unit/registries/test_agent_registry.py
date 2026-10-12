"""验证专业 Agent 能力目录加载、排序和重复保护。"""

from pathlib import Path

import pytest

from kg_agent.domain.registry_models import AgentRegistration
from kg_agent.exceptions import RegistryValidationError
from kg_agent.registries.agent_registry import AgentRegistry


def test_registry_loads_exactly_five_professional_agents(project_root: Path) -> None:
    """第一阶段能力目录只能包含已经确认的五个专业 Agent。"""

    registry = AgentRegistry.from_directory(project_root / "configs" / "agents")
    agent_types = {item.agent_type for item in registry.list_all()}

    assert agent_types == {
        "document_parsing_agent",
        "knowledge_extraction_agent",
        "knowledge_fusion_agent",
        "quality_evaluation_agent",
        "graph_management_agent",
    }


def test_registry_rejects_duplicate_agent_type() -> None:
    """同一逻辑 Agent 类型不能重复注册。"""

    registration = AgentRegistration(
        agent_type="document_parsing_agent",
        name="数据源解析 Agent",
        version="1.0.0",
        description="测试",
        accepted_subtask_types=["document_parse"],
        accepted_input_types=["RawDocument"],
        produced_output_types=["SemanticDocument"],
        allowed_skills=["parse_document"],
        lifecycle="ACTIVE",
    )
    registry = AgentRegistry([registration])

    with pytest.raises(RegistryValidationError):
        registry.register(registration)
