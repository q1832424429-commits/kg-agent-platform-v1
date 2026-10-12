"""验证脚本字段映射不猜语义、不吞缺失值。"""

from uuid import uuid4

import pytest

from kg_agent.extensions.script_field_mapping import (
    ScriptInputFieldBinding,
    detect_script_input_fields,
    list_script_source_fields,
    project_script_input_fields,
)


def test_static_scan_only_reports_declared_input_field_access() -> None:
    """只识别脚本读取的 input_fields 别名，不把普通字面量猜成输入。"""

    source = (
        b"def run(inputs, parameters):\n"
        b"    mapped = parameters.get('input_fields', {})\n"
        b"    return mapped['devices'], mapped.get('faults'), 'unrelated'\n"
    )

    assert detect_script_input_fields(source) == ["devices", "faults"]


def test_source_options_only_include_registered_content_fields() -> None:
    """映射建议的来源必须来自当前受控输入 Schema。"""

    from kg_agent.registries.artifact_registry import ArtifactRegistry

    options = list_script_source_fields(ArtifactRegistry.v1(), ["EntityDataset"])
    assert ("EntityDataset", "/entities") in {
        (item.artifact_type, item.source_pointer) for item in options
    }


def test_confirmed_alias_reads_existing_artifact_field() -> None:
    """脚本参数名与 Artifact 字段不同，也能按确认的路径读取。"""

    binding = ScriptInputFieldBinding(
        script_field="devices",
        artifact_type="EntityDataset",
        source_pointer="/entities",
    )
    value = project_script_input_fields(
        [
            {
                "artifact_id": str(uuid4()),
                "artifact_type": "EntityDataset",
                "content": {"entities": [{"name": "液压泵"}]},
            }
        ],
        [binding],
    )

    assert value == {"devices": [{"name": "液压泵"}]}


def test_removed_nonstandard_source_cannot_be_used() -> None:
    """旧映射若指向非标准内容，应明确拒绝而不是误读主内容。"""

    with pytest.raises(ValueError, match="标准 Artifact 内容"):
        ScriptInputFieldBinding.model_validate(
            {
                "script_field": "faults",
                "artifact_type": "EntityDataset",
                "source_pointer": "/faults",
                "source_scope": "EXTENSION",
                "source_namespace": "fault_analysis",
            }
        )


def test_missing_required_field_blocks_script() -> None:
    """上游未产生已确认的必填字段时不得把空值塞给脚本。"""

    binding = ScriptInputFieldBinding(
        script_field="devices",
        artifact_type="EntityDataset",
        source_pointer="/entities",
    )
    with pytest.raises(ValueError, match="不存在"):
        project_script_input_fields(
            [{"artifact_type": "EntityDataset", "content": {"other": []}}],
            [binding],
        )


def test_two_unrelated_same_type_inputs_are_ambiguous() -> None:
    """同类但无明确来源绑定的两个产物不能靠列表顺序猜测。"""

    binding = ScriptInputFieldBinding(
        script_field="devices",
        artifact_type="EntityDataset",
        source_pointer="/entities",
    )
    with pytest.raises(ValueError, match="实际得到 2 个"):
        project_script_input_fields(
            [
                {"artifact_type": "EntityDataset", "content": {"entities": [{"name": "A"}]}},
                {"artifact_type": "EntityDataset", "content": {"entities": [{"name": "B"}]}},
            ],
            [binding],
        )
