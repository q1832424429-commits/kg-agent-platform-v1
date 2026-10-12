"""验证自定义脚本只交付标准 Artifact 内容或已确认字段映射结果。"""

import json
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.exceptions import ArtifactValidationError
from kg_agent.extensions.script_contract_validation import (
    normalize_script_result,
    validate_entity_transform_result,
)
from kg_agent.extensions.script_field_mapping import ScriptOutputFieldBinding
from kg_agent.extensions.script_skill import ScriptSkill
from kg_agent.extensions.service import ScriptBundle
from kg_agent.registries.artifact_registry import ArtifactRegistry
from tests.factories import make_artifact


class _Extensions:
    """提供不可变测试脚本包。"""

    async def load_script_bundle(self, _name: str, _revision: int) -> ScriptBundle:
        """返回无需文件系统的修订。"""

        return ScriptBundle("main.py", "checksum", ())


class _Reader:
    """提供符合 EntityDataset 契约的输入内容。"""

    async def read_content(self, _artifact_id: UUID) -> bytes:
        """返回一个含实体的正式数据集。"""

        return json.dumps(
            {
                "schema_version": "1.0.0",
                "entities": [
                    {"entity_id": "e1", "name": "液压泵", "class_id": "device", "evidence": []}
                ],
            }
        ).encode()


class _Runner:
    """模拟隔离 Runner 的标准脚本响应。"""

    async def execute(self, **_kwargs: object) -> dict[str, object]:
        """交付能直接进入下游的标准 EntityDataset。"""

        return {
            "result": {
                "outputs": [
                    {
                        "artifact_type": "EntityDataset",
                        "content": json.loads(await _Reader().read_content(uuid4())),
                        "content_summary": "实体转换结果",
                    }
                ]
            }
        }


class _RawContentRunner:
    """模拟脚本直接返回完整实体内容，不自行包装标准 outputs。"""

    async def execute(self, **_kwargs: object) -> dict[str, object]:
        """交付保持上游实体身份的内容。"""

        return {
            "result": {
                "entities": [
                    {
                        "entity_id": "e1",
                        "name": "液压泵",
                        "class_id": "device",
                        "evidence": [],
                    }
                ]
            }
        }


def _registration() -> SkillRegistration:
    """创建同类型变换的自定义 Skill 注册事实。"""

    return SkillRegistration(
        name="fault_analysis",
        version="custom-2",
        description="实体转换",
        owner_agent=None,
        supported_subtask_types=["entity_transform"],
        allowed_agents=["equipment_agent"],
        input_artifact_types=["EntityDataset"],
        output_artifact_types=["EntityDataset"],
        parameters_schema={},
        side_effect=False,
        timeout_seconds=30,
        max_retries=0,
        lifecycle="ACTIVE",
    )


def test_unmapped_raw_result_is_rejected() -> None:
    """普通 JSON 不能冒充标准 Artifact 产物。"""

    with pytest.raises(ValueError, match="标准 outputs"):
        normalize_script_result({"risk": "高"}, ("EntityDataset",), [])


def test_complete_single_artifact_content_is_wrapped() -> None:
    """完整单一内容可补标准外壳，但仍按注册 Schema 严格校验。"""

    registry = ArtifactRegistry.v1()
    entities = [
        {
            "entity_id": "e1",
            "name": "液压泵",
            "class_id": "device",
            "evidence": [],
        }
    ]
    parsed = normalize_script_result(
        {"entities": entities},
        ("EntityDataset",),
        [{"artifact_type": "EntityDataset", "content": {"entities": entities}}],
        artifact_registry=registry,
    )
    assert parsed.outputs[0].artifact_type == "EntityDataset"
    assert parsed.outputs[0].content["entities"][0]["entity_id"] == "e1"
    assert parsed.outputs[0].content["schema_version"] == "1.0.0"


