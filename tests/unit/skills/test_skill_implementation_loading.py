"""验证每个 Skill 都从自身独立目录装配。"""

import json
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.skills.knowledge_extraction import KnowledgeExtractionResponse
from kg_agent.skills.llm import build_skill_output_model
from tests.factories import make_artifact


class BytesReader:
    """为独立 Skill 装配测试提供 Artifact 正文字节。"""

    def __init__(self, contents: dict[UUID, bytes]) -> None:
        """保存按 Artifact ID 读取的固定测试内容。"""

        self._contents = contents

    async def read_content(self, artifact_id: UUID) -> bytes:
        """返回指定 Artifact 的原始 JSON 字节。"""

        return self._contents[artifact_id]


@pytest.mark.asyncio
async def test_llm_skill_revalidates_artifact_content_loaded_by_id(project_root: Path) -> None:
    """下游按 Artifact ID 读取后必须再次校验注册 Schema，非法内容不能调用模型。"""

    task_id = uuid4()
    semantic = make_artifact(task_id, artifact_type="SemanticDocument")
    ontology_id = uuid4()
    published = make_artifact(task_id, artifact_type="PublishedOntology").model_copy(
        update={"ontology_id": ontology_id, "ontology_version": "1.0.0"}
    )
    contents = {
        semantic.artifact_id: json.dumps(
            {
                "schema_version": "1.0.0",
                "source_artifact_ids": [],
                "segments": [],
                "unexpected_field": "不得静默忽略",
            }
        ).encode(),
        published.artifact_id: json.dumps(
            {
                "schema_version": "1.0.0",
                "name": "设备本体",
                "classes": [],
                "relations": [],
                "properties": [],
                "source_artifact_ids": [],
                "ontology_id": str(ontology_id),
                "ontology_version": "1.0.0",
                "approval_artifact_id": str(uuid4()),
            }
        ).encode(),
    }
    model = FakeModelAdapter([])
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=model,
        artifact_reader=BytesReader(contents),
    )

    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="knowledge_extraction_agent",
            skill_name="extract_knowledge",
            input_artifacts=[semantic, published],
        )
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error_code == "LLM_SKILL_INPUT_READ_FAILED"
    assert result.outputs == []
    assert model.calls == []


def test_all_registered_skills_are_self_contained_packages(
    project_root: Path,
) -> None:
    """当前 17 个 Skill 都由 SKILL.md 定义，专用代码只保留给已实现算法。"""

    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=FakeModelAdapter([]),
        artifact_reader=BytesReader({}),
    )

    registrations = registry.list_all()
    assert len(registrations) == 17
    coded_skills = {
        "build_document_manifest",
        "parse_document",
        "extract_knowledge",
        "build_candidate_graph",
        "apply_graph_changeset",
        "publish_ontology",
        "publish_graph",
    }
    for registration in registrations:
        package = project_root / "src" / "kg_agent" / "skills" / "catalog" / registration.name
        assert (package / "SKILL.md").is_file()
        assert (package / "implementation.py").is_file() is (registration.name in coded_skills)


def test_generated_graph_changeset_skill_uses_standard_llm_contract(project_root: Path) -> None:
    """增量变更集生成器必须是图谱管理 Agent 专属的独立 LLM Skill 包。"""

    artifacts = ArtifactRegistry.v1()
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )

    registration = registry.require("generate_graph_changeset")
    assert registration.owner_agent == "graph_management_agent"
    assert registration.allowed_agents == ["graph_management_agent"]
    assert registration.supported_subtask_types == ["graph_changeset_generate"]
    assert registration.requires_dedicated_subtask is True
    assert registration.input_artifact_types == [
        "CandidateGraph",
        "PublishedOntology",
        "EntityDataset",
        "RelationDataset",
        "PropertyDataset",
        "EvidenceDataset",
    ]
    assert registration.output_artifact_types == ["GraphChangeSet"]

    output_model = build_skill_output_model(registration, artifacts)
    with pytest.raises(ValueError, match="target_checksum"):
        output_model.model_validate(
            {
                "outputs": [
                    {
                        "artifact_type": "GraphChangeSet",
                        "content_summary": "缺少目标版本绑定",
                        "content": {
                            "schema_version": "1.0.0",
                            "target_artifact_id": str(uuid4()),
                            "operations": [],
                        },
                    }
                ],
                "quality_signals": {},
            }
        )


