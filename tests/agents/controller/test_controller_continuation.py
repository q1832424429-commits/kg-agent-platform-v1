"""验证总控 Agent 对 CONTINUE、MODIFY 和上下文裁剪的处理。"""

import json
from datetime import timedelta
from pathlib import Path
from uuid import uuid4

import pytest

from kg_agent.agents.controller.agent import ControllerAgent
from kg_agent.agents.controller.prompts import (
    PromptRepository,
    build_decomposition_prompt,
)
from kg_agent.agents.controller.state import ControllerRequest
from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.enums import ArtifactStatus, TaskIntent
from kg_agent.exceptions import SubTaskValidationError
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.services.context_service import InMemoryContextService
from tests.factories import NOW, make_artifact, make_decomposition, make_subtask


def make_controller(
    project_root: Path,
    agent_registry,
    context_service: InMemoryContextService,
    responses,
) -> ControllerAgent:
    """创建用于多轮任务测试的总控 Agent。"""

    return ControllerAgent(
        model_gateway=FakeModelAdapter(responses),
        agent_registry=agent_registry,
        context_service=context_service,
        prompt_repository=PromptRepository(project_root / "configs" / "prompts" / "controller"),
    )


@pytest.mark.asyncio
async def test_continue_appends_only_new_subtask(project_root: Path, agent_registry) -> None:
    """CONTINUE 应保留旧子任务并只追加后续工作。"""

    task_id = uuid4()
    conversation_id = uuid4()
    context_service = InMemoryContextService()
    parse_subtask = make_subtask(task_id)
    initial = await context_service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[parse_subtask]),
        source_artifacts=[],
        expected_version=None,
    )
    extraction = make_subtask(
        task_id,
        subtask_type="ontology_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["CandidateOntology"],
        dependencies=[parse_subtask.subtask_id],
        decomposition_revision=2,
    )
    continued = TaskDecompositionResult(
        task_intent=TaskIntent.CONTINUE,
        task_spec=initial.current_task_spec,
        subtasks=[extraction],
    )
    controller = make_controller(project_root, agent_registry, context_service, [continued])

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            current_task_id=task_id,
            conversation_id=conversation_id,
            user_message="继续抽取候选本体。",
        )
    )

    assert output.shared_context is not None
    assert output.shared_context.version == initial.version + 1
    persisted_subtasks = await context_service.list_subtasks(task_id)
    persisted_ids = {item.subtask_id for item in persisted_subtasks}
    # 旧任务 ID 必须保留；本轮模型给出的 extraction ID 则不能直接写入持久化层。
    assert parse_subtask.subtask_id in persisted_ids
    assert extraction.subtask_id not in persisted_ids
    appended = next(
        item for item in persisted_subtasks if item.subtask_id != parse_subtask.subtask_id
    )
    assert appended.dependency_subtask_ids == [parse_subtask.subtask_id]


@pytest.mark.asyncio
async def test_model_cannot_reuse_historical_subtask_id_when_planning_new_work(
    project_root: Path, agent_registry
) -> None:
    """模型误用历史 UUID 时，运行时必须改写为新的持久化子任务 ID。"""

    task_id = uuid4()
    conversation_id = uuid4()
    context_service = InMemoryContextService()
    historical = make_subtask(task_id)
    initial = await context_service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[historical]),
        source_artifacts=[],
        expected_version=None,
    )
    # 模拟 LLM 复制了历史子任务 UUID。该 ID 不能成为本轮新任务的数据库主键。
    model_subtask = make_subtask(
        task_id,
        subtask_type="ontology_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["CandidateOntology"],
        decomposition_revision=2,
        subtask_id=historical.subtask_id,
    )
    planned = TaskDecompositionResult(
        task_intent=TaskIntent.CONTINUE,
        task_spec=initial.current_task_spec,
        subtasks=[model_subtask],
    )
    controller = make_controller(project_root, agent_registry, context_service, [planned])

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            current_task_id=task_id,
            conversation_id=conversation_id,
            user_message="继续构建候选本体。",
        )
    )

    assert output.shared_context is not None
    persisted = await context_service.list_subtasks(task_id)
    assert len(persisted) == 2
    assert {item.subtask_id for item in persisted} != {historical.subtask_id}
    assert any(
        item.subtask_type == "ontology_extract" and item.subtask_id != historical.subtask_id
        for item in persisted
    )