def test_partial_or_identity_losing_content_is_not_wrapped() -> None:
    """部分结果和丢失上游实体身份的结果不能冒充完整实体产物。"""

    registry = ArtifactRegistry.v1()
    inputs = [
        {
            "artifact_type": "EntityDataset",
            "content": {
                "entities": [
                    {
                        "entity_id": "e1",
                        "name": "液压泵",
                        "class_id": "device",
                        "evidence": [],
                    }
                ]
            },
        }
    ]
    with pytest.raises(ValueError, match="字段与 EntityDataset 标准内容不匹配"):
        normalize_script_result(
            {"standard_name": "液压泵"},
            ("EntityDataset",),
            inputs,
            artifact_registry=registry,
        )
    with pytest.raises(ValueError, match="未保留上游实体 ID"):
        normalize_script_result(
            {"entities": []}, ("EntityDataset",), inputs, artifact_registry=registry
        )
    with pytest.raises(ArtifactValidationError):
        normalize_script_result(
            {"entities": [{"name": "液压泵"}]},
            ("EntityDataset",),
            inputs,
            artifact_registry=registry,
        )


def test_confirmed_output_mapping_cannot_erase_parent_entities() -> None:
    """即使用户确认字段映射，实体同型转换也不能把上游实体清空。"""

    inputs = [
        {
            "artifact_type": "EntityDataset",
            "content": {
                "schema_version": "1.0.0",
                "entities": [
                    {
                        "entity_id": "e1",
                        "name": "液压泵",
                        "class_id": "device",
                        "evidence": [],
                    }
                ],
            },
        }
    ]
    parsed = normalize_script_result(
        {"normalized_entities": []},
        ("EntityDataset",),
        inputs,
        (
            ScriptOutputFieldBinding(
                artifact_type="EntityDataset",
                source_pointer="/normalized_entities",
                target_field="entities",
            ),
        ),
    )
    with pytest.raises(ValueError, match="输入 1 个实体，脚本输出 0 个"):
        validate_entity_transform_result(inputs, parsed)


def test_sidecar_field_is_rejected_from_standard_output() -> None:
    """标准输出不得悄悄丢弃额外的独立附加数据字段。"""

    with pytest.raises(ValidationError):
        normalize_script_result(
            {
                "outputs": [
                    {
                        "artifact_type": "EntityDataset",
                        "content": {"schema_version": "1.0.0", "entities": []},
                        "content_summary": "实体数据",
                        "extension_data": {"risk": "高"},
                    }
                ]
            },
            ("EntityDataset",),
            [],
        )


def test_confirmed_output_mapping_builds_standard_report() -> None:
    """已确认的原始字段映射仍可生成已注册的标准报告。"""

    parsed = normalize_script_result(
        {"headline": "故障报告", "finding": "液压泵漏油", "severity": "高"},
        ("TextReport",),
        [],
        (
            ScriptOutputFieldBinding(
                artifact_type="TextReport", source_pointer="/headline", target_field="title"
            ),
            ScriptOutputFieldBinding(
                artifact_type="TextReport", source_pointer="/finding", target_field="summary"
            ),
        ),
    )
    content = ArtifactRegistry.v1().serialize_and_validate(
        artifact_type="TextReport", schema_version="1.0.0", content=parsed.outputs[0].content
    )
    assert json.loads(content)["title"] == "故障报告"
    assert json.loads(content)["summary"] == "液压泵漏油"


@pytest.mark.asyncio
async def test_script_skill_returns_only_standard_output() -> None:
    """隔离执行后的正式产物保留输入血缘且可被下游直接读取。"""

    ref = make_artifact(uuid4(), artifact_type="EntityDataset")
    skill = ScriptSkill(
        _registration(),
        extensions=_Extensions(),
        artifact_reader=_Reader(),
        artifact_registry=ArtifactRegistry.v1(),
        runner=_Runner(),
    )
    result = await skill.execute(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="equipment_agent",
            skill_name=skill.name,
            input_artifacts=[ref],
        )
    )
    assert result.status is AgentRunStatus.SUCCESS
    assert result.outputs[0].parent_artifact_ids == [ref.artifact_id]
    assert result.outputs[0].content["entities"][0]["name"] == "液压泵"