@pytest.mark.asyncio
async def test_knowledge_extraction_uses_its_dedicated_prompt(project_root: Path) -> None:
    """统一知识抽取应携带联合抽取与跨产物引用约束。"""

    task_id = uuid4()
    ontology_id = uuid4()
    semantic = make_artifact(task_id, artifact_type="SemanticDocument")
    published = make_artifact(task_id, artifact_type="PublishedOntology").model_copy(
        update={"ontology_id": ontology_id, "ontology_version": "1.0.0"}
    )
    contents = {
        semantic.artifact_id: json.dumps(
            {
                "schema_version": "1.0.0",
                "source_artifact_ids": [],
                "segments": [],
            }
        ).encode(),
        published.artifact_id: json.dumps(
            {
                "schema_version": "1.0.0",
                "name": "设备本体",
                "classes": [],
                "relations": [],
                "properties": [],
                "source_artifact_ids": [],
                "ontology_id": str(ontology_id),
                "ontology_version": "1.0.0",
                "approval_artifact_id": str(uuid4()),
            }
        ).encode(),
    }
    model = FakeModelAdapter(
        [
            {
                "entities": [],
                "relations": [],
                "properties": [],
            }
        ]
    )
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=model,
        artifact_reader=BytesReader(contents),
    )

    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="knowledge_extraction_agent",
            skill_name="extract_knowledge",
            input_artifacts=[semantic, published],
            parameters={"_task_extraction_scope": {"allow_empty": True}},
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    assert "一次调用中完成实例知识联合抽取" in model.calls[0].system_prompt
    assert "关系的 source_entity_id" in model.calls[0].system_prompt
    assert "不要返回 outputs" in model.calls[0].system_prompt


def test_skill_output_model_binds_content_to_artifact_pydantic_model(project_root: Path) -> None:
    """动态响应模型必须在模型网关阶段拒绝 Artifact 深层字段缺失。"""

    artifacts = ArtifactRegistry.v1()
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )
    output_model = build_skill_output_model(registry.require("extract_knowledge"), artifacts)

    with pytest.raises(ValueError, match="class_id"):
        output_model.model_validate(
            {
                "outputs": [
                    {
                        "artifact_type": "EntityDataset",
                        "content_summary": "字段不完整的实体",
                        "content": {
                            "schema_version": "1.0.0",
                            "entities": [{"entity_id": "e-1", "name": "设备"}],
                        },
                    },
                    {
                        "artifact_type": "RelationDataset",
                        "content_summary": "空关系",
                        "content": {"schema_version": "1.0.0", "relations": []},
                    },
                    {
                        "artifact_type": "PropertyDataset",
                        "content_summary": "空属性",
                        "content": {"schema_version": "1.0.0", "properties": []},
                    },
                    {
                        "artifact_type": "EvidenceDataset",
                        "content_summary": "空证据",
                        "content": {"schema_version": "1.0.0", "evidence": []},
                    },
                ],
                "quality_signals": {},
            }
        )


def test_candidate_ontology_generation_requires_complete_business_arrays(
    project_root: Path,
) -> None:
    """候选本体的模型响应必须在原生 Schema 层要求完整业务数组。"""

    artifacts = ArtifactRegistry.v1()
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )
    output_model = build_skill_output_model(
        registry.require("extract_ontology_candidates"),
        artifacts,
    )

    with pytest.raises(ValueError, match="classes|relations|properties"):
        output_model.model_validate(
            {
                "outputs": [
                    {
                        "artifact_type": "CandidateOntology",
                        "content_summary": "摘要声称存在本体内容，但没有返回数组",
                        "content": {
                            "name": "设备候选本体",
                            "source_artifact_ids": [str(uuid4())],
                        },
                    }
                ],
                "quality_signals": {},
            }
        )

    validated = output_model.model_validate(
        {
            "outputs": [
                {
                    "artifact_type": "CandidateOntology",
                    "content_summary": "完整候选本体",
                    "content": {
                        "name": "设备候选本体",
                        "classes": [
                            {
                                "class_id": "Equipment",
                                "name": "设备",
                                "parent_class_ids": [],
                            }
                        ],
                        "relations": [],
                        "properties": [],
                        "source_artifact_ids": [str(uuid4())],
                    },
                }
            ],
            "quality_signals": {},
        }
    )
    schema = output_model.model_json_schema()
    assert validated.outputs[0].content.classes[0].class_id == "Equipment"
    assert "CandidateOntologyGenerationContent" in schema["$defs"]
    assert set(schema["$defs"]["CandidateOntologyGenerationContent"]["required"]) >= {
        "name",
        "classes",
        "relations",
        "properties",
        "source_artifact_ids",
    }


