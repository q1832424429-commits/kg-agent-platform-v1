"""验证总控 Agent 从对话到子任务发布的第一阶段最小闭环。"""

from pathlib import Path
from uuid import uuid4

import pytest

from kg_agent.agents.controller.agent import ControllerAgent
from kg_agent.agents.controller.nodes import ControllerNodes
from kg_agent.agents.controller.prompts import PromptRepository
from kg_agent.agents.controller.state import ControllerRequest
from kg_agent.domain.enums import TaskIntent
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.domain.task_models import OntologyStrategy
from kg_agent.exceptions import SubTaskValidationError
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.context_service import InMemoryContextService
from tests.factories import make_artifact, make_decomposition, make_subtask


def build_controller(
    *,
    project_root: Path,
    agent_registry,
    context_service: InMemoryContextService,
    responses,
    max_repairs: int = 2,
    skill_registry: SkillRegistry | None = None,
) -> tuple[ControllerAgent, FakeModelAdapter]:
    """创建使用真实总控图和Fake模型响应的测试实例。"""

    model = FakeModelAdapter(responses)
    controller = ControllerAgent(
        model_gateway=model,
        agent_registry=agent_registry,
        context_service=context_service,
        prompt_repository=PromptRepository(project_root / "configs" / "prompts" / "controller"),
        max_repairs=max_repairs,
        skill_registry=skill_registry,
    )
    return controller, model


@pytest.mark.asyncio
async def test_start_publishes_valid_subtask(project_root: Path, agent_registry) -> None:
    """START 应创建 TaskSpec revision 1 并发布给正确专业 Agent。"""

    task_id = uuid4()
    subtask = make_subtask(task_id)
    decomposition = make_decomposition(task_id, subtasks=[subtask])
    context_service = InMemoryContextService()
    controller, model = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=context_service,
        responses=[decomposition],
    )

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="解析我上传的制造业设备手册。",
        )
    )

    assert output.decomposition.task_intent is TaskIntent.START
    assert output.shared_context is not None
    # 模型响应中的临时 subtask_id 仅用于表示候选计划；正式持久化 ID 必须由总控
    # 的确定性归一化层重新生成，避免模型复用历史 UUID 覆盖任务。
    assert len(output.shared_context.subtask_ids) == 1
    assert output.shared_context.subtask_ids[0] != subtask.subtask_id
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_plan_review_mode_validates_without_publishing(
    project_root: Path, agent_registry
) -> None:
    """审核模式应返回完整合法候选，但 SharedContext 中不能出现正式子任务。"""

    task_id = uuid4()
    context_service = InMemoryContextService()
    controller, _ = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=context_service,
        responses=[make_decomposition(task_id, subtasks=[make_subtask(task_id)])],
    )

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="先生成候选任务计划供我审核。",
            publish_decomposition=False,
        )
    )

    assert output.decomposition.subtasks
    assert output.shared_context is None
    assert await context_service.get(task_id) is None
    assert await context_service.list_subtasks(task_id) == []


@pytest.mark.asyncio
async def test_controller_compiler_completes_atomic_skill_outputs(
    project_root: Path, agent_registry
) -> None:
    """统一知识抽取的四种原子输出由 Registry 补齐，无需模型记忆机械字段。"""

    task_id = uuid4()
    semantic = make_artifact(task_id, artifact_type="SemanticDocument")
    ontology = make_artifact(task_id, artifact_type="PublishedOntology").model_copy(
        update={"ontology_id": uuid4(), "ontology_version": "1.0.0"}
    )
    extraction = make_subtask(
        task_id,
        subtask_type="knowledge_extract",
        agent_type="knowledge_extraction_agent",
        inputs=[semantic.artifact_id, ontology.artifact_id],
        outputs=["EntityDataset"],
    )
    conflict = make_subtask(
        task_id,
        subtask_type="conflict_analyze",
        agent_type="knowledge_fusion_agent",
        outputs=["ConflictReport"],
        dependencies=[extraction.subtask_id],
    )
    plan = make_decomposition(task_id, subtasks=[extraction, conflict])
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
    )
    controller, model = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=InMemoryContextService(),
        responses=[plan],
        skill_registry=skills,
    )

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="使用已发布本体抽取关系并分析冲突。",
            existing_artifacts=[semantic, ontology],
        )
    )

    extraction_result = next(
        item for item in output.decomposition.subtasks if item.subtask_type == "knowledge_extract"
    )
    assert extraction_result.expected_output_types == [
        "EntityDataset",
        "RelationDataset",
        "PropertyDataset",
        "EvidenceDataset",
    ]
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_controller_receives_and_publishes_recalled_long_term_memory(
    project_root: Path, agent_registry
) -> None:
    """总控提示和发布后的 SharedContext 都必须包含 Mem0 召回摘要。"""

    task_id = uuid4()
    memory = MemoryRef(
        memory_id=uuid4(),
        memory_type="LONG_TERM",
        summary="术语映射：机台等同于设备",
        source_refs=["mem0://memory/term-map"],
    )
    context_service = InMemoryContextService()
    controller, model = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=context_service,
        responses=[make_decomposition(task_id, subtasks=[make_subtask(task_id)])],
    )

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="解析机台文档",
            recalled_long_term_memories=[memory],
        )
    )

    assert "术语映射：机台等同于设备" in model.calls[0].user_prompt
    assert output.shared_context is not None
    assert output.shared_context.long_term_memory_refs == [memory]


