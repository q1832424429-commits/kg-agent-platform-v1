"""验证脚本 Skill 发布前的依赖扫描与 Artifact 契约门禁。"""

import io
import json
import sys
from pathlib import Path

import pytest

from kg_agent.extensions.contract_profiles import ContractProfileRegistry
from kg_agent.extensions.script_contract_validation import ScriptContractValidator
from kg_agent.extensions.script_entry import main as script_entry_main
from kg_agent.extensions.script_field_mapping import ScriptInputFieldBinding
from kg_agent.extensions.script_runner import _has_run_function
from kg_agent.registries.artifact_registry import ArtifactRegistry


class _Runner:
    """返回测试指定响应的确定性隔离 Runner 客户端。"""

    def __init__(self, response: dict[str, object]) -> None:
        self.response = response
        self.calls = 0
        self.last_input_payload: dict[str, object] | None = None

    async def execute(self, **kwargs: object) -> dict[str, object]:
        """记录试运行次数和脚本实参，再返回预置结构。"""

        self.calls += 1
        self.last_input_payload = kwargs["input_payload"]  # type: ignore[assignment]
        return self.response


def test_runner_detects_function_entry_before_sandbox_handoff() -> None:
    """Runner 应在沙箱权限移交前从源码识别同步或异步 run 入口。"""

    assert _has_run_function("def run(inputs, parameters):\n    return {}\n", "main.py")
    assert _has_run_function("async def run(inputs, parameters):\n    return {}\n", "main.py")
    assert not _has_run_function("def helper():\n    return {}\n", "main.py")


def test_function_entry_adapts_run_without_user_stdio(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """用户只实现 run 函数时，平台入口应负责标准输入输出 JSON 编解码。"""

    entrypoint = tmp_path / "main.py"
    entrypoint.write_text(
        "def run(inputs, parameters):\n"
        "    return {'outputs': [], 'quality_signals': {'received': len(inputs)}}\n",
        encoding="utf-8",
    )
    stdin = io.StringIO(json.dumps({"inputs": [{"artifact_type": "TextReport"}], "parameters": {}}))
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "argv", ["script_entry.py", str(entrypoint)])
    monkeypatch.setattr(sys, "stdin", stdin)
    monkeypatch.setattr(sys, "stdout", stdout)

    script_entry_main()

    assert json.loads(stdout.getvalue())["quality_signals"]["received"] == 1


@pytest.mark.asyncio
async def test_unsupported_dependency_blocks_runner_execution() -> None:
    """未进入批准运行环境的第三方依赖应在静态阶段阻断发布检查。"""

    artifacts = ArtifactRegistry.v1()
    runner = _Runner({})
    validator = ScriptContractValidator(artifacts, runner)
    result = await validator.validate(
        entrypoint="main.py",
        resources=(("main.py", b"import requests\n"),),
        profile=ContractProfileRegistry.v1(artifacts).require("document_text_report"),
        timeout_seconds=30,
    )

    assert result.passed is False
    assert runner.calls == 0
    assert result.dependency_findings[0].name == "requests"
    assert result.dependency_findings[0].supported is False


@pytest.mark.asyncio
async def test_nonstandard_run_is_rejected_before_runner() -> None:
    """单参数 run 先给出可操作的适配提示，不产生沙箱参数异常。"""

    artifacts = ArtifactRegistry.v1()
    runner = _Runner({})
    result = await ScriptContractValidator(artifacts, runner).validate(
        entrypoint="main.py",
        resources=(("main.py", b"def run(payload): return payload\n"),),
        profile=ContractProfileRegistry.v1(artifacts).require("document_text_report"),
        timeout_seconds=30,
    )
    assert result.passed is False
    assert runner.calls == 0
    assert "确认已有函数" in result.diagnostics[-1]


def test_output_shape_summary_excludes_business_values() -> None:
    """试运行只持久化返回结构，不把具体业务文本放入检查报告。"""

    from kg_agent.extensions.script_contract_validation import describe_script_output_shape

    shapes = describe_script_output_shape(
        {
            "normalized_entities": [{"entity_id": "secret-123", "name": "液压泵"}],
            "warning": "保密的业务说明",
        }
    )
    assert shapes["normalized_entities"] == "列表（1 项），单项字段：entity_id, name"
    assert shapes["warning"] == "文本"
    assert "secret-123" not in str(shapes)
    assert "保密的业务说明" not in str(shapes)


@pytest.mark.asyncio
async def test_complete_raw_content_passes_same_contract_check() -> None:
    """试运行可安全封装完整单产物内容，并记录该转换方式。"""

    artifacts = ArtifactRegistry.v1()
    class _EchoContentRunner:
        """把契约样例完整返回，模拟保持实体身份的脚本。"""

        async def execute(self, **kwargs: object) -> dict[str, object]:
            """读取当前随机化样例，避免把测试固定到某个证据 UUID。"""

            payload = kwargs["input_payload"]
            assert isinstance(payload, dict)
            inputs = payload["inputs"]
            assert isinstance(inputs, list)
            return {"result": inputs[0]["content"]}

    result = await ScriptContractValidator(artifacts, _EchoContentRunner()).validate(
        entrypoint="main.py",
        resources=(("main.py", b"def run(inputs, parameters): return {'entities': []}\n"),),
        profile=ContractProfileRegistry.v1(artifacts).require("entity_transform"),
        timeout_seconds=30,
    )
    assert result.passed is True
    assert "自动封装" in result.diagnostics[-1]


