"""实现历史 Artifact 是否可复用的确定性判定。

LLM 可以建议候选 Artifact，但类型、Schema、revision、本体、状态和父输入 checksum
必须全部由本服务校验。
"""

from kg_agent.domain.artifact_models import ArtifactRef, ArtifactReuseDecision, ArtifactReuseRequest
from kg_agent.domain.enums import ArtifactStatus
from kg_agent.repositories.interfaces import ArtifactRepository


class ArtifactReuseService:
    """根据明确兼容条件判断一个既有 Artifact 是否可复用。"""

    def __init__(self, repository: ArtifactRepository) -> None:
        """绑定只读 Artifact Repository。"""

        self._repository = repository

    async def evaluate(self, request: ArtifactReuseRequest) -> ArtifactReuseDecision:
        """返回全部不兼容原因；所有条件满足时才允许复用。"""

        record = await self._repository.get(request.artifact_id)
        parents = await self._repository.get_parent_refs(request.artifact_id) if record else []
        return self.evaluate_snapshot(request, record.ref if record else None, parents)

    @staticmethod
    def evaluate_snapshot(
        request: ArtifactReuseRequest,
        ref: ArtifactRef | None,
        parent_refs: list[ArtifactRef],
    ) -> ArtifactReuseDecision:
        """使用同一规则校验只读快照，供无数据库的单元测试适配器使用。

        生产装配必须使用 evaluate 读取正式记录；快照入口不代表对象内容已经持久化。
        """

        reasons: list[str] = []
        if ref is None:
            return ArtifactReuseDecision(
                artifact_id=request.artifact_id,
                reusable=False,
                reason_codes=["ARTIFACT_NOT_FOUND"],
            )
        if ref.status is not ArtifactStatus.AVAILABLE:
            reasons.append("ARTIFACT_NOT_AVAILABLE")
        if ref.artifact_type != request.expected_artifact_type:
            reasons.append("ARTIFACT_TYPE_MISMATCH")
        if ref.schema_version != request.expected_schema_version:
            reasons.append("SCHEMA_VERSION_MISMATCH")
        if ref.task_spec_revision not in request.compatible_task_spec_revisions:
            reasons.append("TASK_SPEC_REVISION_INCOMPATIBLE")
        if ref.ontology_id != request.expected_ontology_id:
            reasons.append("ONTOLOGY_ID_MISMATCH")
        if ref.ontology_version != request.expected_ontology_version:
            reasons.append("ONTOLOGY_VERSION_MISMATCH")

        if {parent.artifact_id for parent in parent_refs} != set(ref.parent_artifact_ids):
            reasons.append("PARENT_LINEAGE_MISMATCH")
        cross_task_parent = any(parent.task_id != ref.task_id for parent in parent_refs)
        allows_cross_task_import = ArtifactReuseService._allows_cross_task_import(ref, parent_refs)
        if cross_task_parent and not allows_cross_task_import:
            reasons.append("PARENT_TASK_MISMATCH")
        if any(parent.status is not ArtifactStatus.AVAILABLE for parent in parent_refs):
            reasons.append("PARENT_NOT_AVAILABLE")
        actual_parent_checksums = {parent.checksum for parent in parent_refs}
        if actual_parent_checksums != request.expected_parent_checksums:
            reasons.append("PARENT_CHECKSUM_MISMATCH")

        return ArtifactReuseDecision(
            artifact_id=request.artifact_id,
            reusable=not reasons,
            reason_codes=reasons,
        )

    @staticmethod
    def _allows_cross_task_import(ref: ArtifactRef, parents: list[ArtifactRef]) -> bool:
        """只允许受控的已发布资产导入拥有跨任务直接父级。

        普通中间产物跨任务复用仍必须通过当前任务中的受控导入副本，不能直接
        建立跨 MasterTask 血缘。这里与 ArtifactService 的写入门禁保持同一边界：
        已发布本体可作为 PublishedOntology 导入，已发布图谱可作为候选图谱基线导入。
        """

        if ref.producer_agent != "system":
            return False
        if ref.producer_skill == "published_asset_import":
            return ref.artifact_type == "PublishedOntology" and all(
                parent.artifact_type == "PublishedOntology" for parent in parents
            )
        if ref.producer_skill == "published_graph_baseline_import":
            return ref.artifact_type == "CandidateGraph" and all(
                parent.artifact_type == "PublishedGraph" for parent in parents
            )
        return False
