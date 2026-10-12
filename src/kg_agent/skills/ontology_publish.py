"""把已批准候选本体确定性提升为 PublishedOntology。

本模块只负责发布阶段的不可变内容复制，不参与本体候选生成、对齐、融合或审核。调用前的
PublishGuard 与调用后的 ArtifactService 共同保证候选版本、Approval 和发布产物严格绑定。
"""

from __future__ import annotations

import json
from uuid import UUID

from pydantic import ValidationError

from kg_agent.domain.artifact_content_models import (
    ApprovalContent,
    CandidateOntologyContent,
    FusedCandidateOntologyContent,
    PublishedOntologyContent,
)
from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillArtifactOutput, SkillInvocation, SkillRunResult
from kg_agent.skills.llm import ArtifactContentReader


class OntologyPublishSkill:
    """原样发布一个获得当前 OntologyApproval 授权的候选本体。"""

    def __init__(
        self, registration: SkillRegistration, *, artifact_reader: ArtifactContentReader
    ) -> None:
        """绑定固定的发布输入输出契约和只读 Artifact 内容读取器。"""

        if registration.name != "publish_ontology":
            raise ValueError("本体发布实现只能绑定 publish_ontology")
        if registration.input_artifact_types != ["CandidateOntology", "OntologyApproval"]:
            raise ValueError("publish_ontology 输入契约必须是 CandidateOntology、OntologyApproval")
        if registration.output_artifact_types != ["PublishedOntology"]:
            raise ValueError("publish_ontology 输出契约必须是 PublishedOntology")
        self._registration = registration
        self._reader = artifact_reader

    @property
    def name(self) -> str:
        """返回 Registry 中稳定的 Skill 名称。"""

        return self._registration.name

    async def execute(self, invocation: SkillInvocation) -> SkillRunResult:
        """校验候选本体与批准绑定后，原样创建带用户名称的发布版本。"""

        try:
            candidates = [
                item
                for item in invocation.input_artifacts
                if item.artifact_type in {"CandidateOntology", "FusedCandidateOntology"}
            ]
            approvals = [
                item
                for item in invocation.input_artifacts
                if item.artifact_type == "OntologyApproval"
            ]
            if len(candidates) != 1 or len(approvals) != 1:
                raise ValueError("publish_ontology 必须且只能接收一个候选或融合本体和一个批准记录")
            candidate_ref, approval_ref = candidates[0], approvals[0]
            raw_candidate = json.loads(await self._reader.read_content(candidate_ref.artifact_id))
            candidate = (
                FusedCandidateOntologyContent.model_validate(raw_candidate)
                if candidate_ref.artifact_type == "FusedCandidateOntology"
                else CandidateOntologyContent.model_validate(raw_candidate)
            )
            approval = ApprovalContent.model_validate(
                json.loads(await self._reader.read_content(approval_ref.artifact_id))
            )
            if approval.decision != "APPROVED":
                raise ValueError("OntologyApproval 没有批准候选本体")
            if (
                approval.target_artifact_id != candidate_ref.artifact_id
                or approval.target_checksum != candidate_ref.checksum
            ):
                raise ValueError("OntologyApproval 未绑定当前候选本体版本")
            publication_title = approval.publication_title.strip()
            if not publication_title:
                raise ValueError("OntologyApproval 缺少用户填写的本体发布名称")
            ontology_version = str(invocation.parameters.get("ontology_version", "1.0.0")).strip()
            if not ontology_version:
                raise ValueError("ontology_version 不能为空")
            ontology_id = UUID(
                str(invocation.parameters.get("ontology_id", invocation.invocation_id))
            )
            # 融合过程说明保留在已审核候选产物中，发布内容只复制公共本体事实。
            published = PublishedOntologyContent(
                **candidate.model_dump(exclude={"alignment_notes"}),
                publication_title=publication_title,
                ontology_id=ontology_id,
                ontology_version=ontology_version,
                approval_artifact_id=approval_ref.artifact_id,
            )
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
            return self._failed(
                invocation,
                "ONTOLOGY_PUBLISH_FAILED",
                f"本体发布失败：{type(exc).__name__}: {exc}",
            )

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.SUCCESS,
            outputs=[
                SkillArtifactOutput(
                    artifact_type="PublishedOntology",
                    content=published.model_dump(mode="json"),
                    content_summary=(
                        f"已发布本体“{published.publication_title}” v{published.ontology_version}："
                        f"{len(published.classes)} 个类、{len(published.relations)} 种关系、"
                        f"{len(published.properties)} 个属性"
                    ),
                    parent_artifact_ids=[candidate_ref.artifact_id, approval_ref.artifact_id],
                    ontology_id=published.ontology_id,
                    ontology_version=published.ontology_version,
                    publication_title=published.publication_title,
                )
            ],
            quality_signals={
                "published_ontology_class_count": float(len(published.classes)),
                "published_ontology_relation_count": float(len(published.relations)),
                "published_ontology_property_count": float(len(published.properties)),
            },
        )

    def _failed(self, invocation: SkillInvocation, code: str, message: str) -> SkillRunResult:
        """把发布前置条件失败转为不可重试的统一 Skill 结果。"""

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.FAILED,
            error_code=code,
            error_message=message,
            retryable=False,
        )
