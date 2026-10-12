"""验证属性脚本的跨列表映射建议和非空输入保全门禁。"""

import io
import json
import sys
from pathlib import Path

import pytest

from kg_agent.extensions.contract_profiles import ContractProfileRegistry
from kg_agent.extensions.script_adapter_generator import GENERATED_ENTRYPOINT, render_function_entry
from kg_agent.extensions.script_contract_validation import (
    ScriptCandidateOutput,
    ScriptCandidateResponse,
    ScriptContractValidator,
    validate_property_transform_result,
)
from kg_agent.extensions.script_entry import main as script_entry_main
from kg_agent.extensions.script_field_mapping import (
    ScriptFunctionAdapter,
    ScriptFunctionArgument,
    ScriptNestedFieldBinding,
    list_nested_source_fields,
    list_script_source_fields,
    recommend_function_field_bindings,
    unresolved_function_input_paths,
)
from kg_agent.registries.artifact_registry import ArtifactRegistry


def _property() -> dict[str, object]:
    """返回符合正式契约的单条属性，用于测试身份与证据保全。"""

    return {
        "property_id": "p1",
        "entity_id": "e1",
        "property_type_id": "height",
        "value": 1.75,
        "evidence": [],
    }


def test_property_transform_rejects_empty_output() -> None:
    """上游有属性时，空输出不能仅凭 Pydantic 格式正确就通过。"""

    inputs = [{"artifact_type": "PropertyDataset", "content": {"properties": [_property()]}}]
    parsed = ScriptCandidateResponse(
        outputs=[
            ScriptCandidateOutput(
                artifact_type="PropertyDataset",
                content={"schema_version": "1.0.0", "properties": []},
                content_summary="空结果",
            )
        ]
    )
    with pytest.raises(ValueError, match="输入 1 条，脚本输出 0 条"):
        validate_property_transform_result(inputs, parsed)


def test_list_alias_proposal_marks_missing_unit_unresolved() -> None:
    """只推荐注册字段的列表和同名字段，不为缺失单位编造来源。"""

    registry = ArtifactRegistry.v1()
    choices = list_nested_source_fields(registry, ["PropertyDataset"])
    suggested, unresolved = recommend_function_field_bindings(
        "payload",
        "PropertyDataset",
        [
            "/attributes",
            "/attributes/*/entity_id",
            "/attributes/*/value",
            "/attributes/*/unit",
        ],
        choices,
    )
    pairs = {(item.source_pointer, item.target_pointer) for item in suggested}
    assert ("/properties", "/attributes") in pairs
    assert ("/properties/*/entity_id", "/attributes/*/entity_id") in pairs
    assert ("/properties/*/value", "/attributes/*/value") in pairs
    assert "/attributes/*/unit" in unresolved


def test_existing_scripts_get_generic_alias_candidates_and_missing_report() -> None:
    """不同列表名称共用结构建议；缺失数据不得被映射规则编造。"""

    registry = ArtifactRegistry.v1()
    sources = [
        *list_script_source_fields(registry, ["EntityDataset"]),
        *list_nested_source_fields(registry, ["EntityDataset"]),
    ]
    for collection in ("devices", "parts"):
        paths = [
            f"/{collection}",
            f"/{collection}/*/id",
            f"/{collection}/*/label",
        ]
        suggestions, unresolved = recommend_function_field_bindings(
            "payload", "EntityDataset", paths, sources
        )
        assert unresolved == []
        assert {
            (item.source_pointer, item.target_pointer) for item in suggestions
        } == {
            ("/entities", f"/{collection}"),
            ("/entities/*/entity_id", f"/{collection}/*/id"),
            ("/entities/*/name", f"/{collection}/*/label"),
        }
        assert unresolved_function_input_paths(
            "payload", "EntityDataset", paths, sources, suggestions
        ) == []
        assert unresolved_function_input_paths(
            "payload", "EntityDataset", paths, sources, suggestions[:1]
        ) == [f"/{collection}/*/id", f"/{collection}/*/label"]
        assert unresolved_function_input_paths(
            "payload",
            "EntityDataset",
            [*paths, f"/{collection}/*/unit"],
            sources,
            suggestions,
        ) == [f"/{collection}/*/unit"]


