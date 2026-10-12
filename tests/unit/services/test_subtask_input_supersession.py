"""验证下游只接收未被同类型新版本取代的 Artifact。"""

from uuid import uuid4

from kg_agent.services.subtask_input_resolver import (
    discard_superseded_ancestor_inputs,
    select_effective_input_refs,
)
from tests.factories import make_artifact


def test_transform_output_replaces_implicit_parent() -> None:
    """同类变换产物应替代隐式祖先，且无关文档不受影响。"""

    task_id = uuid4()
    old = make_artifact(task_id, artifact_type="EntityDataset")
    new = make_artifact(task_id, artifact_type="EntityDataset").model_copy(
        update={"parent_artifact_ids": [old.artifact_id]}
    )
    unrelated = make_artifact(task_id, artifact_type="EntityDataset")

    selected = discard_superseded_ancestor_inputs([old, new, unrelated], explicit_ids=set())

    assert [item.artifact_id for item in selected] == [
        new.artifact_id,
        unrelated.artifact_id,
    ]


def test_explicit_parent_is_preserved() -> None:
    """用户明确指定的旧版本不能被下游绑定器静默丢弃。"""

    task_id = uuid4()
    old = make_artifact(task_id, artifact_type="EntityDataset")
    new = make_artifact(task_id, artifact_type="EntityDataset").model_copy(
        update={"parent_artifact_ids": [old.artifact_id]}
    )

    selected = discard_superseded_ancestor_inputs([old, new], explicit_ids={old.artifact_id})

    assert [item.artifact_id for item in selected] == [old.artifact_id, new.artifact_id]


def test_cross_type_intermediate_still_supersedes_old_entity_dataset() -> None:
    """先完整遍历血缘再按 Agent 类型裁剪，不能漏掉跨类型中间版本。"""

    task_id = uuid4()
    old = make_artifact(task_id, artifact_type="EntityDataset")
    intermediate = make_artifact(task_id, artifact_type="StructuredDataset").model_copy(
        update={"parent_artifact_ids": [old.artifact_id]}
    )
    new = make_artifact(task_id, artifact_type="EntityDataset").model_copy(
        update={"parent_artifact_ids": [intermediate.artifact_id]}
    )

    current = discard_superseded_ancestor_inputs([old, intermediate, new], explicit_ids=set())
    entities = [item for item in current if item.artifact_type == "EntityDataset"]

    assert [item.artifact_id for item in entities] == [new.artifact_id]


def test_effective_inputs_keep_every_untouched_artifact_type() -> None:
    """自定义同型产物只替代其旧版，关系、属性、证据等其他输入均保留。"""

    task_id = uuid4()
    old_entity = make_artifact(task_id, artifact_type="EntityDataset")
    new_entity = make_artifact(task_id, artifact_type="EntityDataset").model_copy(
        update={"parent_artifact_ids": [old_entity.artifact_id]}
    )
    relation = make_artifact(task_id, artifact_type="RelationDataset")
    property_data = make_artifact(task_id, artifact_type="PropertyDataset")
    evidence = make_artifact(task_id, artifact_type="EvidenceDataset")
    refs = [old_entity, relation, property_data, evidence, new_entity]

    selected = select_effective_input_refs(
        refs,
        explicit_ids=set(),
        accepted_types={item.artifact_type for item in refs},
    )

    assert {item.artifact_id for item in selected} == {
        new_entity.artifact_id,
        relation.artifact_id,
        property_data.artifact_id,
        evidence.artifact_id,
    }
