"""验证总控 Agent 输出的确定性子任务校验规则。"""

from uuid import uuid4

from kg_agent.domain.enums import TaskIntent
from kg_agent.domain.task_models import OntologyStrategy
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.validators.subtask_validator import (
    SubTaskValidationContext,
    SubTaskValidator,
)
from tests.factories import make_artifact, make_decomposition, make_subtask


def test_validator_accepts_registered_assignment(agent_registry) -> None:
    """合法子任务可以分配给声明支持该类型的专业 Agent。"""

    task_id = uuid4()
    result = make_decomposition(task_id, subtasks=[make_subtask(task_id)])
    validator = SubTaskValidator(agent_registry)

    issues = validator.validate(result, SubTaskValidationContext(task_id=task_id))

    assert issues == []


def test_full_plan_rejects_graph_build_without_conflict_report(
    project_root, agent_registry
) -> None:
    """完整计划缺少冲突分析前置任务时，不能发布无法执行的 graph_build。"""

    task_id = uuid4()
    entity = make_artifact(task_id, artifact_type="EntityDataset")
    relation = make_artifact(task_id, artifact_type="RelationDataset")
    graph = make_subtask(
        task_id,
        subtask_type="graph_build",
        agent_type="graph_management_agent",
        inputs=[entity.artifact_id, relation.artifact_id],
        outputs=["CandidateGraph"],
    )
    result = make_decomposition(task_id, subtasks=[graph])
    artifacts = ArtifactRegistry.v1()
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )

    issues = SubTaskValidator(agent_registry, skills).validate(
        result,
        SubTaskValidationContext(
            task_id=task_id,
            user_message="构建候选图谱",
            available_artifacts=[entity, relation],
        ),
    )

    assert any(
        issue.code == "UNEXECUTABLE_SUBTASK_PLAN" and issue.subtask_id == graph.subtask_id
        for issue in issues
    )


def test_graph_changeset_generation_must_be_an_independent_subtask(
    project_root, agent_registry
) -> None:
    """变更建议与候选图谱应用不能被总控错误合并为一个子任务。"""

    task_id = uuid4()
    baseline = make_artifact(task_id, artifact_type="CandidateGraph")
    ontology = make_artifact(task_id, artifact_type="PublishedOntology")
    entities = make_artifact(task_id, artifact_type="EntityDataset")
    relations = make_artifact(task_id, artifact_type="RelationDataset")
    properties = make_artifact(task_id, artifact_type="PropertyDataset")
    evidence = make_artifact(task_id, artifact_type="EvidenceDataset")
    combined = make_subtask(
        task_id,
        subtask_type="graph_changeset_generate",
        agent_type="graph_management_agent",
        inputs=[
            baseline.artifact_id,
            ontology.artifact_id,
            entities.artifact_id,
            relations.artifact_id,
            properties.artifact_id,
            evidence.artifact_id,
        ],
        outputs=["GraphChangeSet", "CandidateGraph"],
    )
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
    )

    issues = SubTaskValidator(agent_registry, skills).validate(
        make_decomposition(task_id, subtasks=[combined]),
        SubTaskValidationContext(
            task_id=task_id,
            user_message="基于已有候选图谱和新文档生成增量预览，暂不发布",
            available_artifacts=[baseline, ontology, entities, relations, properties, evidence],
        ),
    )

    assert any(
        issue.code == "DEDICATED_SKILL_OUTPUT_MISMATCH" and issue.subtask_id == combined.subtask_id
        for issue in issues
    )


def test_graph_changeset_generation_accepts_changeset_only_subtask(
    project_root, agent_registry
) -> None:
    """独立生成子任务只声明 GraphChangeSet 时不应触发专属边界错误。"""

    task_id = uuid4()
    baseline = make_artifact(task_id, artifact_type="CandidateGraph")
    ontology = make_artifact(task_id, artifact_type="PublishedOntology")
    entities = make_artifact(task_id, artifact_type="EntityDataset")
    relations = make_artifact(task_id, artifact_type="RelationDataset")
    properties = make_artifact(task_id, artifact_type="PropertyDataset")
    evidence = make_artifact(task_id, artifact_type="EvidenceDataset")
    generate = make_subtask(
        task_id,
        subtask_type="graph_changeset_generate",
        agent_type="graph_management_agent",
        inputs=[
            baseline.artifact_id,
            ontology.artifact_id,
            entities.artifact_id,
            relations.artifact_id,
            properties.artifact_id,
            evidence.artifact_id,
        ],
        outputs=["GraphChangeSet"],
    )
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
    )

    issues = SubTaskValidator(agent_registry, skills).validate(
        make_decomposition(task_id, subtasks=[generate]),
        SubTaskValidationContext(
            task_id=task_id,
            user_message="基于已有候选图谱和新文档生成增量预览，暂不发布",
            available_artifacts=[baseline, ontology, entities, relations, properties, evidence],
        ),
    )

    assert "DEDICATED_SKILL_OUTPUT_MISMATCH" not in {issue.code for issue in issues}