@pytest.mark.asyncio
async def test_selected_published_ontology_forces_reuse_and_prunes_rebuild_plan(
    project_root: Path, agent_registry
) -> None:
    """用户选定已发布本体后，不得重新建设或发布本体。"""

    task_id = uuid4()
    raw_document = make_artifact(task_id, artifact_type="RawDocument")
    selected_ontology = make_artifact(task_id, artifact_type="PublishedOntology").model_copy(
        update={"ontology_id": uuid4(), "ontology_version": "1.0.0"}
    )
    document_parse = make_subtask(
        task_id,
        inputs=[raw_document.artifact_id],
    )
    ontology_extract = make_subtask(
        task_id,
        subtask_type="ontology_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["CandidateOntology"],
        dependencies=[document_parse.subtask_id],
    )
    ontology_publish = make_subtask(
        task_id,
        subtask_type="ontology_publish",
        agent_type="graph_management_agent",
        outputs=["PublishedOntology"],
        dependencies=[ontology_extract.subtask_id],
    )
    knowledge_extract = make_subtask(
        task_id,
        subtask_type="knowledge_extract",
        agent_type="knowledge_extraction_agent",
        outputs=[
            "EntityDataset",
            "RelationDataset",
            "PropertyDataset",
            "EvidenceDataset",
        ],
        dependencies=[document_parse.subtask_id, ontology_publish.subtask_id],
    )
    # 模拟模型忽略了用户选择，错误地产生了本体建设、审核和发布路线。
    invalid_plan = make_decomposition(
        task_id,
        subtasks=[
            document_parse,
            ontology_extract,
            ontology_publish,
            knowledge_extract,
        ],
    )
    context_service = InMemoryContextService()
    controller, _ = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=context_service,
        responses=[invalid_plan],
    )

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="使用选择的本体从文档中抽取实体和属性。",
            existing_artifacts=[raw_document, selected_ontology],
        )
    )

    assert output.shared_context is not None
    strategy = output.decomposition.task_spec.ontology_strategy
    assert strategy.mode == "REUSE_PUBLISHED"
    assert strategy.selected_ontology_id == selected_ontology.ontology_id
    assert strategy.selected_ontology_version == "1.0.0"
    assert selected_ontology.artifact_id in output.decomposition.reused_artifact_ids
    persisted = await context_service.list_subtasks(task_id)
    assert {item.subtask_type for item in persisted}.isdisjoint(
        {"ontology_extract", "ontology_align", "ontology_fuse", "ontology_publish"}
    )
    persisted_extraction = next(
        item for item in persisted if item.subtask_type == "knowledge_extract"
    )
    assert selected_ontology.artifact_id in persisted_extraction.input_artifact_ids


@pytest.mark.asyncio
async def test_invalid_assignment_is_repaired_before_publish(
    project_root: Path, agent_registry
) -> None:
    """未知Agent分配必须经过修复，非法候选不能先写入SharedContext。"""

    task_id = uuid4()
    invalid = make_decomposition(
        task_id,
        subtasks=[make_subtask(task_id, agent_type="unknown_agent")],
    )
    valid = make_decomposition(task_id, subtasks=[make_subtask(task_id)])
    context_service = InMemoryContextService()
    controller, model = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=context_service,
        responses=[invalid, valid],
    )

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="解析设备手册。",
        )
    )

    assert output.shared_context is not None
    assert len(model.calls) == 2
    assert "UNKNOWN_AGENT" in model.calls[1].user_prompt