@pytest.mark.asyncio
async def test_script_skill_wraps_complete_raw_content() -> None:
    """正式执行与试运行共用完整单产物封装，不丢失父 Artifact 血缘。"""

    ref = make_artifact(uuid4(), artifact_type="EntityDataset")
    skill = ScriptSkill(
        _registration(),
        extensions=_Extensions(),
        artifact_reader=_Reader(),
        artifact_registry=ArtifactRegistry.v1(),
        runner=_RawContentRunner(),
    )
    result = await skill.execute(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="equipment_agent",
            skill_name=skill.name,
            input_artifacts=[ref],
        )
    )
    assert result.status is AgentRunStatus.SUCCESS
    assert result.outputs[0].parent_artifact_ids == [ref.artifact_id]
    assert result.outputs[0].content["entities"][0]["entity_id"] == "e1"


def test_output_recommendation_maps_script_records_to_standard_entity_fields() -> None:
    """依据真实脚本形状推荐下游实体字段，空冲突列表不影响主结果。"""

    from kg_agent.extensions.script_field_mapping import (
        list_nested_source_fields,
        list_script_output_targets,
        recommend_script_output_mapping,
    )

    registry = ArtifactRegistry.v1()
    proposal = recommend_script_output_mapping(
        "EntityDataset",
        ["normalized_entities", "conflicts"],
        {
            "normalized_entities": (
                "列表（2 项），单项字段：canonical_id, canonical_name, type, aliases, merged_ids"
            ),
            "conflicts": "列表（0 项）",
        },
        list_script_output_targets(registry, "EntityDataset"),
        list_nested_source_fields(registry, ["EntityDataset"]),
    )
    binding = proposal.suggested_bindings[0]
    assert binding.source_pointer == "/normalized_entities"
    assert binding.target_field == "entities"
    assert binding.record_field_bindings == {
        "aliases": "aliases",
        "class_id": "type",
        "entity_id": "canonical_id",
        "name": "canonical_name",
    }


def test_confirmed_record_output_mapping_preserves_entity_evidence() -> None:
    """一对一字段重命名继承同 ID 的原始证据，并形成标准 EntityDataset。"""

    binding = ScriptOutputFieldBinding(
        artifact_type="EntityDataset",
        source_pointer="/normalized_entities",
        target_field="entities",
        record_field_bindings={
            "entity_id": "canonical_id",
            "name": "canonical_name",
            "class_id": "type",
            "aliases": "aliases",
        },
    )
    original = {
        "schema_version": "1.0.0",
        "entities": [
            {
                "entity_id": "e1",
                "name": "旧名称",
                "class_id": "device",
                "aliases": [],
                "evidence": [],
            }
        ],
    }
    parsed = normalize_script_result(
        {
            "normalized_entities": [
                {
                    "canonical_id": "e1",
                    "canonical_name": "新名称",
                    "type": "device",
                    "aliases": ["旧名称"],
                    "merged_ids": ["e1"],
                }
            ]
        },
        ("EntityDataset",),
        [{"artifact_type": "EntityDataset", "content": original}],
        (binding,),
        ArtifactRegistry.v1(),
    )
    validate_entity_transform_result(
        [{"artifact_type": "EntityDataset", "content": original}], parsed
    )
    assert parsed.outputs[0].content["entities"][0]["name"] == "新名称"
    assert parsed.outputs[0].content["entities"][0]["evidence"] == []


def test_record_output_mapping_rejects_unresolved_entity_merge() -> None:
    """多实体合并未重定向关系与证据前不得发布为标准实体数据集。"""

    binding = ScriptOutputFieldBinding(
        artifact_type="EntityDataset",
        source_pointer="/normalized_entities",
        target_field="entities",
        record_field_bindings={"entity_id": "canonical_id"},
    )
    inputs = [
        {
            "artifact_type": "EntityDataset",
            "content": {
                "entities": [
                    {"entity_id": "e1", "name": "A", "class_id": "device", "evidence": []},
                    {"entity_id": "e2", "name": "B", "class_id": "device", "evidence": []},
                ]
            },
        }
    ]
    with pytest.raises(ValueError, match="关系及证据重定向"):
        normalize_script_result(
            {"normalized_entities": [{"canonical_id": "e1", "merged_ids": ["e1", "e2"]}]},
            ("EntityDataset",),
            inputs,
            (binding,),
            ArtifactRegistry.v1(),
        )