def test_conflict_plan_reports_missing_evidence_input(project_root, agent_registry) -> None:
    """冲突分析缺少证据时，错误必须明确指出 EvidenceDataset 输入缺口。"""

    task_id = uuid4()
    entity = make_artifact(task_id, artifact_type="EntityDataset")
    relation = make_artifact(task_id, artifact_type="RelationDataset")
    conflict = make_subtask(
        task_id,
        subtask_type="conflict_analyze",
        agent_type="knowledge_fusion_agent",
        inputs=[entity.artifact_id, relation.artifact_id],
        outputs=["ConflictReport"],
    )
    result = make_decomposition(task_id, subtasks=[conflict])
    artifacts = ArtifactRegistry.v1()
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )

    issues = SubTaskValidator(agent_registry, skills).validate(
        result,
        SubTaskValidationContext(
            task_id=task_id,
            user_message="分析实体和关系冲突",
            available_artifacts=[entity, relation],
        ),
    )

    conflict_issue = next(
        issue
        for issue in issues
        if issue.code == "UNEXECUTABLE_SUBTASK_PLAN" and issue.subtask_id == conflict.subtask_id
    )
    assert "analyze_conflicts" in conflict_issue.message
    assert "EvidenceDataset" in conflict_issue.message


def test_validator_rejects_unknown_agent(agent_registry) -> None:
    """未知 Agent 类型不能进入 SharedContext。"""

    task_id = uuid4()
    subtask = make_subtask(task_id, agent_type="unknown_agent")
    result = make_decomposition(task_id, subtasks=[subtask])

    issues = SubTaskValidator(agent_registry).validate(
        result, SubTaskValidationContext(task_id=task_id)
    )

    assert "UNKNOWN_AGENT" in {item.code for item in issues}


def test_validator_rejects_cyclic_dependencies(agent_registry) -> None:
    """总控 Agent 生成的子任务依赖不能成环。"""

    task_id = uuid4()
    first_id = uuid4()
    second_id = uuid4()
    first = make_subtask(
        task_id,
        subtask_id=first_id,
        dependencies=[second_id],
    )
    second = make_subtask(
        task_id,
        subtask_id=second_id,
        dependencies=[first_id],
    )
    result = make_decomposition(task_id, subtasks=[first, second])

    issues = SubTaskValidator(agent_registry).validate(
        result, SubTaskValidationContext(task_id=task_id)
    )

    assert "CYCLIC_DEPENDENCY" in {item.code for item in issues}


def test_validator_requires_ontology_review_gate_before_publish(agent_registry) -> None:
    """本体发布前必须存在直接绑定候选本体的人工审核门禁。"""

    task_id = uuid4()
    publish = make_subtask(
        task_id,
        subtask_type="ontology_publish",
        agent_type="graph_management_agent",
        outputs=["PublishedOntology"],
    )
    result = make_decomposition(task_id, subtasks=[publish])

    issues = SubTaskValidator(agent_registry).validate(
        result, SubTaskValidationContext(task_id=task_id)
    )

    assert "MISSING_ONTOLOGY_REVIEW_GATE" in {item.code for item in issues}


