"""验证用户逐项实体脚本只修改已确认名称并保留标准 Artifact 身份。"""

from types import ModuleType

import pytest

from kg_agent.extensions.script_entry import _run_entity_record_adapter
from kg_agent.extensions.script_field_mapping import EntityRecordAdapter


def _input() -> list[dict[str, object]]:
    """构造含两条实体和证据的上游标准数据。"""

    return [
        {
            "artifact_type": "EntityDataset",
            "content": {
                "schema_version": "1.0.0",
                "entities": [
                    {
                        "entity_id": "e1",
                        "name": "液压泵总成",
                        "class_id": "equipment",
                        "aliases": [],
                        "evidence": [{"locator": "p1"}],
                    },
                    {
                        "entity_id": "e2",
                        "name": "电机",
                        "class_id": "equipment",
                        "aliases": [],
                        "evidence": [{"locator": "p2"}],
                    },
                ],
            },
        }
    ]


def test_entity_script_maps_values_and_preserves_identity() -> None:
    """平台逐项传参和组装，脚本无需知道 EntityDataset 的路径。"""

    module = ModuleType("user_skill")
    module.normalize = lambda device_name: {"standard_name": device_name.replace("总成", "")}
    config = EntityRecordAdapter(
        function_name="normalize", parameter_name="device_name", result_field="standard_name"
    )
    result = _run_entity_record_adapter(module, _input(), config.model_dump())
    output = result["outputs"][0]["content"]["entities"]
    assert [item["name"] for item in output] == ["液压泵", "电机"]
    assert output[0]["aliases"] == ["液压泵总成"]
    assert output[0]["entity_id"] == "e1"
    assert output[0]["evidence"] == [{"locator": "p1"}]
    assert output[1]["entity_id"] == "e2"


def test_entity_script_missing_result_stops_before_artifact() -> None:
    """结果缺少已确认字段时不能形成半成品并交给下游。"""

    module = ModuleType("user_skill")
    module.normalize = lambda device_name: {"other": device_name}
    with pytest.raises(ValueError, match="标准名称"):
        _run_entity_record_adapter(
            module,
            _input(),
            EntityRecordAdapter(
                function_name="normalize",
                parameter_name="device_name",
                result_field="standard_name",
            ).model_dump(),
        )
