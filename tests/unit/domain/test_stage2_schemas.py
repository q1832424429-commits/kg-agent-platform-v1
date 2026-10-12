"""验证第二阶段新增公共契约可以生成稳定 JSON Schema。"""

from kg_agent.domain.schema_export import export_all_contract_schemas


def test_stage2_contracts_are_exported() -> None:
    """TaskEvent 和两类 Context View 必须进入统一 Schema 目录。"""

    schemas = export_all_contract_schemas()
    assert {
        "TaskEvent",
        "ControllerContextView",
        "ProfessionalAgentContextView",
    } <= schemas.keys()