def test_candidate_ontology_is_valid_direct_review_gate(agent_registry) -> None:
    """候选本体生产任务可直接作为发布任务的用户审核祖先。"""

    task_id = uuid4()
    candidate = make_subtask(
        task_id,
        subtask_type="ontology_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["CandidateOntology"],
        human_review_required=True,
    )
    publish = make_subtask(
        task_id,
        subtask_type="ontology_publish",
        agent_type="graph_management_agent",
        outputs=["PublishedOntology"],
        dependencies=[candidate.subtask_id],
    )

    issues = SubTaskValidator(agent_registry).validate(
        make_decomposition(task_id, subtasks=[candidate, publish]),
        SubTaskValidationContext(task_id=task_id),
    )

    assert "MISSING_ONTOLOGY_REVIEW_GATE" not in {item.code for item in issues}


def test_fused_ontology_with_approval_is_executable(project_root, agent_registry) -> None:
    """已审核融合候选本体应满足本体发布的计划输入，不再要求普通候选。"""

    task_id = uuid4()
    fused = make_artifact(task_id, "FusedCandidateOntology")
    approval = make_artifact(task_id, "OntologyApproval")
    publish = make_subtask(
        task_id,
        subtask_type="ontology_publish",
        agent_type="graph_management_agent",
        inputs=[fused.artifact_id, approval.artifact_id],
        outputs=["PublishedOntology"],
    )
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
    )
    issues = SubTaskValidator(agent_registry, skills).validate(
        make_decomposition(task_id, subtasks=[publish]),
        SubTaskValidationContext(
            task_id=task_id,
            user_message="发布已审核的融合本体",
            available_artifacts=[fused, approval],
        ),
    )

    assert not {item.code for item in issues}.intersection(
        {"UNEXECUTABLE_SUBTASK_PLAN", "MISSING_ONTOLOGY_REVIEW_GATE"}
    )


def test_ontology_quality_evaluation_is_rejected(agent_registry) -> None:
    """总控不得重新插入已取消的候选本体质量评估步骤。"""

    task_id = uuid4()
    quality = make_subtask(
        task_id,
        subtask_type="ontology_quality_evaluate",
        agent_type="quality_evaluation_agent",
        outputs=["OntologyQualityReport"],
        human_review_required=True,
    )

    issues = SubTaskValidator(agent_registry).validate(
        make_decomposition(task_id, subtasks=[quality]),
        SubTaskValidationContext(task_id=task_id),
    )

    assert "ONTOLOGY_QUALITY_EVALUATION_NOT_ALLOWED" in {item.code for item in issues}


def test_clarification_is_control_result_not_new_intent(agent_registry) -> None:
    """没有当前任务的 CONTINUE 可请求澄清且不发布子任务。"""

    task_id = uuid4()
    result = make_decomposition(
        task_id,
        intent=TaskIntent.CONTINUE,
        subtasks=[],
        clarification=True,
    )

    issues = SubTaskValidator(agent_registry).validate(
        result, SubTaskValidationContext(task_id=task_id)
    )

    assert issues == []
    assert result.task_intent is TaskIntent.CONTINUE


def test_unrelated_quality_task_cannot_fake_review_gate(agent_registry) -> None:
    """旧的本体质量任务不能冒充候选本体直接审核门禁。"""

    task_id = uuid4()
    quality = make_subtask(
        task_id,
        subtask_type="ontology_quality_evaluate",
        agent_type="quality_evaluation_agent",
        outputs=["OntologyQualityReport"],
        human_review_required=True,
    )
    publish = make_subtask(
        task_id,
        subtask_type="ontology_publish",
        agent_type="graph_management_agent",
        outputs=["PublishedOntology"],
    )
    result = make_decomposition(task_id, subtasks=[quality, publish])

    issues = SubTaskValidator(agent_registry).validate(
        result, SubTaskValidationContext(task_id=task_id)
    )

    assert "MISSING_ONTOLOGY_REVIEW_GATE" in {item.code for item in issues}


def test_publish_permission_comes_from_agent_registry(agent_registry) -> None:
    """分配发布任务时必须检查 Registry 权限位，不只检查名称文本。"""

    task_id = uuid4()
    quality = make_subtask(
        task_id,
        subtask_type="ontology_quality_evaluate",
        agent_type="quality_evaluation_agent",
        outputs=["OntologyQualityReport"],
        human_review_required=True,
    )
    publish = make_subtask(
        task_id,
        subtask_type="ontology_publish",
        agent_type="knowledge_extraction_agent",
        outputs=["PublishedOntology"],
        dependencies=[quality.subtask_id],
    )
    result = make_decomposition(task_id, subtasks=[quality, publish])

    issues = SubTaskValidator(agent_registry).validate(
        result, SubTaskValidationContext(task_id=task_id)
    )

    assert "ILLEGAL_PUBLISH_AGENT" in {item.code for item in issues}


