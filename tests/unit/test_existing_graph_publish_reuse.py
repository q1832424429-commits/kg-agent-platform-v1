"""回归验证文档建本体后继续发布已有候选图谱的复用边界。"""

from types import SimpleNamespace
from uuid import uuid4

from kg_agent.agents.controller.nodes import ControllerNodes
from kg_agent.agents.controller.state import ControllerRequest
from kg_agent.domain.enums import TaskIntent
from kg_agent.domain.task_models import OntologyStrategy
from kg_agent.validators.artifact_reuse_validator import is_task_spec_compatible
from tests.factories import make_artifact, make_decomposition, make_subtask, make_task_spec


def test_existing_published_ontology_is_not_a_new_user_selection() -> None:
    """历史已发布本体不能把 BUILD_FROM_DOCUMENTS 偷换成 REUSE_PUBLISHED。"""

    task_id, conversation_id, ontology_id = uuid4(), uuid4(), uuid4()
    published = make_artifact(task_id, "PublishedOntology").model_copy(
        update={"ontology_id": ontology_id, "ontology_version": "1.0.0"}
    )
    old = make_task_spec(task_id)
    context = SimpleNamespace(current_task_spec=old, artifact_refs=[published])
    request = ControllerRequest(
        task_id=task_id,
        conversation_id=conversation_id,
        user_message="发布图谱",
        existing_artifacts=[published],
    )
    assert ControllerNodes._selected_published_ontology(
        request=request, current_context=context
    ) is None

    imported = published.model_copy(update={"artifact_id": uuid4()})
    selected = ControllerNodes._selected_published_ontology(
        request=request.model_copy(update={"existing_artifacts": [published, imported]}),
        current_context=context,
    )
    assert selected is not None and selected.artifact_id == imported.artifact_id


def test_publish_only_preserves_source_scope_and_ontology_lineage() -> None:
    """即使模型误报 MODIFY，纯发布也只扩展目标而不重做已有图谱。"""

    task_id, conversation_id, ontology_id = uuid4(), uuid4(), uuid4()
    raw = make_artifact(task_id, "RawDocument")
    published = make_artifact(task_id, "PublishedOntology").model_copy(
        update={"ontology_id": ontology_id, "ontology_version": "1.0.0"}
    )
    graph = make_artifact(task_id, "CandidateGraph").model_copy(
        update={"ontology_id": ontology_id, "ontology_version": "1.0.0"}
    )
    conflict = make_artifact(task_id, "ConflictReport").model_copy(
        update={"ontology_id": ontology_id, "ontology_version": "1.0.0"}
    )
    old = make_task_spec(task_id).model_copy(
        update={
            "source_artifact_ids": [raw.artifact_id],
            "target_output_types": ["CandidateGraph"],
            "extraction_scope": {"equipment": "manufacturing"},
        }
    )
    context = SimpleNamespace(
        current_task_spec=old,
        artifact_refs=[raw, published, graph, conflict],
        master_task=SimpleNamespace(current_decomposition_revision=1),
    )
    request = ControllerRequest(
        task_id=task_id,
        conversation_id=conversation_id,
        user_message="发布图谱",
        existing_artifacts=context.artifact_refs,
    )
    mistaken = make_decomposition(task_id, intent=TaskIntent.MODIFY, revision=2)
    mistaken = mistaken.model_copy(
        update={
            "task_spec": mistaken.task_spec.model_copy(
                update={
                    "source_artifact_ids": [graph.artifact_id],
                    "target_output_types": ["PublishedGraph"],
                    "ontology_strategy": OntologyStrategy(
                        mode="REUSE_PUBLISHED",
                        selected_ontology_id=ontology_id,
                        selected_ontology_version="1.0.0",
                    ),
                    "extraction_scope": {"unexpected": True},
                }
            )
        }
    )
    nodes = object.__new__(ControllerNodes)
    nodes._skill_registry = None
    normalized = nodes._apply_operation_constraints(
        mistaken, {"request": request, "current_context": context}
    )
    assert normalized.task_intent is TaskIntent.CONTINUE
    assert normalized.task_spec.business_goal == "发布当前候选图谱"
    assert normalized.task_spec.source_artifact_ids == old.source_artifact_ids
    assert normalized.task_spec.extraction_scope == old.extraction_scope
    assert normalized.task_spec.ontology_strategy == old.ontology_strategy
    assert normalized.task_spec.target_output_types == ["CandidateGraph", "PublishedGraph"]
    assert is_task_spec_compatible(graph, old, normalized.task_spec)
    assert is_task_spec_compatible(conflict, old, normalized.task_spec)
    changed_source = normalized.task_spec.model_copy(
        update={"source_artifact_ids": [graph.artifact_id]}
    )
    assert not is_task_spec_compatible(graph, old, changed_source)
    changed_scope = normalized.task_spec.model_copy(update={"extraction_scope": {"other": True}})
    assert not is_task_spec_compatible(graph, old, changed_scope)


def test_task_generated_ontology_remains_available_to_knowledge_extraction() -> None:
    """历史本体不改变来源策略，但仍能作为下轮实例抽取的真实输入。"""

    task_id, conversation_id = uuid4(), uuid4()
    published = make_artifact(task_id, "PublishedOntology").model_copy(
        update={"ontology_id": uuid4(), "ontology_version": "1.0.0"}
    )
    old = make_task_spec(task_id)
    context = SimpleNamespace(
        current_task_spec=old,
        artifact_refs=[published],
        master_task=SimpleNamespace(current_decomposition_revision=1),
    )
    request = ControllerRequest(
        task_id=task_id,
        conversation_id=conversation_id,
        user_message="继续抽取实体关系",
        existing_artifacts=[published],
    )
    extraction = make_subtask(
        task_id,
        subtask_type="knowledge_extract",
        agent_type="knowledge_extraction_agent",
        outputs=["EntityDataset"],
    )
    candidate = make_decomposition(task_id, intent=TaskIntent.CONTINUE, subtasks=[extraction])
    nodes = object.__new__(ControllerNodes)
    nodes._skill_registry = None
    normalized = nodes._apply_operation_constraints(
        candidate, {"request": request, "current_context": context}
    )
    assert normalized.task_spec.ontology_strategy.mode == "BUILD_FROM_DOCUMENTS"
    assert published.artifact_id in normalized.subtasks[0].input_artifact_ids