@pytest.mark.asyncio
async def test_modify_creates_revision_and_preserves_artifact(
    project_root: Path, agent_registry
) -> None:
    """MODIFY 应创建下一版本 TaskSpec 并保留已有 ArtifactRef。"""

    task_id = uuid4()
    conversation_id = uuid4()
    artifact = make_artifact(task_id)
    context_service = InMemoryContextService()
    initial = await context_service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[artifact],
        expected_version=None,
    )
    modified_subtask = make_subtask(
        task_id,
        subtask_type="ontology_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["CandidateOntology"],
        inputs=[artifact.artifact_id],
        decomposition_revision=2,
    )
    modified = make_decomposition(
        task_id,
        intent=TaskIntent.MODIFY,
        revision=2,
        subtasks=[modified_subtask],
    ).model_copy(update={"reused_artifact_ids": [artifact.artifact_id]})
    controller = make_controller(project_root, agent_registry, context_service, [modified])

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            current_task_id=task_id,
            conversation_id=conversation_id,
            user_message="修改任务，只抽取设备候选本体。",
        )
    )

    assert output.shared_context is not None
    assert output.shared_context.version == initial.version + 1
    assert output.shared_context.current_task_spec.revision == 2
    assert output.shared_context.artifact_refs == [artifact]


@pytest.mark.asyncio
async def test_ontology_publish_automatically_waits_for_candidate_review(
    project_root: Path, agent_registry
) -> None:
    """发布本体时，已有候选本体任务必须自动成为人工审核暂停点。"""

    task_id = uuid4()
    conversation_id = uuid4()
    candidate_ontology = make_subtask(
        task_id,
        subtask_type="ontology_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["CandidateOntology"],
    )
    publish = make_subtask(
        task_id,
        subtask_type="ontology_publish",
        agent_type="graph_management_agent",
        outputs=["PublishedOntology"],
    )
    planned = make_decomposition(task_id, subtasks=[candidate_ontology, publish])
    context_service = InMemoryContextService()
    controller = make_controller(project_root, agent_registry, context_service, [planned])

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=conversation_id,
            user_message="根据文档构建、审核并发布本体。",
        )
    )

    assert output.shared_context is not None
    subtasks = await context_service.list_subtasks(task_id)
    persisted_candidate = next(item for item in subtasks if item.subtask_type == "ontology_extract")
    persisted_publish = next(item for item in subtasks if item.subtask_type == "ontology_publish")
    assert persisted_candidate.subtask_id != candidate_ontology.subtask_id
    assert persisted_publish.subtask_id != publish.subtask_id
    assert persisted_candidate.human_review_required is True
    assert persisted_candidate.subtask_id in persisted_publish.dependency_subtask_ids


@pytest.mark.asyncio
async def test_continue_publish_graph_reuses_latest_candidate_and_adds_review_gate(
    project_root: Path, agent_registry
) -> None:
    """已有候选图谱继续发布时应自动补齐质量评估、审核依赖和精确复用。"""

    task_id = uuid4()
    conversation_id = uuid4()
    conflict = make_artifact(task_id, artifact_type="ConflictReport")
    candidate_graph = make_artifact(task_id, artifact_type="CandidateGraph").model_copy(
        update={"parent_artifact_ids": [conflict.artifact_id]}
    )
    contexts = InMemoryContextService()
    initial_result = make_decomposition(task_id, subtasks=[])
    initial_result = initial_result.model_copy(
        update={
            "task_spec": initial_result.task_spec.model_copy(
                update={"target_output_types": ["CandidateGraph"]}
            )
        }
    )
    await contexts.publish_decomposition(
        conversation_id=conversation_id,
        result=initial_result,
        source_artifacts=[conflict, candidate_graph],
        expected_version=None,
    )
    publish = make_subtask(
        task_id,
        subtask_type="graph_publish",
        agent_type="graph_management_agent",
        outputs=["PublishedGraph"],
        decomposition_revision=2,
    )
    model_plan = make_decomposition(
        task_id,
        intent=TaskIntent.CONTINUE,
        subtasks=[publish],
    )
    controller = make_controller(project_root, agent_registry, contexts, [model_plan])

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            current_task_id=task_id,
            conversation_id=conversation_id,
            user_message="发布图谱",
        )
    )

    assert output.shared_context is not None
    assert output.decomposition.task_intent is TaskIntent.CONTINUE
    assert output.decomposition.task_spec.revision == 2
    assert "PublishedGraph" in output.decomposition.task_spec.target_output_types
    quality = next(
        item
        for item in output.decomposition.subtasks
        if item.subtask_type == "graph_quality_evaluate"
    )
    persisted_publish = next(
        item for item in output.decomposition.subtasks if item.subtask_type == "graph_publish"
    )
    assert quality.human_review_required is True
    assert quality.input_artifact_ids == [
        candidate_graph.artifact_id,
        conflict.artifact_id,
    ]
    assert quality.subtask_id in persisted_publish.dependency_subtask_ids
    assert output.decomposition.reused_artifact_ids == sorted(
        [candidate_graph.artifact_id, conflict.artifact_id], key=str
    )


