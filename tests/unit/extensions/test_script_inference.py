"""验证自定义 Skill 仅依据已上传资源自动确定脚本入口。"""

import pytest

from kg_agent.exceptions import CustomExtensionValidationError
from kg_agent.extensions.contracts import CustomSkillDefinitionInput
from kg_agent.extensions.service import CustomExtensionService


def test_without_python_resource_uses_model_path() -> None:
    """只有提示词或参考资料时，不应误判为脚本 Skill。"""

    assert CustomExtensionService._infer_script_entrypoint(["references/domain.md"]) is None


def test_single_python_resource_becomes_entrypoint() -> None:
    """上传一个 Python 文件时，该文件应自动成为固定入口。"""

    assert CustomExtensionService._infer_script_entrypoint(["scripts/run.py"]) == "scripts/run.py"


def test_multiple_python_resources_require_one_main() -> None:
    """多脚本包必须包含唯一 main.py，避免服务端猜测执行文件。"""

    assert (
        CustomExtensionService._infer_script_entrypoint(["helpers.py", "app/main.py"])
        == "app/main.py"
    )
    with pytest.raises(CustomExtensionValidationError, match="main.py"):
        CustomExtensionService._infer_script_entrypoint(["one.py", "two.py"])


def test_public_skill_input_has_no_manual_runtime_selector() -> None:
    """HTTP 创建契约不得重新暴露 LLM/SCRIPT 人工选择字段。"""

    assert "runtime_type" not in CustomSkillDefinitionInput.model_fields
    assert "entrypoint" not in CustomSkillDefinitionInput.model_fields


def test_generated_entrypoint_takes_precedence_over_user_main() -> None:
    """用户 main.py 与平台入口共存时，试运行和发布必须选择已确认入口。"""

    paths = ["main.py", "_kg_skill_entry.py"]
    metadata = {
        "generated_script_entrypoint": "_kg_skill_entry.py",
        "entity_record_adapter": {
            "function_name": "normalize",
            "parameter_name": "device_name",
            "result_field": "standard_name",
        },
    }
    assert (
        CustomExtensionService._resolve_script_entrypoint(paths, metadata) == "_kg_skill_entry.py"
    )
    assert CustomExtensionService._resolve_script_entrypoint(["main.py"], {}) == "main.py"
    with pytest.raises(CustomExtensionValidationError, match="不一致"):
        CustomExtensionService._resolve_script_entrypoint(["main.py"], metadata)