@pytest.mark.asyncio
async def test_continue_without_current_task_returns_clarification(
    project_root: Path, agent_registry
) -> None:
    """没有当前任务时，CONTINUE 应请求澄清且不创建SharedContext。"""

    task_id = uuid4()
    clarification = make_decomposition(
        task_id,
        intent=TaskIntent.CONTINUE,
        clarification=True,
    )
    context_service = InMemoryContextService()
    controller, _ = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=context_service,
        responses=[clarification],
    )

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="继续刚才的任务。",
        )
    )

    assert output.decomposition.requires_clarification is True
    assert output.shared_context is None
    assert await context_service.get(task_id) is None


@pytest.mark.asyncio
async def test_clarification_plan_discards_speculative_subtasks_without_repair(
    project_root: Path, agent_registry
) -> None:
    """澄清轮不得发布模型猜测的子任务，也不应为该矛盾额外调用模型修复。"""

    task_id = uuid4()
    clarification_with_guess = make_decomposition(
        task_id,
        clarification=True,
        subtasks=[make_subtask(task_id)],
    )
    context_service = InMemoryContextService()
    controller, model = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=context_service,
        responses=[clarification_with_guess],
    )

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="继续处理。",
        )
    )

    assert output.decomposition.requires_clarification is True
    assert output.decomposition.subtasks == []
    assert output.shared_context is None
    assert len(model.calls) == 1


@pytest.mark.asyncio
async def test_single_external_ontology_forces_import_strategy(
    project_root: Path, agent_registry
) -> None:
    """唯一外部本体是输入事实，不能被模型错误改写为已发布本体复用。"""

    task_id = uuid4()
    raw_document = make_artifact(task_id, artifact_type="RawDocument")
    external_ontology = make_artifact(task_id, artifact_type="ExternalOntology")
    invalid_strategy_plan = make_decomposition(
        task_id,
        subtasks=[make_subtask(task_id, inputs=[raw_document.artifact_id])],
    ).model_copy(
        update={
            "task_spec": make_decomposition(task_id).task_spec.model_copy(
                update={"ontology_strategy": OntologyStrategy(mode="REUSE_PUBLISHED")}
            )
        }
    )
    controller, _ = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=InMemoryContextService(),
        responses=[invalid_strategy_plan],
    )

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="解析上传文档。",
            existing_artifacts=[raw_document, external_ontology],
        )
    )

    strategy = output.decomposition.task_spec.ontology_strategy
    assert strategy.mode == "IMPORT_EXTERNAL_JSON"
    assert strategy.external_ontology_artifact_id == external_ontology.artifact_id


def test_entity_relation_ingestion_is_an_explicit_graph_publish_request() -> None:
    """总控编译器与范围校验器必须一致识别“实体关系并入库”。"""

    assert ControllerNodes._explicit_graph_publish_request(
        "根据上传的 pdf 抽取实体关系并入库"
    )
    assert not ControllerNodes._explicit_graph_publish_request("只抽取实体关系，不入库")


@pytest.mark.asyncio
async def test_validation_failure_never_publishes(project_root: Path, agent_registry) -> None:
    """修复次数耗尽后应抛错，不能写入非法子任务。"""

    task_id = uuid4()
    invalid = make_decomposition(
        task_id,
        subtasks=[make_subtask(task_id, agent_type="unknown_agent")],
    )
    context_service = InMemoryContextService()
    controller, _ = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=context_service,
        responses=[invalid],
        max_repairs=0,
    )

    with pytest.raises(SubTaskValidationError):
        await controller.run(
            ControllerRequest(
                task_id=task_id,
                conversation_id=uuid4(),
                user_message="解析文档。",
            )
        )

    assert await context_service.get(task_id) is None


@pytest.mark.asyncio
async def test_authoritative_parent_failure_does_not_call_model_repair(
    project_root: Path, agent_registry
) -> None:
    """权威父血缘错误不能交给看不到数据库事实的模型反复修复。"""

    task_id = uuid4()
    orphan = make_artifact(task_id).model_copy(
        update={"parent_artifact_ids": [uuid4()]}
    )
    plan = make_decomposition(
        task_id,
        subtasks=[make_subtask(task_id, inputs=[orphan.artifact_id])],
    ).model_copy(
        update={"reused_artifact_ids": [orphan.artifact_id]}
    )
    context_service = InMemoryContextService()
    controller, model = build_controller(
        project_root=project_root,
        agent_registry=agent_registry,
        context_service=context_service,
        responses=[plan],
    )

    with pytest.raises(SubTaskValidationError, match="REUSE_PARENT_LINEAGE_MISMATCH"):
        await controller.run(
            ControllerRequest(
                task_id=task_id,
                conversation_id=uuid4(),
                user_message="复用现有解析结果。",
                existing_artifacts=[orphan],
            )
        )

    assert len(model.calls) == 1
