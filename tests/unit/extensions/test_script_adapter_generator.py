"""验证平台生成入口与用户已有 main.py 可以共存并产生标准实体产物。"""

import io
import json
import sys
from pathlib import Path

import pytest

from kg_agent.extensions.script_adapter_generator import (
    GENERATED_ENTRYPOINT,
    render_entity_record_entry,
)
from kg_agent.extensions.script_entry import main as script_entry_main
from kg_agent.extensions.script_field_mapping import EntityRecordAdapter


def _run_generated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, original: str, *, result_field: str
) -> dict[str, object]:
    """在测试目录执行生成入口，并返回脚本标准响应。"""

    original_path = tmp_path / "main.py"
    original_path.write_text(original, encoding="utf-8")
    entrypoint = tmp_path / GENERATED_ENTRYPOINT
    entrypoint.write_bytes(
        render_entity_record_entry(
            "main.py",
            EntityRecordAdapter(
                function_name="main",
                parameter_name="device_name",
                result_field=result_field,
            ),
        )
    )
    input_payload = {
        "inputs": [
            {
                "artifact_type": "EntityDataset",
                "content": {
                    "schema_version": "1.0.0",
                    "entities": [
                        {
                            "entity_id": "e-1",
                            "name": "液压泵总成",
                            "class_id": "equipment",
                            "aliases": [],
                            "evidence": [{"locator": "p1"}],
                        }
                    ],
                },
            }
        ],
        "parameters": {},
    }
    output = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["script_entry.py", str(entrypoint)])
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(input_payload)))
    monkeypatch.setattr(sys, "stdout", output)
    script_entry_main()
    assert original_path.read_text(encoding="utf-8") == original
    return json.loads(output.getvalue())


def test_existing_main_function_can_return_name_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """已有 main.py 和 main 函数无需改名，平台入口保留实体 ID 与证据。"""

    result = _run_generated(
        tmp_path,
        monkeypatch,
        "def main(device_name):\n    return device_name.replace('总成', '')\n",
        result_field="__value__",
    )
    entity = result["outputs"][0]["content"]["entities"][0]
    assert entity["name"] == "液压泵"
    assert entity["aliases"] == ["液压泵总成"]
    assert entity["entity_id"] == "e-1"
    assert entity["evidence"] == [{"locator": "p1"}]


def test_existing_main_function_can_return_named_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """字典返回值只读取用户确认的字段，不消费其他不明数据。"""

    result = _run_generated(
        tmp_path,
        monkeypatch,
        "def main(device_name):\n    return {'standard_name': device_name.replace('总成', '')}\n",
        result_field="standard_name",
    )
    assert result["outputs"][0]["content"]["entities"][0]["name"] == "液压泵"


def test_generated_entry_rejects_missing_confirmed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """脚本未返回确认字段时直接失败，不生成半成品。"""

    with pytest.raises(ValueError, match="标准名称"):
        _run_generated(
            tmp_path,
            monkeypatch,
            "def main(device_name):\n    return {'other': device_name}\n",
            result_field="standard_name",
        )
