"""验证非标准用户函数可经确认映射适配，且原脚本不被改写。"""

import io
import json
import sys
from pathlib import Path

import pytest

from kg_agent.extensions.script_adapter_generator import (
    GENERATED_ENTRYPOINT,
    render_function_entry,
)
from kg_agent.extensions.script_entry import main as script_entry_main
from kg_agent.extensions.script_field_mapping import (
    ScriptFunctionAdapter,
    ScriptFunctionArgument,
)
from kg_agent.extensions.script_signature import inspect_script_functions


def test_static_signature_distinguishes_standard_and_nonstandard_run() -> None:
    """仅精确的双参数协议是标准入口，其他函数须确认适配。"""

    one = inspect_script_functions(b"def run(payload): return payload\n")
    assert one["standard_entrypoint"] is False
    assert one["functions"][0]["parameters"] == ["payload"]
    two = inspect_script_functions(b"def run(inputs, parameters): return {}\n")
    assert two["standard_entrypoint"] is True
    named = inspect_script_functions(b"def analyze(entities, rules): return {}\n")
    assert named["functions"][0]["parameters"] == ["entities", "rules"]
    content = inspect_script_functions(
        b"def run(payload):\n    return payload.get('entities', [])\n"
    )
    assert content["functions"][0]["accessed_keys"]["payload"] == ["entities"]


def test_static_reads_distinguish_required_from_optional_comprehension_fields() -> None:
    """列表推导式的方括号字段是必需项，带默认值的 get 不应被误拦截。"""

    from kg_agent.extensions.script_signature import detect_function_field_paths

    source = (
        b"def run(payload):\n"
        b"    return [row['id'] for row in payload.get('items', []) "
        b"if row.get('enabled', True)]\n"
    )
    assert detect_function_field_paths(source, "run")["payload"] == [
        "/items",
        "/items/*/enabled",
        "/items/*/id",
    ]
    assert detect_function_field_paths(
        source, "run", required_only=True
    )["payload"] == ["/items/*/id"]


def test_generated_entry_can_pass_complete_artifact_content(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单参数脚本可接收完整上游 content，既不强制解析平台 envelope 也不丢字段。"""

    (tmp_path / "business.py").write_text(
        "def run(payload):\n    return {'entities': payload['entities']}\n",
        encoding="utf-8",
    )
    adapter = ScriptFunctionAdapter(
        function_name="run",
        arguments=[
            ScriptFunctionArgument(
                parameter_name="payload",
                source="ARTIFACT_CONTENT",
                artifact_type="EntityDataset",
            )
        ],
    )
    entry = tmp_path / GENERATED_ENTRYPOINT
    entry.write_bytes(render_function_entry("business.py", adapter))
    payload = {
        "inputs": [
            {
                "artifact_type": "EntityDataset",
                "content": {"entities": [{"entity_id": "e1", "name": "液压泵"}]},
            }
        ],
        "parameters": {},
    }
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["script_entry.py", str(entry)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", stdout)
    script_entry_main()
    assert json.loads(stdout.getvalue())["entities"][0]["entity_id"] == "e1"


def test_generated_entry_exposes_plain_list_for_output_mapping(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有函数返回记录列表时，保留记录供后续受控输出映射观察。"""

    (tmp_path / "business.py").write_text(
        "def process(payload):\n"
        "    return [{'record_key': row['entity_id']} for row in payload['entities']]\n",
        encoding="utf-8",
    )
    adapter = ScriptFunctionAdapter(
        function_name="process",
        arguments=[
            ScriptFunctionArgument(
                parameter_name="payload",
                source="ARTIFACT_CONTENT",
                artifact_type="EntityDataset",
            )
        ],
    )
    entry = tmp_path / GENERATED_ENTRYPOINT
    entry.write_bytes(render_function_entry("business.py", adapter))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["script_entry.py", str(entry)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({
        "inputs": [{
            "artifact_type": "EntityDataset",
            "content": {"entities": [{"entity_id": "e1"}]},
        }],
        "parameters": {},
    })))
    monkeypatch.setattr(sys, "stdout", stdout)
    script_entry_main()
    assert json.loads(stdout.getvalue()) == {"result": [{"record_key": "e1"}]}