def test_knowledge_extraction_response_requires_all_business_arrays() -> None:
    """专用响应 Schema 必须要求三个知识数组，并禁止模型重复生成统一证据表。"""

    schema = KnowledgeExtractionResponse.model_json_schema()
    assert set(schema["required"]) == {"entities", "relations", "properties"}
    with pytest.raises(ValueError, match="entities"):
        KnowledgeExtractionResponse.model_validate({"relations": [], "properties": []})
    with pytest.raises(ValueError, match="extra_forbidden"):
        KnowledgeExtractionResponse.model_validate(
            {"entities": [], "relations": [], "properties": [], "evidence": []}
        )


@pytest.mark.asyncio
async def test_llm_skill_repairs_specific_pydantic_error_at_most_twice(
    project_root: Path,
) -> None:
    """专用 Schema 失败应反馈字段路径，并仅修复一次后返回标准 Artifact。"""

    task_id = uuid4()
    ontology_id = uuid4()
    semantic = make_artifact(task_id, artifact_type="SemanticDocument")
    published = make_artifact(task_id, artifact_type="PublishedOntology").model_copy(
        update={"ontology_id": ontology_id, "ontology_version": "1.0.0"}
    )
    contents = {
        semantic.artifact_id: json.dumps(
            {"schema_version": "1.0.0", "source_artifact_ids": [], "segments": []}
        ).encode(),
        published.artifact_id: json.dumps(
            {
                "schema_version": "1.0.0",
                "name": "设备本体",
                "classes": [],
                "relations": [],
                "properties": [],
                "source_artifact_ids": [],
                "ontology_id": str(ontology_id),
                "ontology_version": "1.0.0",
                "approval_artifact_id": str(uuid4()),
            }
        ).encode(),
    }
    pointer = {
        "source_artifact_id": str(semantic.artifact_id),
        "locator": "segment:1",
        "quote": "设备",
    }
    invalid = {
        "entities": [{"entity_id": "e-1", "name": "设备", "evidence": [pointer]}],
        "relations": [],
        "properties": [],
    }
    valid = {
        "entities": [],
        "relations": [],
        "properties": [],
    }
    model = FakeModelAdapter([invalid, valid])
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=model,
        artifact_reader=BytesReader(contents),
    )

    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="knowledge_extraction_agent",
            skill_name="extract_knowledge",
            input_artifacts=[semantic, published],
            parameters={"_task_extraction_scope": {"allow_empty": True}},
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    assert len(model.calls) == 2
    assert "class_id" in model.calls[1].user_prompt

    # 第二次仍不合法时必须终止，第三个预设响应不能被消费，更不能生成部分 Artifact。
    failing_model = FakeModelAdapter([invalid, invalid, valid])
    failing_registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=failing_model,
        artifact_reader=BytesReader(contents),
    )
    failed = await failing_registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="knowledge_extraction_agent",
            skill_name="extract_knowledge",
            input_artifacts=[semantic, published],
            parameters={"_task_extraction_scope": {"allow_empty": True}},
        )
    )

    assert failed.status is AgentRunStatus.FAILED
    assert failed.retryable is False
    assert failed.outputs == []
    assert failed.error_code == "LLM_SKILL_OUTPUT_INVALID"
    assert len(failing_model.calls) == 2


@pytest.mark.asyncio
async def test_knowledge_extraction_repairs_empty_content_and_recomputes_counts(
    project_root: Path,
) -> None:
    """摘要声称有实体但真实数组为空时必须修复，统计只能来自实际内容。"""

    task_id = uuid4()
    source_id = uuid4()
    ontology_id = uuid4()
    semantic = make_artifact(task_id, artifact_type="SemanticDocument")
    published = make_artifact(task_id, artifact_type="PublishedOntology").model_copy(
        update={"ontology_id": ontology_id, "ontology_version": "1.0.0"}
    )
    contents = {
        semantic.artifact_id: json.dumps(
            {
                "schema_version": "1.0.0",
                "source_artifact_ids": [str(source_id)],
                "segments": [
                    {
                        "segment_id": "segment-1",
                        "text": "一号冲压机由华东智造生产。",
                        "evidence": [
                            {
                                "source_artifact_id": str(source_id),
                                "locator": "word/paragraph:1",
                                "quote": "一号冲压机由华东智造生产。",
                            }
                        ],
                    }
                ],
            }
        ).encode(),
        published.artifact_id: json.dumps(
            {
                "schema_version": "1.0.0",
                "name": "设备本体",
                "classes": [{"class_id": "equipment", "name": "设备", "description": ""}],
                "relations": [],
                "properties": [],
                "source_artifact_ids": [],
                "ontology_id": str(ontology_id),
                "ontology_version": "1.0.0",
                "approval_artifact_id": str(uuid4()),
            }
        ).encode(),
    }
    empty_but_claimed = {
        "entities": [],
        "relations": [],
        "properties": [],
    }
    pointer = {
        "source_artifact_id": str(source_id),
        "locator": "word/paragraph:1",
        "quote": "一号冲压机由华东智造生产。",
    }
    repaired = {
        "entities": [
            {
                "entity_id": "equipment-1",
                "name": "一号冲压机",
                "class_id": "equipment",
                "evidence": [pointer],
            }
        ],
        "relations": [],
        "properties": [],
    }
    model = FakeModelAdapter([empty_but_claimed, repaired])
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=model,
        artifact_reader=BytesReader(contents),
    )

    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="knowledge_extraction_agent",
            skill_name="extract_knowledge",
            input_artifacts=[semantic, published],
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    assert len(model.calls) == 2
    assert "entities 为空" in model.calls[1].user_prompt
    assert result.quality_signals["entity_count"] == 1.0
    assert result.quality_signals["evidence_count"] == 1.0
    assert result.outputs[0].content_summary == "统一知识抽取产生 1 个实体。"
    evidence_output = next(
        item for item in result.outputs if item.artifact_type == "EvidenceDataset"
    )
    assert evidence_output.content["evidence"][0]["pointer"] == pointer
    assert evidence_output.content["evidence"][0]["evidence_id"].startswith("evidence-")


