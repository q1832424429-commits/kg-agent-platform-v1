"""定义第六阶段人工审核应用服务的输入命令。

这些命令是服务入口参数，不改变 HumanReviewRequest 等已冻结公共业务契约。
"""

from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from kg_agent.domain.artifact_content_models import ChangeOperation
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.enums import ReviewOption, ReviewTargetType
from kg_agent.domain.review_models import HumanReviewRequest


class CreateHumanReviewCommand(BaseModel):
    """描述针对候选本体或候选图谱创建人工审核门禁的请求。"""

    review_id: UUID = Field(default_factory=uuid4)
    task_id: UUID
    subtask_id: UUID | None = None
    target_type: ReviewTargetType
    target_artifact_id: UUID
    quality_report_artifact_ids: list[UUID] = Field(default_factory=list)
    conflict_report_artifact_ids: list[UUID] = Field(default_factory=list)
    evidence_artifact_ids: list[UUID] = Field(default_factory=list)
    allowed_actions: list[ReviewOption] = Field(
        default_factory=lambda: [
            ReviewOption.APPROVE,
            ReviewOption.REJECT,
            ReviewOption.SUBMIT_CHANGES,
        ],
        min_length=1,
    )

    @model_validator(mode="after")
    def validate_unique_references(self) -> "CreateHumanReviewCommand":
        """拒绝重复附件和重复操作，保持审核输入无歧义。"""

        groups = (
            self.quality_report_artifact_ids,
            self.conflict_report_artifact_ids,
            self.evidence_artifact_ids,
            self.allowed_actions,
        )
        if any(len(items) != len(set(items)) for items in groups):
            raise ValueError("审核附件和 allowed_actions 不能包含重复项")
        return self


class ResolveHumanReviewCommand(BaseModel):
    """描述用户批准、拒绝或提交结构化修改的一次审核操作。"""

    review_id: UUID
    action: ReviewOption
    reviewer_id: str = "default-user"
    reviewer_feedback: str | None = None
    # 本体或图谱得到批准时由用户输入的资产名称；领域服务会在实际读取审核目标后
    # 判断是否必填，避免命令层复制 ReviewTargetType 这一受控事实。
    publication_title: str | None = Field(default=None, max_length=120)
    operations: list[ChangeOperation] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_action_payload(self) -> "ResolveHumanReviewCommand":
        """确保修改必须携带操作，其他操作不能夹带 ChangeSet 内容。"""

        if self.action is ReviewOption.SUBMIT_CHANGES and not self.operations:
            raise ValueError("SUBMIT_CHANGES 必须至少包含一个修改操作")
        if self.action is not ReviewOption.SUBMIT_CHANGES and self.operations:
            raise ValueError("只有 SUBMIT_CHANGES 可以携带修改操作")
        if self.action is ReviewOption.REJECT and not self.reviewer_feedback:
            raise ValueError("拒绝审核时必须提供 reviewer_feedback")
        if self.publication_title is not None and not self.publication_title.strip():
            raise ValueError("发布名称不能为空白")
        if self.action is not ReviewOption.APPROVE and self.publication_title is not None:
            raise ValueError("只有 APPROVE 可以携带发布名称")
        return self


class HumanReviewMaterials(BaseModel):
    """返回审核页面所需候选结果及质量、冲突、证据 Artifact 引用。"""

    review: HumanReviewRequest
    target: ArtifactRef
    quality_reports: list[ArtifactRef] = Field(default_factory=list)
    conflict_reports: list[ArtifactRef] = Field(default_factory=list)
    evidence_artifacts: list[ArtifactRef] = Field(default_factory=list)
