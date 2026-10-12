"""实现发布类 Skill 执行前的确定性 Approval 门禁。

PublishGuard 只验证角色、候选 Artifact、Approval 内容与 HumanReviewRequest 的绑定，
不调用 LLM、不执行 Skill，也不创建 Published Artifact。
"""

import hashlib
import json

from kg_agent.domain.artifact_content_models import ApprovalContent
from kg_agent.domain.enums import ArtifactStatus, HumanReviewStatus
from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.exceptions import PublishGuardError
from kg_agent.infrastructure.object_storage.base import ObjectStorage
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.repositories.interfaces import ArtifactRepository, HumanReviewRepository


class PublishGuard:
    """在发布 Skill 执行前验证当前候选版本具有真实用户批准。"""

    def __init__(
        self,
        *,
        artifacts: ArtifactRepository,
        reviews: HumanReviewRepository,
        agents: AgentRegistry,
        storage: ObjectStorage,
    ) -> None:
        """注入 Artifact、审核、Agent Registry 和受控对象存储。"""

        self._artifacts = artifacts
        self._reviews = reviews
        self._agents = agents
        self._storage = storage

    async def authorize(self, invocation: SkillInvocation) -> None:
        """校验发布调用，任何不匹配均抛出 PublishGuardError。

        校验包括图谱管理 Agent 权限、Skill 与目标类型、Artifact 任务隔离和状态、
        Approval 内容 checksum、审核终态，以及 Approval 到候选结果的直接血缘。
        """

        specifications = {
            "publish_ontology": {
                "candidate_types": {"CandidateOntology", "FusedCandidateOntology"},
                "approval_type": "OntologyApproval",
                "capability": "ontology",
            },
            "publish_graph": {
                "candidate_types": {"CandidateGraph"},
                "approval_type": "GraphApproval",
                "capability": "graph",
            },
        }
        specification = specifications.get(invocation.skill_name)
        if specification is None:
            raise PublishGuardError("PublishGuard 只授权已登记的发布 Skill")
        if invocation.agent_type != "graph_management_agent":
            raise PublishGuardError("只有图谱管理更新 Agent 可以执行发布 Skill")
        registration = self._agents.require(invocation.agent_type)
        capability = specification["capability"]
        if (
            capability == "ontology"
            and not registration.can_publish_ontology
            or capability == "graph"
            and not registration.can_publish_graph
        ):
            raise PublishGuardError("当前 Agent Registry 没有对应发布权限")

        candidates = [
            item
            for item in invocation.input_artifacts
            if item.artifact_type in specification["candidate_types"]
        ]
        approvals = [
            item
            for item in invocation.input_artifacts
            if item.artifact_type == specification["approval_type"]
        ]
        if len(candidates) != 1 or len(approvals) != 1:
            raise PublishGuardError("发布必须且只能提供一个候选结果和一个对应 Approval")
        candidate_ref, approval_ref = candidates[0], approvals[0]
        if candidate_ref.task_id != approval_ref.task_id:
            raise PublishGuardError("候选结果与 Approval 不能跨 MasterTask")
        if (
            candidate_ref.status is not ArtifactStatus.AVAILABLE
            or approval_ref.status is not ArtifactStatus.AVAILABLE
        ):
            raise PublishGuardError("发布输入 Artifact 必须为 AVAILABLE")

        candidate = await self._artifacts.get(candidate_ref.artifact_id)
        approval = await self._artifacts.get(approval_ref.artifact_id)
        if candidate is None or approval is None:
            raise PublishGuardError("发布输入 Artifact 不存在")
        if candidate.ref != candidate_ref or approval.ref != approval_ref:
            raise PublishGuardError("发布输入与持久化 Artifact 元数据不一致")
        if candidate.ref.artifact_id not in approval.ref.parent_artifact_ids:
            raise PublishGuardError("Approval 血缘未直接包含当前候选 Artifact")

        raw = await self._storage.get_bytes(approval.object_key)
        checksum = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if checksum != approval.ref.checksum:
            raise PublishGuardError("Approval 内容 checksum 校验失败")
        try:
            content = ApprovalContent.model_validate(json.loads(raw))
        except (json.JSONDecodeError, ValueError) as exc:
            raise PublishGuardError("Approval 内容不符合冻结契约") from exc
        if (
            content.decision != "APPROVED"
            or content.target_artifact_id != candidate.ref.artifact_id
            or content.target_checksum != candidate.ref.checksum
        ):
            raise PublishGuardError("Approval 未批准当前候选 Artifact 版本")

        review = await self._reviews.get_by_decision_artifact(approval.ref.artifact_id)
        if (
            review is None
            or review.status is not HumanReviewStatus.APPROVED
            or review.target_artifact_id != candidate.ref.artifact_id
            or review.target_artifact_checksum != candidate.ref.checksum
        ):
            raise PublishGuardError("Approval 没有关联有效的已批准 HumanReviewRequest")
