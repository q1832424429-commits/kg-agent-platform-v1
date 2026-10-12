"""验证五个专业 Agent 的版本化职责提示与 Registry 保持一致。"""

from pathlib import Path

import pytest

from kg_agent.agents.professional.prompts import ProfessionalPromptRepository


@pytest.mark.parametrize(
    ("agent_type", "required_phrases"),
    [
        (
            "document_parsing_agent",
            ("parse_document", "build_document_manifest", "RawDocument"),
        ),
        (
            "knowledge_extraction_agent",
            ("extract_knowledge", "联合输出", "PublishedOntology"),
        ),
        (
            "knowledge_fusion_agent",
            ("align_entities", "normalize_relations", "analyze_conflicts"),
        ),
        (
            "quality_evaluation_agent",
            ("evaluate_graph_quality", "evaluate_evidence_coverage", "不评估候选本体"),
        ),
        (
            "graph_management_agent",
            ("build_candidate_graph", "publish_ontology", "publish_graph"),
        ),
    ],
)
def test_role_prompt_explains_skill_selection_and_boundary(
    project_root: Path,
    agent_type: str,
    required_phrases: tuple[str, ...],
) -> None:
    """每个角色应说明自己的 Skill 语义，并排除越权职责表述。"""

    repository = ProfessionalPromptRepository(
        project_root / "configs" / "prompts" / "professional"
    )

    prompt = repository.load_system_prompt(agent_type)

    assert all(phrase in prompt for phrase in required_phrases)
    assert "最短 Skill 调用方案" in prompt
    assert "运行时会用 Registry 和校验器独立检查" in prompt


def test_fusion_prompt_assigns_candidate_graph_to_graph_management(
    project_root: Path,
) -> None:
    """知识融合 Agent 只能做冲突分析，候选图谱必须归图谱管理 Agent。"""

    repository = ProfessionalPromptRepository(
        project_root / "configs" / "prompts" / "professional"
    )

    prompt = repository.load_system_prompt("knowledge_fusion_agent")

    assert "`CandidateGraph` 不属于本角色" in prompt
    assert "候选图谱由图谱管理更新 Agent 构建" in prompt