def test_external_ontology_strategy_requires_artifact(agent_registry) -> None:
    """选择外部本体模式时必须提供 ExternalOntology Artifact 引用。"""

    task_id = uuid4()
    result = make_decomposition(task_id, subtasks=[make_subtask(task_id)])
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={"ontology_strategy": OntologyStrategy(mode="IMPORT_EXTERNAL_JSON")}
            )
        }
    )

    issues = SubTaskValidator(agent_registry).validate(
        result, SubTaskValidationContext(task_id=task_id)
    )

    assert "MISSING_EXTERNAL_ONTOLOGY" in {item.code for item in issues}


def test_missing_ontology_forces_build_from_documents(agent_registry) -> None:
    """没有外部或已发布本体时，不允许总控虚构复用策略。"""

    task_id = uuid4()
    result = make_decomposition(task_id, subtasks=[make_subtask(task_id)])
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={
                    "ontology_strategy": OntologyStrategy(
                        mode="REUSE_PUBLISHED",
                        selected_ontology_id=uuid4(),
                        selected_ontology_version="1.0.0",
                    )
                }
            )
        }
    )

    issues = SubTaskValidator(agent_registry).validate(
        result, SubTaskValidationContext(task_id=task_id)
    )

    assert "BUILD_ONTOLOGY_STRATEGY_REQUIRED" in {item.code for item in issues}


def test_newly_published_ontology_does_not_rewrite_build_strategy(agent_registry) -> None:
    """本任务建设并发布本体后，最终验收仍应保留原始 BUILD 策略。"""

    task_id = uuid4()
    published = make_artifact(task_id, artifact_type="PublishedOntology")
    result = make_decomposition(task_id, subtasks=[])

    issues = SubTaskValidator(agent_registry).validate(
        result,
        SubTaskValidationContext(task_id=task_id, available_artifacts=[published]),
    )

    assert "SELECTED_PUBLISHED_ONTOLOGY_MUST_BE_REUSED" not in {item.code for item in issues}


def test_extraction_request_cannot_expand_to_graph_pipeline(agent_registry) -> None:
    """用户只要求抽取三元组时，不能增加候选图谱或图谱发布范围。"""

    task_id = uuid4()
    graph_task = make_subtask(
        task_id,
        subtask_type="graph_build",
        agent_type="graph_management_agent",
        outputs=["CandidateGraph"],
    )
    result = make_decomposition(task_id, subtasks=[graph_task])
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={"target_output_types": ["CandidateGraph", "PublishedGraph"]}
            )
        }
    )

    issues = SubTaskValidator(agent_registry).validate(
        result,
        SubTaskValidationContext(task_id=task_id, user_message="只抽取文档中的三元组"),
    )
    codes = {item.code for item in issues}

    assert "EXPLICIT_GRAPH_PUBLISH_REQUIRED" in codes
    assert "EXTRACTION_SCOPE_EXPANDED_TO_GRAPH" in codes


def test_entity_relation_request_requires_property_extraction(
    agent_registry,
) -> None:
    """实体关系抽取默认包含属性，带有 PropertyDataset 的计划可以通过该边界。"""

    task_id = uuid4()
    extraction = make_subtask(
        task_id,
        subtask_type="knowledge_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["EntityDataset", "RelationDataset", "PropertyDataset", "EvidenceDataset"],
    )
    result = make_decomposition(task_id, subtasks=[extraction])
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={"target_output_types": ["EntityDataset", "RelationDataset"]}
            )
        }
    )

    issues = SubTaskValidator(agent_registry).validate(
        result,
        SubTaskValidationContext(
            task_id=task_id,
            user_message="根据上传的 PDF 抽取实体关系并入库",
        ),
    )

    assert "MISSING_PROPERTY_EXTRACTION" not in {item.code for item in issues}


