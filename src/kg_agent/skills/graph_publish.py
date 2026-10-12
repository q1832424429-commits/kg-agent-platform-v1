"""把已批准候选图谱确定性提升为 PublishedGraph。

本模块实现既有 ``publish_graph`` Skill，不重新抽取、改写或补造知识。PublishGuard 仍在
Registry 调用本 Skill 前校验人工 Approval；本实现再次校验内容绑定并原样保留候选图谱。
"""

from __future__ import annotations

import json
from uuid import UUID

from pydantic import ValidationError

from kg_agent.domain.artifact_content_models import (
    ApprovalContent,
    CandidateGraphContent,
    PublishedGraphContent,
)
from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillArtifactOutput, SkillInvocation, SkillRunResult
from kg_agent.skills.llm import ArtifactContentReader


class GraphPublishSkill:
    """原样发布一个经过当前版本 Approval 授权的 CandidateGraph。"""

    def __init__(
        self, registration: SkillRegistration, *, artifact_reader: ArtifactContentReader
    ) -> None:
        """绑定既有 publish_graph 注册信息和只读 Artifact 内容服务。"""

        if registration.name != "publish_graph":
            raise ValueError("图谱发布实现只能绑定 publish_graph")
        if registration.input_artifact_types != ["CandidateGraph", "GraphApproval"]:
            raise ValueError("publish_graph 输入契约必须是 CandidateGraph、GraphApproval")
        if registration.output_artifact_types != ["PublishedGraph"]:
            raise ValueError("publish_graph 输出契约必须是 PublishedGraph")
        self._registration = registration
        self._reader = artifact_reader

    @property
    def name(self) -> str:
        """返回稳定的 Registry Skill 名称。"""

        return self._registration.name

    async def execute(self, invocation: SkillInvocation) -> SkillRunResult:
        """读取候选图谱与批准记录，并生成内容完全一致的发布版本。"""

        try:
            candidates = [
                item
                for item in invocation.input_artifacts
                if item.artifact_type == "CandidateGraph"
            ]
            approvals = [
                item for item in invocation.input_artifacts if item.artifact_type == "GraphApproval"
            ]
            if len(candidates) != 1 or len(approvals) != 1:
                raise ValueError("publish_graph 必须且只能接收一个候选图谱和一个批准记录")
            candidate_ref, approval_ref = candidates[0], approvals[0]
            candidate = CandidateGraphContent.model_validate(
                json.loads(await self._reader.read_content(candidate_ref.artifact_id))
            )
            approval = ApprovalContent.model_validate(
                json.loads(await self._reader.read_content(approval_ref.artifact_id))
            )
            if approval.decision != "APPROVED":
                raise ValueError("GraphApproval 没有批准候选图谱")
            if (
                approval.target_artifact_id != candidate_ref.artifact_id
                or approval.target_checksum != candidate_ref.checksum
            ):
                raise ValueError("GraphApproval 未绑定当前 CandidateGraph 版本")
            publication_title = approval.publication_title.strip()
            if not publication_title:
                raise ValueError("GraphApproval 缺少用户填写的图谱发布名称")
            graph_version = str(invocation.parameters.get("graph_version", "1.0.0")).strip()
            if not graph_version:
                raise ValueError("graph_version 不能为空")
            graph_id_value = invocation.parameters.get("graph_id", invocation.invocation_id)
            graph_id = UUID(str(graph_id_value))
            published = PublishedGraphContent(
                **candidate.model_dump(),
                publication_title=publication_title,
                graph_id=graph_id,
                graph_version=graph_version,
                approval_artifact_id=approval_ref.artifact_id,
            )
        except (json.JSONDecodeError, TypeError, ValueError, ValidationError) as exc:
            return self._failed(
                invocation,
                "GRAPH_PUBLISH_FAILED",
                f"图谱发布失败：{type(exc).__name__}: {exc}",
            )

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.SUCCESS,
            outputs=[
                SkillArtifactOutput(
                    artifact_type="PublishedGraph",
                    content=published.model_dump(mode="json"),
                    content_summary=(
                        f"已发布图谱“{published.publication_title}” v{published.graph_version}："
                        f"{len(published.entities)} 个实体、{len(published.relations)} 条关系"
                    ),
                    parent_artifact_ids=[candidate_ref.artifact_id, approval_ref.artifact_id],
                    ontology_id=candidate_ref.ontology_id,
                    ontology_version=candidate_ref.ontology_version,
                    publication_title=published.publication_title,
                )
            ],
            quality_signals={
                "published_graph_entity_count": float(len(published.entities)),
                "published_graph_relation_count": float(len(published.relations)),
                "published_graph_postgres_projection_required": 1.0,
            },
        )

    def _failed(
        self,
        invocation: SkillInvocation,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> SkillRunResult:
        """把发布校验错误映射为无输出的统一 Skill 失败结果。"""

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.RETRYABLE_FAILED if retryable else AgentRunStatus.FAILED,
            error_code=code,
            error_message=message,
            retryable=retryable,
        )