def test_standard_same_type_output_keeps_unmentioned_graph_fields() -> None:
    """自定义图谱 Skill 只改属性时，实体、关系和证据字段仍从同型上游继承。"""

    parent = {
        "schema_version": "1.0.0",
        "entities": [{"entity_id": "e1", "name": "泵", "class_id": "device"}],
        "relations": [
            {
                "relation_id": "r1",
                "source_entity_id": "e1",
                "relation_type_id": "part_of",
                "target_entity_id": "e1",
            }
        ],
        "properties": [
            {"property_id": "p1", "entity_id": "e1", "property_type_id": "power", "value": 5}
        ],
        "evidence": [],
    }
    parsed = normalize_script_result(
        {
            "outputs": [
                {
                    "artifact_type": "CandidateGraph",
                    "content": {"properties": [{**parent["properties"][0], "value": 8}]},
                    "content_summary": "更新功率",
                }
            ]
        },
        ("CandidateGraph",),
        [{"artifact_type": "CandidateGraph", "content": parent}],
        artifact_registry=ArtifactRegistry.v1(),
    )
    content = parsed.outputs[0].content

    assert content["entities"] == parent["entities"]
    assert content["relations"] == parent["relations"]
    assert content["evidence"] == []
    assert content["properties"][0]["value"] == 8
    ArtifactRegistry.v1().serialize_and_validate(
        artifact_type="CandidateGraph", schema_version="1.0.0", content=content
    )


def test_relation_record_mapping_keeps_unmodified_fields() -> None:
    """关系数据集逐项映射只覆盖已确认字段，端点与证据不丢失。"""

    original = {
        "relation_id": "r1",
        "source_entity_id": "e1",
        "relation_type_id": "old_type",
        "target_entity_id": "e2",
        "evidence": [],
    }
    parsed = normalize_script_result(
        {"normalized_relations": [{"id": "r1", "type": "new_type"}]},
        ("RelationDataset",),
        [{"artifact_type": "RelationDataset", "content": {"relations": [original]}}],
        (
            ScriptOutputFieldBinding(
                artifact_type="RelationDataset",
                source_pointer="/normalized_relations",
                target_field="relations",
                record_field_bindings={"relation_id": "id", "relation_type_id": "type"},
            ),
        ),
        ArtifactRegistry.v1(),
    )

    assert parsed.outputs[0].content["relations"] == [{**original, "relation_type_id": "new_type"}]
    ArtifactRegistry.v1().serialize_and_validate(
        artifact_type="RelationDataset",
        schema_version="1.0.0",
        content=parsed.outputs[0].content,
    )


def test_generic_record_mapping_rejects_dropped_evidence() -> None:
    """证据产物也必须按稳定 ID 传递，脚本漏项不能悄悄删除上游数据。"""

    binding = ScriptOutputFieldBinding(
        artifact_type="EvidenceDataset",
        source_pointer="/normalized_evidence",
        target_field="evidence",
        record_field_bindings={"evidence_id": "id", "confidence": "score"},
    )
    parent = {
        "evidence_id": "v1",
        "pointer": {"source_artifact_id": str(uuid4()), "locator": "page:1"},
        "confidence": 0.6,
    }
    with pytest.raises(ValueError, match="遗漏 evidence"):
        normalize_script_result(
            {"normalized_evidence": []},
            ("EvidenceDataset",),
            [{"artifact_type": "EvidenceDataset", "content": {"evidence": [parent]}}],
            (binding,),
            ArtifactRegistry.v1(),
        )