@pytest.mark.asyncio
async def test_continue_publish_graph_with_exact_approval_skips_quality_recheck(
    project_root: Path, agent_registry
) -> None:
    """当前候选版本已有 GraphApproval 时应直接进入发布，不重复质量评估。"""

    task_id = uuid4()
    conversation_id = uuid4()
    conflict = make_artifact(task_id, artifact_type="ConflictReport")
    candidate_graph = make_artifact(task_id, artifact_type="CandidateGraph").model_copy(
        update={"parent_artifact_ids": [conflict.artifact_id]}
    )
    approval = make_artifact(task_id, artifact_type="GraphApproval").model_copy(
        update={"parent_artifact_ids": [candidate_graph.artifact_id]}
    )
    contexts = InMemoryContextService()
    initial_result = make_decomposition(task_id, subtasks=[])
    initial_result = initial_result.model_copy(
        update={
            "task_spec": initial_result.task_spec.model_copy(
                update={"target_output_types": ["CandidateGraph"]}
            )
        }
    )
    await contexts.publish_decomposition(
        conversation_id=conversation_id,
        result=initial_result,
        source_artifacts=[conflict, candidate_graph, approval],
        expected_version=None,
    )
    publish = make_subtask(
        task_id,
        subtask_type="graph_publish",
        agent_type="graph_management_agent",
        outputs=["PublishedGraph"],
        decomposition_revision=2,
    )
    model_plan = make_decomposition(
        task_id,
        intent=TaskIntent.CONTINUE,
        subtasks=[publish],
    )
    controller = make_controller(project_root, agent_registry, contexts, [model_plan])

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            current_task_id=task_id,
            conversation_id=conversation_id,
            user_message="发布图谱",
        )
    )

    assert not any(
        item.subtask_type == "graph_quality_evaluate"
        for item in output.decomposition.subtasks
    )
    persisted_publish = next(
        item for item in output.decomposition.subtasks if item.subtask_type == "graph_publish"
    )
    assert persisted_publish.input_artifact_ids == [
        candidate_graph.artifact_id,
        approval.artifact_id,
    ]
    assert output.decomposition.reused_artifact_ids == sorted(
        [candidate_graph.artifact_id, approval.artifact_id], key=str
    )


@pytest.mark.asyncio
async def test_start_normalizes_model_generated_revisions(
    project_root: Path, agent_registry
) -> None:
    """新任务的 revision 由运行时代码固定为 1，而不是依赖模型输出。"""

    task_id = uuid4()
    planned = make_decomposition(
        task_id,
        revision=7,
        subtasks=[make_subtask(task_id, decomposition_revision=9)],
    )
    context_service = InMemoryContextService()
    controller = make_controller(project_root, agent_registry, context_service, [planned])

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=uuid4(),
            user_message="根据文档建立候选本体。",
        )
    )

    assert output.shared_context is not None
    assert output.shared_context.current_task_spec.revision == 1
    subtasks = await context_service.list_subtasks(task_id)
    assert [item.decomposition_revision for item in subtasks] == [1]