def test_generated_entry_applies_confirmed_list_alias(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """隔离入口只改输入副本，原上游属性产物保持不变。"""

    (tmp_path / "business.py").write_text(
        "def run(payload):\n    return {'received': payload['attributes'][0]['id']}\n",
        encoding="utf-8",
    )
    adapter = ScriptFunctionAdapter(
        function_name="run",
        arguments=[
            ScriptFunctionArgument(
                parameter_name="payload",
                source="ARTIFACT_CONTENT",
                artifact_type="PropertyDataset",
            )
        ],
        field_bindings=[
            ScriptNestedFieldBinding(
                parameter_name="payload",
                artifact_type="PropertyDataset",
                source_pointer="/properties",
                target_pointer="/attributes",
            ),
            ScriptNestedFieldBinding(
                parameter_name="payload",
                artifact_type="PropertyDataset",
                source_pointer="/properties/*/property_id",
                target_pointer="/attributes/*/id",
            ),
        ],
    )
    entry = tmp_path / GENERATED_ENTRYPOINT
    entry.write_bytes(render_function_entry("business.py", adapter))
    original = {"schema_version": "1.0.0", "properties": [_property()]}
    payload = {
        "inputs": [{"artifact_type": "PropertyDataset", "content": original}],
        "parameters": {},
    }
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["script_entry.py", str(entry)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", stdout)
    script_entry_main()
    assert json.loads(stdout.getvalue()) == {"received": "p1"}
    assert "attributes" not in original


class _EmptyPropertyRunner:
    """模拟把非空契约样本处理成空属性的隔离脚本。"""

    async def execute(self, **_kwargs: object) -> dict[str, object]:
        """返回格式合法但业务数据被清空的结果。"""

        return {
            "result": {
                "outputs": [
                    {
                        "artifact_type": "PropertyDataset",
                        "content": {"schema_version": "1.0.0", "properties": []},
                        "content_summary": "空属性",
                    }
                ]
            }
        }


def test_property_output_mapping_preserves_source_identity_and_evidence() -> None:
    """已确认属性 ID 和新值的映射只更新值，不丢失类型与证据。"""

    from kg_agent.extensions.script_contract_validation import normalize_script_result
    from kg_agent.extensions.script_field_mapping import ScriptOutputFieldBinding

    parent = _property()
    inputs = [{"artifact_type": "PropertyDataset", "content": {"properties": [parent]}}]
    parsed = normalize_script_result(
        {"normalized_attributes": [{"attr_id": "p1", "canonical_value": 175}]},
        ("PropertyDataset",),
        inputs,
        (
            ScriptOutputFieldBinding(
                artifact_type="PropertyDataset",
                source_pointer="/normalized_attributes",
                target_field="properties",
                record_field_bindings={
                    "property_id": "attr_id",
                    "value": "canonical_value",
                },
            ),
        ),
    )
    validate_property_transform_result(inputs, parsed)
    assert parsed.outputs[0].content["properties"] == [{**parent, "value": 175}]


@pytest.mark.asyncio
async def test_property_contract_check_uses_nonempty_fixture() -> None:
    """发布前必须以非空属性样本发现空输出，不能批准这类脚本。"""

    registry = ArtifactRegistry.v1()
    validator = ScriptContractValidator(registry, _EmptyPropertyRunner())
    result = await validator.validate(
        entrypoint="main.py",
        resources=(("main.py", b"def run(inputs, parameters): return {}\n"),),
        profile=ContractProfileRegistry.v1(registry).require("property_transform"),
        timeout_seconds=30,
    )
    assert result.passed is False
    assert "输入 1 条，脚本输出 0 条" in result.diagnostics[-1]