@pytest.mark.asyncio
async def test_ontology_extraction_repairs_omitted_business_arrays(
    project_root: Path,
) -> None:
    """摘要声称已抽取但本体数组缺失时，必须修复后才能产生 Artifact。"""

    task_id = uuid4()
    source_id = uuid4()
    semantic = make_artifact(task_id, artifact_type="SemanticDocument")
    contents = {
        semantic.artifact_id: json.dumps(
            {
                "schema_version": "1.0.0",
                "source_artifact_ids": [str(source_id)],
                "segments": [
                    {
                        "segment_id": "segment-1",
                        "text": "E7 由临港工厂生产，并使用动力电池包。",
                        "evidence": [],
                    }
                ],
            }
        ).encode(),
    }
    omitted = {
        "outputs": [
            {
                "artifact_type": "CandidateOntology",
                "content_summary": "已抽取车型、工厂和零件类及其关系。",
                "content": {
                    "schema_version": "1.0.0",
                    "name": "汽车制造候选本体",
                },
            }
        ],
        "quality_signals": {},
    }
    repaired = {
        "outputs": [
            {
                "artifact_type": "CandidateOntology",
                "content_summary": "已抽取 3 个类、1 个关系和 1 个属性。",
                "content": {
                    "schema_version": "1.0.0",
                    "name": "汽车制造候选本体",
                    "classes": [
                        {"class_id": "vehicle", "name": "车型"},
                        {"class_id": "factory", "name": "工厂"},
                        {"class_id": "part", "name": "零件"},
                    ],
                    "relations": [
                        {
                            "relation_id": "produced_at",
                            "name": "生产于",
                            "domain_class_id": "vehicle",
                            "range_class_id": "factory",
                        }
                    ],
                    "properties": [
                        {
                            "property_id": "vehicle_code",
                            "name": "车型编码",
                            "owner_class_id": "vehicle",
                            "value_type": "string",
                        }
                    ],
                    "source_artifact_ids": [str(semantic.artifact_id)],
                },
            }
        ],
        "quality_signals": {},
    }
    model = FakeModelAdapter([omitted, repaired])
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=model,
        artifact_reader=BytesReader(contents),
    )

    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="knowledge_extraction_agent",
            skill_name="extract_ontology_candidates",
            input_artifacts=[semantic],
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    assert len(model.calls) == 2
    assert "outputs.0.content.classes" in model.calls[1].user_prompt
    assert len(result.outputs[0].content["classes"]) == 3


def test_entity_generation_schema_requires_explicit_entities(project_root: Path) -> None:
    """新生成 EntityDataset 不得沿用历史持久化模型的空列表默认值。"""

    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
    )
    registration = registry.require("align_entities")
    output_model = build_skill_output_model(registration, ArtifactRegistry.v1())
    schema = output_model.model_json_schema()
    assert "entities" in schema["$defs"]["EntityDatasetGenerationContent"]["required"]