def test_prompt_context_excludes_artifact_content_uri(agent_registry) -> None:
    """模型上下文只包含 Artifact 摘要和血缘，不暴露对象存储地址。"""

    task_id = uuid4()
    artifact = make_artifact(task_id)
    request = ControllerRequest(
        task_id=task_id,
        conversation_id=uuid4(),
        user_message="继续抽取实体。",
        existing_artifacts=[artifact],
    )

    prompt = build_decomposition_prompt(
        request=request,
        current_context=None,
        known_subtasks=[],
        registry=agent_registry,
    )

    assert artifact.content_uri not in prompt
    assert artifact.content_summary in prompt
    catalog = json.loads(prompt)["agent_catalog"]
    assert all(item["accepted_subtask_types"] for item in catalog)
    # 总控只输出语义规划草案；运行时字段由代码编译器补齐，不再让模型生成。
    output_contract = json.loads(prompt)["output_contract"]
    assert output_contract["title"] == "TaskPlanDraft"
    assert "$defs" in output_contract


@pytest.mark.asyncio
async def test_prompt_uses_compact_deduplicated_reuse_candidates(agent_registry) -> None:
    """续办 Prompt 仅提供每类最近两个可用复用候选，不重复完整 Artifact 清单。"""

    task_id = uuid4()
    conversation_id = uuid4()
    artifacts = [
        make_artifact(task_id, artifact_type="EntityDataset").model_copy(
            update={
                "created_at": NOW + timedelta(minutes=index),
                "content_summary": "很长的摘要 " * 100,
            }
        )
        for index in range(4)
    ]
    unavailable = make_artifact(task_id, artifact_type="RelationDataset").model_copy(
        update={"status": ArtifactStatus.INVALIDATED}
    )
    context_service = InMemoryContextService()
    current = await context_service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id),
        source_artifacts=[*artifacts, unavailable],
        expected_version=None,
    )
    request = ControllerRequest(
        task_id=task_id,
        current_task_id=task_id,
        conversation_id=conversation_id,
        user_message="继续使用已有实体构图。",
        existing_artifacts=[*artifacts, unavailable],
    )

    payload = json.loads(
        build_decomposition_prompt(
            request=request,
            current_context=current,
            known_subtasks=[],
            registry=agent_registry,
            task_spec_history={1: current.current_task_spec},
        )
    )

    candidates = payload["current_context_view"]["reusable_artifact_candidates"]
    assert len(candidates) == 2
    assert [item["artifact_id"] for item in candidates] == [
        str(artifacts[3].artifact_id),
        str(artifacts[2].artifact_id),
    ]
    assert payload["provided_artifacts"] == []
    assert all("checksum" not in item and "parent_artifact_ids" not in item for item in candidates)
    assert all(len(item["content_summary"]) <= 241 for item in candidates)


@pytest.mark.asyncio
async def test_prompt_excludes_rejected_reuse_candidate_during_repair(agent_registry) -> None:
    """已被当前轮复用校验拒绝的 Artifact 不会再次进入总控修复候选集。"""

    task_id = uuid4()
    conversation_id = uuid4()
    artifact = make_artifact(task_id, artifact_type="RelationDataset")
    context_service = InMemoryContextService()
    current = await context_service.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id),
        source_artifacts=[artifact],
        expected_version=None,
    )

    payload = json.loads(
        build_decomposition_prompt(
            request=ControllerRequest(
                task_id=task_id,
                current_task_id=task_id,
                conversation_id=conversation_id,
                user_message="继续检查关系。",
                existing_artifacts=[artifact],
            ),
            current_context=current,
            known_subtasks=[],
            registry=agent_registry,
            task_spec_history={1: current.current_task_spec},
            excluded_reuse_artifact_ids={artifact.artifact_id},
        )
    )

    assert payload["current_context_view"]["reusable_artifact_candidates"] == []