def test_generated_entry_calls_confirmed_business_function(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """生成入口从已确认资料字段取值；业务脚本保持原样并返回标准响应。"""

    source = (
        "def analyze(entities, rules):\n"
        "    return {'outputs': [{'artifact_type': 'EntityDataset', "
        "'content': {'schema_version': '1.0.0', 'entities': entities}, "
        "'content_summary': rules}]}\n"
    )
    (tmp_path / "business.py").write_text(source, encoding="utf-8")
    adapter = ScriptFunctionAdapter(
        function_name="analyze",
        arguments=[
            ScriptFunctionArgument(
                parameter_name="entities",
                source="ARTIFACT_FIELD",
                artifact_type="EntityDataset",
                source_pointer="/entities",
            ),
            ScriptFunctionArgument(parameter_name="rules", source="TASK_PAYLOAD"),
        ],
    )
    entry = tmp_path / GENERATED_ENTRYPOINT
    entry.write_bytes(render_function_entry("business.py", adapter))
    payload = {
        "inputs": [{"artifact_type": "EntityDataset", "content": {"entities": [{"name": "泵"}]}}],
        "parameters": {"input_fields": {"entities": [{"name": "泵"}]}},
    }
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["script_entry.py", str(entry)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", stdout)
    script_entry_main()
    result = json.loads(stdout.getvalue())
    assert result["outputs"][0]["content"]["entities"] == [{"name": "泵"}]
    assert result["outputs"][0]["content_summary"]["inputs"] == payload["inputs"]
    assert (tmp_path / "business.py").read_text(encoding="utf-8") == source


def test_nested_field_recommendation_uses_uploaded_code_and_registered_schema() -> None:
    """从真实脚本访问路径推荐字段来源，不把未注册字段当作事实。"""

    from kg_agent.extensions.script_field_mapping import (
        list_nested_source_fields,
        recommend_nested_field_bindings,
    )
    from kg_agent.extensions.script_signature import detect_function_field_paths
    from kg_agent.registries.artifact_registry import ArtifactRegistry

    source = (
        b"def run(payload):\n"
        b"    entities = payload.get('entities') or []\n"
        b"    for entity in entities:\n"
        b"        print(entity['id'], entity['type'], entity.get('aliases'))\n"
    )
    paths = detect_function_field_paths(source, "run")
    options = list_nested_source_fields(ArtifactRegistry.v1(), ["EntityDataset"])
    bindings, unresolved = recommend_nested_field_bindings(
        "payload", "EntityDataset", paths["payload"], options
    )
    assert {(item.source_pointer, item.target_pointer) for item in bindings} == {
        ("/entities/*/entity_id", "/entities/*/id"),
        ("/entities/*/class_id", "/entities/*/type"),
    }
    assert unresolved == []


def test_confirmed_nested_mapping_is_used_by_generated_entry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """隔离入口传字段别名副本给旧脚本，不改变原始 Artifact 内容。"""

    from kg_agent.extensions.script_field_mapping import ScriptNestedFieldBinding

    (tmp_path / "business.py").write_text(
        "def run(payload):\n"
        "    return {'entities': [{'entity_id': e['id'], 'class_id': e['type']} "
        "for e in payload['entities']]}\n",
        encoding="utf-8",
    )
    adapter = ScriptFunctionAdapter(
        function_name="run",
        arguments=[
            ScriptFunctionArgument(
                parameter_name="payload",
                source="ARTIFACT_CONTENT",
                artifact_type="EntityDataset",
            )
        ],
        field_bindings=[
            ScriptNestedFieldBinding(
                parameter_name="payload",
                artifact_type="EntityDataset",
                source_pointer="/entities/*/entity_id",
                target_pointer="/entities/*/id",
            ),
            ScriptNestedFieldBinding(
                parameter_name="payload",
                artifact_type="EntityDataset",
                source_pointer="/entities/*/class_id",
                target_pointer="/entities/*/type",
            ),
        ],
    )
    entry = tmp_path / GENERATED_ENTRYPOINT
    entry.write_bytes(render_function_entry("business.py", adapter))
    payload = {
        "inputs": [
            {
                "artifact_type": "EntityDataset",
                "content": {"entities": [{"entity_id": "e1", "class_id": "equipment"}]},
            }
        ],
        "parameters": {},
    }
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["script_entry.py", str(entry)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    monkeypatch.setattr(sys, "stdout", stdout)
    script_entry_main()
    assert json.loads(stdout.getvalue())["entities"] == [
        {"entity_id": "e1", "class_id": "equipment"}
    ]
    assert "id" not in payload["inputs"][0]["content"]["entities"][0]