@pytest.mark.asyncio
async def test_supported_dependency_and_valid_artifact_pass_contract_check() -> None:
    """批准依赖和符合 Profile 的 Pydantic 内容应得到可发布通过结果。"""

    artifacts = ArtifactRegistry.v1()
    runner = _Runner(
        {
            "result": {
                "outputs": [
                    {
                        "artifact_type": "TextReport",
                        "content": {
                            "title": "样例报告",
                            "summary": "契约试运行通过。",
                            "sections": [],
                            "evidence": [],
                        },
                        "content_summary": "样例报告",
                    }
                ],
                "quality_signals": {},
            }
        }
    )
    validator = ScriptContractValidator(artifacts, runner)
    result = await validator.validate(
        entrypoint="main.py",
        resources=(
            ("main.py", b"import pandas as pd\ndef run(inputs, parameters): return {}\n"),
            ("requirements.txt", b"pandas==2.2.3\n"),
        ),
        profile=ContractProfileRegistry.v1(artifacts).require("document_text_report"),
        timeout_seconds=30,
    )

    assert result.passed is True
    assert runner.calls == 1
    assert all(item.supported for item in result.dependency_findings)


@pytest.mark.asyncio
async def test_wrong_output_artifact_is_rejected_after_runner_returns() -> None:
    """脚本返回合法 JSON 但产物类型不匹配时不得进入后续流程。"""

    artifacts = ArtifactRegistry.v1()
    runner = _Runner(
        {
            "result": {
                "outputs": [
                    {
                        "artifact_type": "StructuredDataset",
                        "content": {"columns": ["value"], "rows": []},
                        "content_summary": "错误类型",
                    }
                ]
            }
        }
    )
    validator = ScriptContractValidator(artifacts, runner)
    result = await validator.validate(
        entrypoint="main.py",
        resources=(("main.py", b"import json\ndef run(inputs, parameters): return {}\n"),),
        profile=ContractProfileRegistry.v1(artifacts).require("document_text_report"),
        timeout_seconds=30,
    )

    assert result.passed is False
    assert "输出类型不匹配" in result.diagnostics[-1]


@pytest.mark.asyncio
async def test_contract_check_projects_confirmed_script_fields() -> None:
    """发布前试运行必须给脚本传入与正式执行相同的字段别名。"""

    artifacts = ArtifactRegistry.v1()
    runner = _Runner(
        {
            "result": {
                "outputs": [
                    {
                        "artifact_type": "TextReport",
                        "content": {
                            "title": "样例报告",
                            "summary": "映射成功",
                            "sections": [],
                            "evidence": [],
                        },
                        "content_summary": "样例报告",
                    }
                ]
            }
        }
    )
    result = await ScriptContractValidator(artifacts, runner).validate(
        entrypoint="main.py",
        resources=(("main.py", b"def run(inputs, parameters): return {}\n"),),
        profile=ContractProfileRegistry.v1(artifacts).require("document_text_report"),
        timeout_seconds=30,
        input_bindings=(
            ScriptInputFieldBinding(
                script_field="paragraphs",
                artifact_type="SemanticDocument",
                source_pointer="/segments",
            ),
        ),
    )

    assert result.passed is True
    assert runner.last_input_payload is not None
    parameters = runner.last_input_payload["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["input_fields"]["paragraphs"][0]["text"]


@pytest.mark.asyncio
async def test_contract_check_rejects_missing_mapped_field_before_runner() -> None:
    """样例中不存在已声明必填字段时，发布前就应失败且不运行脚本。"""

    artifacts = ArtifactRegistry.v1()
    runner = _Runner({})
    result = await ScriptContractValidator(artifacts, runner).validate(
        entrypoint="main.py",
        resources=(("main.py", b"def run(inputs, parameters): return {}\n"),),
        profile=ContractProfileRegistry.v1(artifacts).require("document_text_report"),
        timeout_seconds=30,
        input_bindings=(
            ScriptInputFieldBinding(
                script_field="missing",
                artifact_type="SemanticDocument",
                source_pointer="/no_such_field",
            ),
        ),
    )

    assert result.passed is False
    assert runner.calls == 0


@pytest.mark.asyncio
async def test_confirmed_raw_output_mapping_passes_contract_check() -> None:
    """异类型原始脚本结果通过显式字段映射后仍需目标 Pydantic Schema 校验。"""

    from kg_agent.extensions.script_field_mapping import ScriptOutputFieldBinding

    artifacts = ArtifactRegistry.v1()
    runner = _Runner({"result": {"headline": "维修报告", "finding": "液压泵漏油"}})
    validator = ScriptContractValidator(artifacts, runner)
    result = await validator.validate(
        entrypoint="main.py",
        resources=(("main.py", b"def run(inputs, parameters): return {}"),),
        profile=ContractProfileRegistry.v1(artifacts).require("document_text_report"),
        timeout_seconds=30,
        output_bindings=(
            ScriptOutputFieldBinding(
                artifact_type="TextReport", source_pointer="/headline", target_field="title"
            ),
            ScriptOutputFieldBinding(
                artifact_type="TextReport", source_pointer="/finding", target_field="summary"
            ),
        ),
    )

    assert result.passed is True
