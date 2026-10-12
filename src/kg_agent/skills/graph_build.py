"""实现基于已抽取事实的候选图谱确定性组装 Skill。

该 Skill 不重新抽取或编造实体关系：它只读取上游数据集、完成去重与端点校验，并输出
既有 ``CandidateGraph`` 契约。它是正式确定性后处理，保持 Agent 选择、Registry 权限、
Artifact 输入输出和人工审核门禁不变。
"""

from __future__ import annotations

import json
from typing import Any

from kg_agent.domain.artifact_content_models import (
    CandidateGraphContent,
    ConflictReportContent,
    EntityDatasetContent,
    EntityRecord,
    EvidenceDatasetContent,
    EvidenceRecord,
    PropertyDatasetContent,
    PropertyRecord,
    RelationDatasetContent,
    RelationRecord,
)
from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillArtifactOutput, SkillInvocation, SkillRunResult
from kg_agent.skills.llm import ArtifactContentReader


class GraphBuildSkill:
    """将实体、关系及可选属性证据确定性汇聚成不可变 CandidateGraph。"""

    def __init__(
        self, registration: SkillRegistration, *, artifact_reader: ArtifactContentReader
    ) -> None:
        """绑定既有 ``build_candidate_graph`` 注册契约与 Artifact 内容读取器。"""

        if registration.name != "build_candidate_graph":
            raise ValueError("图谱构建实现只能绑定 build_candidate_graph")
        if registration.output_artifact_types != ["CandidateGraph"]:
            raise ValueError("build_candidate_graph 输出契约必须是 CandidateGraph")
        self._registration = registration
        self._reader = artifact_reader

    @property
    def name(self) -> str:
        """返回稳定的 Registry Skill 名称。"""

        return self._registration.name

    async def execute(self, invocation: SkillInvocation) -> SkillRunResult:
        """读取上游数据集并构建候选图谱；无效血缘以标准失败结果返回。"""

        try:
            payloads = await self._load_payloads(invocation)
            graph, conflict_count = self._assemble(payloads)
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            return self._failed(
                invocation,
                "CANDIDATE_GRAPH_BUILD_FAILED",
                f"候选图谱构建失败：{type(exc).__name__}: {exc}",
            )

        parent_ids = [item.artifact_id for item in invocation.input_artifacts]
        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.SUCCESS,
            outputs=[
                SkillArtifactOutput(
                    artifact_type="CandidateGraph",
                    content=graph.model_dump(mode="json"),
                    content_summary=(
                        "由上游数据集构建候选图谱："
                        f"{len(graph.entities)} 个实体、{len(graph.relations)} 条关系、"
                        f"{len(graph.properties)} 个属性"
                    ),
                    parent_artifact_ids=parent_ids,
                    ontology_id=next(
                        (
                            item.ontology_id
                            for item in invocation.input_artifacts
                            if item.ontology_id
                        ),
                        None,
                    ),
                    ontology_version=next(
                        (
                            item.ontology_version
                            for item in invocation.input_artifacts
                            if item.ontology_version
                        ),
                        None,
                    ),
                )
            ],
            quality_signals={
                "graph_entity_count": float(len(graph.entities)),
                "graph_relation_count": float(len(graph.relations)),
                "graph_conflict_count": float(conflict_count),
                "graph_assembly_quality": 1.0,
            },
        )

    async def _load_payloads(self, invocation: SkillInvocation) -> dict[str, list[dict[str, Any]]]:
        """按 Artifact 类型读取已校验 JSON，保留可选属性和证据输入。"""

        payloads: dict[str, list[dict[str, Any]]] = {}
        for artifact in invocation.input_artifacts:
            if artifact.artifact_type not in {
                "EntityDataset",
                "RelationDataset",
                "PropertyDataset",
                "EvidenceDataset",
                "ConflictReport",
            }:
                continue
            payloads.setdefault(artifact.artifact_type, []).append(
                json.loads(await self._reader.read_content(artifact.artifact_id))
            )
        for required in self._registration.input_artifact_types:
            if not payloads.get(required):
                raise ValueError(f"缺少必需输入 {required}")
        return payloads

    @staticmethod
    def _assemble(payloads: dict[str, list[dict[str, Any]]]) -> tuple[CandidateGraphContent, int]:
        """汇聚同类型数据集、拒绝冲突重复 ID 与悬空边，返回标准图谱内容。"""

        entities: dict[str, EntityRecord] = {}
        relations: dict[str, RelationRecord] = {}
        properties: dict[str, PropertyRecord] = {}
        evidence: dict[str, EvidenceRecord] = {}
        for payload in payloads.get("EntityDataset", []):
            for entity_record in EntityDatasetContent.model_validate(payload).entities:
                GraphBuildSkill._insert_unique(
                    entities, entity_record.entity_id, entity_record, "实体"
                )
        for payload in payloads.get("RelationDataset", []):
            for relation_record in RelationDatasetContent.model_validate(payload).relations:
                GraphBuildSkill._insert_unique(
                    relations, relation_record.relation_id, relation_record, "关系"
                )
        for payload in payloads.get("PropertyDataset", []):
            for property_record in PropertyDatasetContent.model_validate(payload).properties:
                GraphBuildSkill._insert_unique(
                    properties, property_record.property_id, property_record, "属性"
                )
        for payload in payloads.get("EvidenceDataset", []):
            for evidence_record in EvidenceDatasetContent.model_validate(payload).evidence:
                GraphBuildSkill._insert_unique(
                    evidence, evidence_record.evidence_id, evidence_record, "证据"
                )
        conflict_count = sum(
            len(ConflictReportContent.model_validate(payload).conflicts)
            for payload in payloads.get("ConflictReport", [])
        )
        for relation in relations.values():
            missing = [
                value
                for value in (relation.source_entity_id, relation.target_entity_id)
                if value not in entities
            ]
            if missing:
                raise ValueError(f"关系 {relation.relation_id} 存在悬空端点：{missing}")
        for property_record in properties.values():
            if property_record.entity_id not in entities:
                raise ValueError(f"属性 {property_record.property_id} 指向不存在实体")
        return (
            CandidateGraphContent(
                schema_version="1.0.0",
                entities=list(entities.values()),
                relations=list(relations.values()),
                properties=list(properties.values()),
                evidence=list(evidence.values()),
            ),
            conflict_count,
        )

    @staticmethod
    def _insert_unique(target: dict[str, Any], key: str, value: Any, label: str) -> None:
        """允许幂等重复，拒绝相同 ID 对应不同业务内容。"""

        existing = target.get(key)
        if existing is None:
            target[key] = value
        elif existing.model_dump(mode="json") != value.model_dump(mode="json"):
            raise ValueError(f"{label} ID 重复且内容冲突：{key}")

    def _failed(
        self,
        invocation: SkillInvocation,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> SkillRunResult:
        """把构建失败映射为统一 SkillRunResult，不创建半成品图谱。"""

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.RETRYABLE_FAILED if retryable else AgentRunStatus.FAILED,
            error_code=code,
            error_message=message,
            retryable=retryable,
        )