def test_entity_relation_request_rejects_plan_without_property_extraction(
    agent_registry,
) -> None:
    """实体、关系或三元组抽取计划缺失属性数据集时必须由总控修复。"""

    task_id = uuid4()
    extraction = make_subtask(
        task_id,
        subtask_type="knowledge_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["RelationDataset"],
    )
    result = make_decomposition(task_id, subtasks=[extraction])
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={"target_output_types": ["EntityDataset", "RelationDataset"]}
            )
        }
    )

    issues = SubTaskValidator(agent_registry).validate(
        result,
        SubTaskValidationContext(
            task_id=task_id,
            user_message="根据上传的 PDF 抽取实体关系并入库",
        ),
    )

    assert "MISSING_PROPERTY_EXTRACTION" in {item.code for item in issues}


def test_explicit_property_extraction_is_not_rejected(agent_registry) -> None:
    """用户明确要求属性时，属性数据集仍是合法目标。"""

    task_id = uuid4()
    extraction = make_subtask(
        task_id,
        subtask_type="knowledge_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["EntityDataset", "RelationDataset", "PropertyDataset", "EvidenceDataset"],
    )
    result = make_decomposition(task_id, subtasks=[extraction])
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={"target_output_types": ["PropertyDataset"]}
            )
        }
    )

    issues = SubTaskValidator(agent_registry).validate(
        result,
        SubTaskValidationContext(
            task_id=task_id,
            user_message="抽取实体、关系和属性字段",
        ),
    )

    assert "UNREQUESTED_PROPERTY_EXTRACTION" not in {item.code for item in issues}


def test_graph_publish_requires_explicit_current_user_authorization(agent_registry) -> None:
    """历史消息提到发布时，当前仅抽取消息仍不能获得图谱发布授权。"""

    task_id = uuid4()
    result = make_decomposition(task_id)
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={"target_output_types": ["PublishedGraph"]}
            )
        }
    )
    wrapped_message = (
        "以下是本任务最近对话：\nuser: 请发布图谱\n\n当前用户消息：现在只抽取三元组，不发布"
    )

    issues = SubTaskValidator(agent_registry).validate(
        result,
        SubTaskValidationContext(task_id=task_id, user_message=wrapped_message),
    )

    assert "EXPLICIT_GRAPH_PUBLISH_REQUIRED" in {item.code for item in issues}


def test_explicit_graph_publish_is_not_rejected_by_scope_guard(agent_registry) -> None:
    """用户当前明确要求图谱入库时，范围守卫不应误报缺少发布授权。"""

    task_id = uuid4()
    result = make_decomposition(task_id)
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={"target_output_types": ["PublishedGraph"]}
            )
        }
    )

    issues = SubTaskValidator(agent_registry).validate(
        result,
        SubTaskValidationContext(task_id=task_id, user_message="将已审核候选图谱确认入库"),
    )

    assert "EXPLICIT_GRAPH_PUBLISH_REQUIRED" not in {item.code for item in issues}


def test_entity_relation_ingestion_requires_published_graph_target(agent_registry) -> None:
    """用户明确要求实体关系入库时，不能把最终目标降级为两个抽取数据集。"""

    task_id = uuid4()
    result = make_decomposition(task_id)
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={"target_output_types": ["EntityDataset", "RelationDataset"]}
            )
        }
    )

    issues = SubTaskValidator(agent_registry).validate(
        result,
        SubTaskValidationContext(
            task_id=task_id,
            user_message="根据上传的pdf抽取里面的实体关系并入库",
        ),
    )

    assert "EXPLICIT_GRAPH_PUBLISH_TARGET_MISSING" in {item.code for item in issues}


def test_entity_relation_ingestion_accepts_published_graph_target(agent_registry) -> None:
    """实体关系入库请求以 PublishedGraph 为终点时应通过目标边界检查。"""

    task_id = uuid4()
    result = make_decomposition(task_id)
    result = result.model_copy(
        update={
            "task_spec": result.task_spec.model_copy(
                update={"target_output_types": ["PublishedGraph"]}
            )
        }
    )

    issues = SubTaskValidator(agent_registry).validate(
        result,
        SubTaskValidationContext(
            task_id=task_id,
            user_message="根据上传的pdf抽取里面的实体关系并入库",
        ),
    )

    codes = {item.code for item in issues}
    assert "EXPLICIT_GRAPH_PUBLISH_TARGET_MISSING" not in codes
    assert "EXPLICIT_GRAPH_PUBLISH_REQUIRED" not in codes