def test_controller_prompts_keep_semantics_but_not_validator_rule_catalog(
    project_root: Path,
) -> None:
    """总控 Prompt 只保留语义决策，不复制确定性校验器的规则清单。"""

    repository = PromptRepository(project_root / "configs" / "prompts" / "controller")
    system_prompt = repository.load_system_prompt()
    decomposition_prompt = repository.load_decomposition_prompt()

    # 模型仍负责理解用户终点、任务变化、本体来源和最少完整计划。
    assert "新建、继续还是修改" in system_prompt
    assert "文档解析、知识抽取、候选图谱构建、人工审核、发布入库" in system_prompt
    assert "最少且完整的 Agent 级子任务" in system_prompt
    assert "本体来源按当前事实选择" in system_prompt
    assert "最少 Agent 级子任务计划" in decomposition_prompt

    # UUID、状态、具体 Artifact 输入、依赖图和 Skill 契约由 Pydantic Schema、
    # SubTaskValidator、HandoffValidator 与 Registry 校验，不再逐项写进 Prompt。
    validator_only_phrases = {
        "初始子任务状态只能使用",
        "新 subtask_id 使用合法 UUID",
        "EvidenceDataset",
        "UNEXECUTABLE_SUBTASK_PLAN",
        "dependency_subtask_ids 连接",
        "decomposition_revision 为 1",
    }
    combined = system_prompt + decomposition_prompt
    assert not any(phrase in combined for phrase in validator_only_phrases)
    assert len(combined) < 1800


@pytest.mark.asyncio
async def test_existing_task_is_loaded_even_without_current_task_id(
    project_root: Path, agent_registry
) -> None:
    """漏传 current_task_id 时仍按 task_id 读取现有事实，不能错误执行 START。"""

    task_id = uuid4()
    conversation_id = uuid4()
    contexts = InMemoryContextService()
    await contexts.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    invalid_start = make_decomposition(task_id, subtasks=[make_subtask(task_id)])
    controller = make_controller(project_root, agent_registry, contexts, [invalid_start])

    with pytest.raises(SubTaskValidationError) as error:
        await controller.run(
            ControllerRequest(
                task_id=task_id,
                conversation_id=conversation_id,
                user_message="重新开始。",
            )
        )

    assert "START_WITH_CURRENT_TASK" in str(error.value)


@pytest.mark.asyncio
async def test_final_verification_preserves_current_task_and_forces_continue(
    project_root: Path, agent_registry
) -> None:
    """内部最终验收即使模型误报 START，也只能沿用当前任务做 CONTINUE。"""

    task_id = uuid4()
    conversation_id = uuid4()
    contexts = InMemoryContextService()
    current = await contexts.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    invalid_start = make_decomposition(task_id, subtasks=[])
    controller = make_controller(project_root, agent_registry, contexts, [invalid_start])

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=conversation_id,
            user_message="检查最终目标是否完成。",
            operation="FINAL_VERIFICATION",
        )
    )

    assert output.decomposition.task_intent is TaskIntent.CONTINUE
    assert output.decomposition.task_spec == current.current_task_spec
    assert output.decomposition.subtasks == []
    assert output.shared_context is not None


@pytest.mark.asyncio
async def test_system_continuation_preserves_repair_subtasks(
    project_root: Path, agent_registry
) -> None:
    """审核修改等系统续办应固定 CONTINUE，但不能像最终验收一样丢弃续办任务。"""

    task_id = uuid4()
    conversation_id = uuid4()
    contexts = InMemoryContextService()
    current = await contexts.publish_decomposition(
        conversation_id=conversation_id,
        result=make_decomposition(task_id, subtasks=[make_subtask(task_id)]),
        source_artifacts=[],
        expected_version=None,
    )
    repair_subtask = make_subtask(
        task_id,
        subtask_type="ontology_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["CandidateOntology"],
        decomposition_revision=2,
    )
    model_start = make_decomposition(task_id, subtasks=[repair_subtask])
    controller = make_controller(project_root, agent_registry, contexts, [model_start])

    output = await controller.run(
        ControllerRequest(
            task_id=task_id,
            conversation_id=conversation_id,
            user_message="应用用户审核修改并重新送审。",
            operation="SYSTEM_CONTINUATION",
        )
    )

    assert output.decomposition.task_intent is TaskIntent.CONTINUE
    assert output.decomposition.task_spec == current.current_task_spec
    assert len(output.decomposition.subtasks) == 1
    assert output.decomposition.subtasks[0].subtask_type == repair_subtask.subtask_type
    assert output.decomposition.subtasks[0].subtask_id != repair_subtask.subtask_id
