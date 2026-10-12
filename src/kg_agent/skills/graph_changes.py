"""将人工确认的 GraphChangeSet 确定性应用到候选图谱。

本模块实现既有 ``apply_graph_changeset`` Skill 的业务语义：它读取不可变的
CandidateGraph 与 GraphChangeSet，校验审核目标和校验和后生成一个新的 CandidateGraph。
它不发布图谱、不修改原 Artifact，也不负责替代后续质量评估与人工审核门禁。
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import ValidationError

from kg_agent.domain.artifact_content_models import (
    CandidateGraphContent,
    ChangeOperation,
    ChangeSetContent,
    EntityRecord,
    PropertyRecord,
    RelationRecord,
)
from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillArtifactOutput, SkillInvocation, SkillRunResult
from kg_agent.skills.llm import ArtifactContentReader


class GraphChangeSetApplySkill:
    """把已确认的图谱修改原子应用为新的候选图谱 Artifact 内容。

    生命周期仅覆盖图谱管理更新 Agent 的一次 Skill 调用。所有 ``ADD``、``UPDATE``、
    ``DELETE``、``MERGE``、``SPLIT`` 操作在内存副本上一次性校验；任一步不合法时
    不返回半成品输出，调用方仍可保留原候选图谱并修订 ChangeSet。
    """

    def __init__(
        self, registration: SkillRegistration, *, artifact_reader: ArtifactContentReader
    ) -> None:
        """绑定既有 ``apply_graph_changeset`` 注册契约和只读 Artifact 读取器。

        参数：
        - registration：已由 Registry 校验的 Skill 元数据。
        - artifact_reader：读取 CandidateGraph 与 GraphChangeSet 正文的服务协议。

        异常：当注册契约被错误绑定到本实现时抛出 ``ValueError``。
        """

        if registration.name != "apply_graph_changeset":
            raise ValueError("图谱修改实现只能绑定 apply_graph_changeset")
        if registration.input_artifact_types != ["CandidateGraph", "GraphChangeSet"]:
            raise ValueError("apply_graph_changeset 输入契约必须是 CandidateGraph、GraphChangeSet")
        if registration.output_artifact_types != ["CandidateGraph"]:
            raise ValueError("apply_graph_changeset 输出契约必须是 CandidateGraph")
        self._registration = registration
        self._reader = artifact_reader

    @property
    def name(self) -> str:
        """返回与 Registry 一致的稳定 Skill 名称。"""

        return self._registration.name

    async def execute(self, invocation: SkillInvocation) -> SkillRunResult:
        """校验 ChangeSet 目标并生成新的候选图谱。

        参数：
        - invocation：包含同一任务的候选图谱与用户提交修改集的调用上下文。

        返回：成功时包含一个新的 ``CandidateGraph`` 输出；失败时不包含输出，保证
        ChangeSet 的原子性。
        """

        try:
            candidate_ref, changeset_ref, graph, changeset = await self._load_inputs(invocation)
            self._validate_target(candidate_ref.checksum, candidate_ref.artifact_id, changeset)
            updated = self._apply_all(graph, changeset.operations)
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
            return self._failed(
                invocation,
                "GRAPH_CHANGESET_APPLY_FAILED",
                f"图谱修改未应用：{type(exc).__name__}: {exc}",
            )

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.SUCCESS,
            outputs=[
                SkillArtifactOutput(
                    artifact_type="CandidateGraph",
                    content=updated.model_dump(mode="json"),
                    content_summary=(
                        f"已原子应用 {len(changeset.operations)} 项用户图谱修改："
                        f"{len(updated.entities)} 个实体、{len(updated.relations)} 条关系、"
                        f"{len(updated.properties)} 个属性"
                    ),
                    parent_artifact_ids=[candidate_ref.artifact_id, changeset_ref.artifact_id],
                    ontology_id=candidate_ref.ontology_id,
                    ontology_version=candidate_ref.ontology_version,
                )
            ],
            quality_signals={
                "graph_changeset_operation_count": float(len(changeset.operations)),
                "graph_entity_count": float(len(updated.entities)),
                "graph_relation_count": float(len(updated.relations)),
            },
        )

    async def _load_inputs(
        self, invocation: SkillInvocation
    ) -> tuple[Any, Any, CandidateGraphContent, ChangeSetContent]:
        """读取并严格解析本次调用唯一的候选图谱和修改集合。

        CandidateGraph 与 GraphChangeSet 都是既有注册输入，所以这里再次检查数量，防止
        多个同类型 Artifact 被静默选择而造成用户修改施加到错误版本。
        """

        candidates = [
            item for item in invocation.input_artifacts if item.artifact_type == "CandidateGraph"
        ]
        changesets = [
            item for item in invocation.input_artifacts if item.artifact_type == "GraphChangeSet"
        ]
        if len(candidates) != 1 or len(changesets) != 1:
            raise ValueError(
                "apply_graph_changeset 必须且只能接收一个 CandidateGraph 和一个 GraphChangeSet"
            )
        candidate_ref, changeset_ref = candidates[0], changesets[0]
        graph = CandidateGraphContent.model_validate(
            json.loads(await self._reader.read_content(candidate_ref.artifact_id))
        )
        changeset = ChangeSetContent.model_validate(
            json.loads(await self._reader.read_content(changeset_ref.artifact_id))
        )
        return candidate_ref, changeset_ref, graph, changeset

    @staticmethod
    def _validate_target(
        candidate_checksum: str, candidate_id: Any, changeset: ChangeSetContent
    ) -> None:
        """确认用户编辑的正是当前输入版本，避免跨版本误写。"""

        if changeset.target_artifact_id != candidate_id:
            raise ValueError("GraphChangeSet 的目标图谱不是本次输入 CandidateGraph")
        if changeset.target_checksum != candidate_checksum:
            raise ValueError("GraphChangeSet 的目标校验和与 CandidateGraph 当前版本不一致")

    @classmethod
    def _apply_all(
        cls, graph: CandidateGraphContent, operations: list[ChangeOperation]
    ) -> CandidateGraphContent:
        """在副本上按用户给定顺序应用所有操作，并在结束时验证血缘一致性。"""

        entities = {item.entity_id: item.model_copy(deep=True) for item in graph.entities}
        relations = {item.relation_id: item.model_copy(deep=True) for item in graph.relations}
        properties = {item.property_id: item.model_copy(deep=True) for item in graph.properties}
        seen_operation_ids: set[str] = set()
        for operation in operations:
            if operation.operation_id in seen_operation_ids:
                raise ValueError(f"ChangeOperation ID 重复：{operation.operation_id}")
            seen_operation_ids.add(operation.operation_id)
            cls._apply_operation(entities, relations, properties, operation)
        cls._validate_graph_references(entities, relations, properties)
        return CandidateGraphContent(
            schema_version="1.0.0",
            entities=list(entities.values()),
            relations=list(relations.values()),
            properties=list(properties.values()),
            evidence=[item.model_copy(deep=True) for item in graph.evidence],
        )

    @classmethod
    def _apply_operation(
        cls,
        entities: dict[str, EntityRecord],
        relations: dict[str, RelationRecord],
        properties: dict[str, PropertyRecord],
        operation: ChangeOperation,
    ) -> None:
        """分派单一操作；不支持的本体目标类型会被拒绝而非静默忽略。"""

        if operation.target_kind not in {"ENTITY", "RELATION", "PROPERTY"}:
            raise ValueError(f"GraphChangeSet 不支持目标类型：{operation.target_kind}")
        if operation.action == "ADD":
            cls._add(entities, relations, properties, operation)
        elif operation.action == "UPDATE":
            cls._update(entities, relations, properties, operation)
        elif operation.action == "DELETE":
            cls._delete(entities, relations, properties, operation)
        elif operation.action == "MERGE":
            cls._merge(entities, relations, properties, operation)
        elif operation.action == "SPLIT":
            cls._split(entities, relations, properties, operation)

    @staticmethod
    def _require_target(operation: ChangeOperation) -> str:
        """取得必填业务目标 ID；缺失时返回用户可理解的校验错误。"""

        if not operation.target_id:
            raise ValueError(f"{operation.action} {operation.target_kind} 缺少 target_id")
        return operation.target_id

    @classmethod
    def _add(
        cls,
        entities: dict[str, EntityRecord],
        relations: dict[str, RelationRecord],
        properties: dict[str, PropertyRecord],
        operation: ChangeOperation,
    ) -> None:
        """新增实体、关系或属性，并拒绝重复标识或不存在端点。"""

        target_id = cls._require_target(operation)
        value = {**operation.value}
        if operation.target_kind == "ENTITY":
            value.setdefault("entity_id", target_id)
            entity_record = EntityRecord.model_validate(value)
            if entity_record.entity_id != target_id or target_id in entities:
                raise ValueError(f"实体 ID 已存在或与 target_id 不一致：{target_id}")
            entities[target_id] = entity_record
        elif operation.target_kind == "RELATION":
            value.setdefault("relation_id", target_id)
            relation_record = RelationRecord.model_validate(value)
            if relation_record.relation_id != target_id or target_id in relations:
                raise ValueError(f"关系 ID 已存在或与 target_id 不一致：{target_id}")
            cls._validate_relation_endpoints(relation_record, entities)
            relations[target_id] = relation_record
        else:
            value.setdefault("property_id", target_id)
            property_record = PropertyRecord.model_validate(value)
            if property_record.property_id != target_id or target_id in properties:
                raise ValueError(f"属性 ID 已存在或与 target_id 不一致：{target_id}")
            cls._validate_property_owner(property_record, entities)
            properties[target_id] = property_record

    @classmethod
    def _update(
        cls,
        entities: dict[str, EntityRecord],
        relations: dict[str, RelationRecord],
        properties: dict[str, PropertyRecord],
        operation: ChangeOperation,
    ) -> None:
        """按显式 value 字段覆盖已有记录，标识字段不能借 UPDATE 偷换。"""

        target_id = cls._require_target(operation)
        value = dict(operation.value)
        if operation.target_kind == "ENTITY":
            original_entity = cls._get(entities, target_id, "实体")
            entity_record = EntityRecord.model_validate(
                {**original_entity.model_dump(), **value, "entity_id": target_id}
            )
            entities[target_id] = entity_record
        elif operation.target_kind == "RELATION":
            original_relation = cls._get(relations, target_id, "关系")
            relation_record = RelationRecord.model_validate(
                {**original_relation.model_dump(), **value, "relation_id": target_id}
            )
            cls._validate_relation_endpoints(relation_record, entities)
            relations[target_id] = relation_record
        else:
            original_property = cls._get(properties, target_id, "属性")
            property_record = PropertyRecord.model_validate(
                {**original_property.model_dump(), **value, "property_id": target_id}
            )
            cls._validate_property_owner(property_record, entities)
            properties[target_id] = property_record

    @classmethod
    def _delete(
        cls,
        entities: dict[str, EntityRecord],
        relations: dict[str, RelationRecord],
        properties: dict[str, PropertyRecord],
        operation: ChangeOperation,
    ) -> None:
        """删除记录；删除实体会显式级联删除其关系和属性，避免悬空图谱。"""

        target_id = cls._require_target(operation)
        if operation.target_kind == "ENTITY":
            cls._get(entities, target_id, "实体")
            del entities[target_id]
            for relation_id, relation in list(relations.items()):
                if target_id in {relation.source_entity_id, relation.target_entity_id}:
                    del relations[relation_id]
            for property_id, property_record in list(properties.items()):
                if property_record.entity_id == target_id:
                    del properties[property_id]
        elif operation.target_kind == "RELATION":
            cls._get(relations, target_id, "关系")
            del relations[target_id]
        else:
            cls._get(properties, target_id, "属性")
            del properties[target_id]

    @classmethod
    def _merge(
        cls,
        entities: dict[str, EntityRecord],
        relations: dict[str, RelationRecord],
        properties: dict[str, PropertyRecord],
        operation: ChangeOperation,
    ) -> None:
        """将 source_entity_ids 合并到 target_id，并重定向端点、属性和别名。"""

        if operation.target_kind != "ENTITY":
            raise ValueError("MERGE 仅支持 ENTITY")
        primary_id = cls._require_target(operation)
        primary = cls._get(entities, primary_id, "主实体")
        source_ids = operation.value.get("source_entity_ids")
        if not isinstance(source_ids, list) or not source_ids:
            raise ValueError("MERGE ENTITY 需要非空 source_entity_ids")
        if primary_id in source_ids or len(source_ids) != len(set(source_ids)):
            raise ValueError("合并源实体不能包含主实体，且不能重复")
        sources = [cls._get(entities, source_id, "待合并实体") for source_id in source_ids]
        aliases = [*primary.aliases]
        for source in sources:
            aliases.extend([source.name, *source.aliases])
        entities[primary_id] = primary.model_copy(
            update={
                "aliases": list(dict.fromkeys(alias for alias in aliases if alias != primary.name))
            }
        )
        source_set = set(source_ids)
        for relation_id, relation in list(relations.items()):
            relations[relation_id] = relation.model_copy(
                update={
                    "source_entity_id": primary_id
                    if relation.source_entity_id in source_set
                    else relation.source_entity_id,
                    "target_entity_id": primary_id
                    if relation.target_entity_id in source_set
                    else relation.target_entity_id,
                }
            )
        for property_id, property_record in list(properties.items()):
            if property_record.entity_id in source_set:
                properties[property_id] = property_record.model_copy(
                    update={"entity_id": primary_id}
                )
        for source_id in source_ids:
            del entities[source_id]

    @classmethod
    def _split(
        cls,
        entities: dict[str, EntityRecord],
        relations: dict[str, RelationRecord],
        properties: dict[str, PropertyRecord],
        operation: ChangeOperation,
    ) -> None:
        """以多个新实体替换一个实体，默认将原连接迁移至第一个新实体。

        该默认值与页面工作台一致，便于用户在同一轮继续编辑关系；复杂的关系重分配可
        通过后续 ``UPDATE RELATION`` 或删除后新增关系明确表达。
        """

        if operation.target_kind != "ENTITY":
            raise ValueError("SPLIT 仅支持 ENTITY")
        source_id = cls._require_target(operation)
        cls._get(entities, source_id, "待拆分实体")
        payload = operation.value.get("entities")
        if not isinstance(payload, list) or len(payload) < 2:
            raise ValueError("SPLIT ENTITY 至少需要两个替代实体")
        replacements = [EntityRecord.model_validate(item) for item in payload]
        replacement_ids = [item.entity_id for item in replacements]
        if len(replacement_ids) != len(set(replacement_ids)) or source_id in replacement_ids:
            raise ValueError("拆分后的实体 ID 不能重复或复用原实体 ID")
        collisions = set(replacement_ids) & (set(entities) - {source_id})
        if collisions:
            raise ValueError(f"拆分后的实体 ID 已存在：{sorted(collisions)}")
        first_id = replacement_ids[0]
        del entities[source_id]
        entities.update({item.entity_id: item for item in replacements})
        for relation_id, relation in list(relations.items()):
            relations[relation_id] = relation.model_copy(
                update={
                    "source_entity_id": first_id
                    if relation.source_entity_id == source_id
                    else relation.source_entity_id,
                    "target_entity_id": first_id
                    if relation.target_entity_id == source_id
                    else relation.target_entity_id,
                }
            )
        for property_id, property_record in list(properties.items()):
            if property_record.entity_id == source_id:
                properties[property_id] = property_record.model_copy(update={"entity_id": first_id})

    @staticmethod
    def _get(records: dict[str, Any], target_id: str, label: str) -> Any:
        """读取指定记录，缺失时给出稳定业务错误。"""

        if target_id not in records:
            raise ValueError(f"{label}不存在：{target_id}")
        return records[target_id]

    @staticmethod
    def _validate_relation_endpoints(
        relation: RelationRecord, entities: dict[str, EntityRecord]
    ) -> None:
        """拒绝引用不存在实体的关系。"""

        if relation.source_entity_id not in entities or relation.target_entity_id not in entities:
            raise ValueError(f"关系 {relation.relation_id} 存在不存在的实体端点")

    @staticmethod
    def _validate_property_owner(
        property_record: PropertyRecord, entities: dict[str, EntityRecord]
    ) -> None:
        """拒绝挂载到不存在实体的属性。"""

        if property_record.entity_id not in entities:
            raise ValueError(f"属性 {property_record.property_id} 指向不存在实体")

    @classmethod
    def _validate_graph_references(
        cls,
        entities: dict[str, EntityRecord],
        relations: dict[str, RelationRecord],
        properties: dict[str, PropertyRecord],
    ) -> None:
        """在整批操作结束后再次验证全图引用，作为原子提交前的最终防线。"""

        for relation in relations.values():
            cls._validate_relation_endpoints(relation, entities)
        for property_record in properties.values():
            cls._validate_property_owner(property_record, entities)

    def _failed(
        self,
        invocation: SkillInvocation,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> SkillRunResult:
        """将任一校验或读取错误标准化为无输出的 Skill 失败结果。"""

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.RETRYABLE_FAILED if retryable else AgentRunStatus.FAILED,
            error_code=code,
            error_message=message,
            retryable=retryable,
        )
