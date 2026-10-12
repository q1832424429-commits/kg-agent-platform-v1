"""验证自定义脚本真实执行时使用确认映射并阻断歧义输出。"""

import json
from uuid import UUID, uuid4

import pytest

from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.extensions.script_field_mapping import ScriptInputFieldBinding
from kg_agent.extensions.script_skill import ScriptSkill
from kg_agent.extensions.service import ScriptBundle
from kg_agent.registries.artifact_registry import ArtifactRegistry
from tests.factories import make_artifact


class _Extensions:
    """返回测试固定的不可变 Skill 修订。"""

    def __init__(self, bundle: ScriptBundle) -> None:
        self.bundle = bundle

    async def load_script_bundle(self, _name: str, _revision: int) -> ScriptBundle:
        """不访问 PostgreSQL，返回已确认的定义包。"""

        return self.bundle


class _Reader:
    """按 Artifact ID 提供已经存储的正式内容字节。"""

    async def read_content(self, _artifact_id: UUID) -> bytes:
        """返回符合 EntityDataset 内容 Schema 的样例。"""

        return json.dumps(
            {
                "schema_version": "1.0.0",
                "entities": [
                    {"entity_id": "e1", "name": "液压泵", "class_id": "device", "evidence": []}
                ],
            }
        ).encode()


class _Runner:
    """记录沙箱调用参数并返回受控候选输出。"""

    def __init__(self, duplicate: bool = False) -> None:
        self.input_payload: dict[str, object] | None = None
        self.duplicate = duplicate

    async def execute(self, **kwargs: object) -> dict[str, object]:
        """只模拟 Runner 边界响应，不在测试进程执行用户脚本。"""

        self.input_payload = kwargs["input_payload"]  # type: ignore[assignment]
        output = {
            "artifact_type": "TextReport",
            "content": {"title": "结果", "summary": "已分析", "sections": [], "evidence": []},
            "content_summary": "结果",
        }
        return {"result": {"outputs": [output, output] if self.duplicate else [output]}}


def _registration() -> SkillRegistration:
    """构造仅供测试的已发布自定义 Skill 元数据。"""

    return SkillRegistration(
        name="custom_equipment_report",
        version="custom-1",
        description="设备报告",
        owner_agent=None,
        supported_subtask_types=["equipment_report"],
        allowed_agents=["custom_equipment_agent"],
        input_artifact_types=["EntityDataset"],
        output_artifact_types=["TextReport"],
        parameters_schema={},
        side_effect=False,
        timeout_seconds=30,
        max_retries=0,
        lifecycle="ACTIVE",
    )


@pytest.mark.asyncio
async def test_runtime_uses_confirmed_field_mapping() -> None:
    """实际执行应把不同名的脚本字段映射到正式实体数据集。"""

    ref = make_artifact(uuid4(), artifact_type="EntityDataset")
    runner = _Runner()
    skill = ScriptSkill(
        _registration(),
        extensions=_Extensions(
            ScriptBundle(
                entrypoint="main.py",
                checksum="checksum",
                resources=(("main.py", b"def run(inputs, parameters): return {}"),),
                input_bindings=(
                    ScriptInputFieldBinding(
                        script_field="devices",
                        artifact_type="EntityDataset",
                        source_pointer="/entities",
                    ),
                ),
            )
        ),
        artifact_reader=_Reader(),
        artifact_registry=ArtifactRegistry.v1(),
        runner=runner,
    )
    result = await skill.execute(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="custom_equipment_agent",
            skill_name=skill.name,
            input_artifacts=[ref],
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    assert runner.input_payload is not None
    parameters = runner.input_payload["parameters"]
    assert isinstance(parameters, dict)
    assert parameters["input_fields"]["devices"][0]["name"] == "液压泵"
    assert result.outputs[0].parent_artifact_ids == [ref.artifact_id]


@pytest.mark.asyncio
async def test_runtime_rejects_duplicate_output_types() -> None:
    """脚本重复返回同一正式产物类型时不允许进入下游。"""

    ref = make_artifact(uuid4(), artifact_type="EntityDataset")
    skill = ScriptSkill(
        _registration(),
        extensions=_Extensions(ScriptBundle("main.py", "checksum", ())),
        artifact_reader=_Reader(),
        artifact_registry=ArtifactRegistry.v1(),
        runner=_Runner(duplicate=True),
    )
    result = await skill.execute(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="custom_equipment_agent",
            skill_name=skill.name,
            input_artifacts=[ref],
        )
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.outputs == []
    assert "重复" in (result.error_message or "")


class _EntityRunner:
    """验证运行期获得已确认的逐项配置，并返回新的标准实体数据集。"""

    def __init__(self) -> None:
        self.adapter: dict[str, str] | None = None

    async def execute(self, **kwargs: object) -> dict[str, object]:
        """用确定性响应模拟隔离容器，不在宿主机运行用户脚本。"""

        payload = kwargs["input_payload"]
        assert isinstance(payload, dict)
        self.adapter = payload["parameters"]["entity_record_adapter"]
        content = dict(payload["inputs"][0]["content"])
        content["entities"] = [
            {
                **content["entities"][0],
                "name": "标准液压泵",
                "aliases": ["液压泵"],
            }
        ]
        return {
            "result": {
                "outputs": [
                    {
                        "artifact_type": "EntityDataset",
                        "content": content,
                        "content_summary": "实体名称已标准化",
                    }
                ]
            }
        }


@pytest.mark.asyncio
async def test_entity_adapter_runtime_passes_config_and_new_artifact_to_downstream() -> None:
    """正式 Skill 用新标准产物替代上游实体数据，并绑定真实父 ID。"""

    from kg_agent.extensions.script_field_mapping import EntityRecordAdapter

    ref = make_artifact(uuid4(), artifact_type="EntityDataset")
    runner = _EntityRunner()
    registration = _registration().model_copy(
        update={
            "name": "normalize_devices",
            "output_artifact_types": ["EntityDataset"],
        }
    )
    skill = ScriptSkill(
        registration,
        extensions=_Extensions(
            ScriptBundle(
                "main.py",
                "checksum",
                (("main.py", b"def normalize(device_name): return {}"),),
                entity_record_adapter=EntityRecordAdapter(
                    function_name="normalize",
                    parameter_name="device_name",
                    result_field="standard_name",
                ),
            )
        ),
        artifact_reader=_Reader(),
        artifact_registry=ArtifactRegistry.v1(),
        runner=runner,
    )
    result = await skill.execute(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="custom_equipment_agent",
            skill_name=skill.name,
            input_artifacts=[ref],
        )
    )
    assert result.status is AgentRunStatus.SUCCESS
    assert runner.adapter is not None
    assert runner.adapter["parameter_name"] == "device_name"
    assert result.outputs[0].artifact_type == "EntityDataset"
    assert result.outputs[0].content["entities"][0]["name"] == "标准液压泵"
    assert result.outputs[0].parent_artifact_ids == [ref.artifact_id]
