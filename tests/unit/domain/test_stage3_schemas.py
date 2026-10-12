"""验证第三阶段 Artifact 与 Skill 公共契约可以导出 JSON Schema。"""

from kg_agent.domain.schema_export import export_all_contract_schemas


def test_stage3_contracts_are_exported() -> None:
    """Artifact 写入、复用和 Skill 调用结果必须进入统一 Schema 目录。"""

    schemas = export_all_contract_schemas()

    assert {
        "ArtifactCreateCommand",
        "ArtifactRecord",
        "ArtifactReuseRequest",
        "ArtifactReuseDecision",
        "SkillInvocation",
        "SkillArtifactOutput",
        "SkillRunResult",
    } <= schemas.keys()