@pytest.mark.asyncio
async def test_entity_alignment_repairs_missing_entities_and_rejects_empty_result(
    project_root: Path,
) -> None:
    """复现模型只报 2 个实体却漏掉 entities 的情况，修复失败不能产生空产物。"""

    task_id = uuid4()
    source = make_artifact(task_id, artifact_type="EntityDataset")
    entities = [
        {"entity_id": "e1", "name": "设备", "class_id": "Equipment", "evidence": []},
        {"entity_id": "e2", "name": "液压泵", "class_id": "Equipment", "evidence": []},
    ]
    reader = BytesReader(
        {source.artifact_id: json.dumps({"schema_version": "1.0.0", "entities": entities}).encode()}
    )
    missing = {
        "outputs": [
            {
                "artifact_type": "EntityDataset",
                "content": {"schema_version": "1.0.0"},
                "content_summary": "对齐后共 2 个实体",
            }
        ],
        "quality_signals": {"entity_count": 2, "original_entity_count": 2, "merged_count": 0},
    }
    valid = {
        "outputs": [
            {
                "artifact_type": "EntityDataset",
                "content": {"schema_version": "1.0.0", "entities": entities},
                "content_summary": "模型报告数量不应作为事实",
            }
        ],
        "quality_signals": {"entity_count": 2, "original_entity_count": 2, "merged_count": 0},
    }
    model = FakeModelAdapter([missing, valid])
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=model,
        artifact_reader=reader,
    )
    invocation = SkillInvocation(
        invocation_id=uuid4(),
        agent_type="knowledge_fusion_agent",
        skill_name="align_entities",
        input_artifacts=[source],
    )
    repaired = await registry.invoke(invocation)
    assert repaired.status is AgentRunStatus.SUCCESS
    assert len(model.calls) == 2
    assert "entities" in model.calls[1].user_prompt
    assert len(repaired.outputs[0].content["entities"]) == 2
    assert repaired.outputs[0].content_summary == "实体对齐后共 2 个实体，合并 0 个。"

    failing_model = FakeModelAdapter([missing, missing])
    failing_registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=failing_model,
        artifact_reader=reader,
    )
    failed = await failing_registry.invoke(invocation)
    assert failed.status is AgentRunStatus.FAILED
    assert failed.error_code == "LLM_SKILL_OUTPUT_INVALID"
    assert failed.outputs == []


@pytest.mark.asyncio
async def test_entity_alignment_rejects_silent_entity_loss(project_root: Path) -> None:
    """即使有 entities 字段，模型漏掉输入实体且宣称未合并也不得成功。"""

    task_id = uuid4()
    source = make_artifact(task_id, artifact_type="EntityDataset")
    entities = [
        {"entity_id": "e1", "name": "设备", "class_id": "Equipment", "evidence": []},
        {"entity_id": "e2", "name": "液压泵", "class_id": "Equipment", "evidence": []},
    ]
    reader = BytesReader(
        {source.artifact_id: json.dumps({"schema_version": "1.0.0", "entities": entities}).encode()}
    )
    partial = {
        "outputs": [
            {
                "artifact_type": "EntityDataset",
                "content": {"schema_version": "1.0.0", "entities": [entities[0]]},
                "content_summary": "对齐成功",
            }
        ],
        "quality_signals": {"entity_count": 2, "original_entity_count": 2, "merged_count": 0},
    }
    model = FakeModelAdapter([partial, partial])
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=model,
        artifact_reader=reader,
    )
    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="knowledge_fusion_agent",
            skill_name="align_entities",
            input_artifacts=[source],
        )
    )
    assert result.status is AgentRunStatus.FAILED
    assert result.outputs == []
    assert len(model.calls) == 2


@pytest.mark.asyncio
async def test_entity_alignment_rejects_explicit_empty_entities(project_root: Path) -> None:
    """模型显式给空数组却宣称已有实体时，重试后仍不得发布空 Artifact。"""

    task_id = uuid4()
    source = make_artifact(task_id, artifact_type="EntityDataset")
    reader = BytesReader(
        {
            source.artifact_id: json.dumps(
                {
                    "schema_version": "1.0.0",
                    "entities": [
                        {"entity_id": "e1", "name": "设备", "class_id": "Equipment", "evidence": []}
                    ],
                }
            ).encode()
        }
    )
    empty = {
        "outputs": [
            {
                "artifact_type": "EntityDataset",
                "content": {"schema_version": "1.0.0", "entities": []},
                "content_summary": "对齐后有 1 个实体",
            }
        ],
        "quality_signals": {"entity_count": 1, "original_entity_count": 1, "merged_count": 0},
    }
    model = FakeModelAdapter([empty, empty])
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=model,
        artifact_reader=reader,
    )
    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="knowledge_fusion_agent",
            skill_name="align_entities",
            input_artifacts=[source],
        )
    )
    assert result.status is AgentRunStatus.FAILED
    assert result.error_code == "LLM_SKILL_OUTPUT_INVALID"
    assert result.outputs == []
    assert len(model.calls) == 2
