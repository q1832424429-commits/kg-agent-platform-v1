"""定义本体和候选图谱人工审核请求契约。

Human Review 是确定性业务门禁而不是 Agent；审核状态由确定性 HumanReviewService 管理。
任何 Agent 都不能自行批准。
"""

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field, model_validator

from kg_agent.domain.enums import HumanReviewStatus, ReviewOption, ReviewTargetType


class HumanReviewRequest(BaseModel):
    """要求用户审核候选本体或候选图谱的持久化门禁请求。"""

    review_id: UUID
    task_id: UUID
    subtask_id: UUID | None = None
    target_type: ReviewTargetType
    target_artifact_id: UUID
    target_artifact_checksum: str
    quality_report_artifact_ids: list[UUID] = Field(default_factory=list)
    conflict_report_artifact_ids: list[UUID] = Field(default_factory=list)
    evidence_artifact_ids: list[UUID] = Field(default_factory=list)
    allowed_actions: list[ReviewOption]
    status: HumanReviewStatus
    requested_at: datetime
    resolved_at: datetime | None = None
    decision_artifact_id: UUID | None = None
    reviewer_feedback: str | None = None

    @model_validator(mode="after")
    def validate_review_state(self) -> "HumanReviewRequest":
        """校验审核操作集合和终态字段保持一致。"""

        if not self.allowed_actions or len(self.allowed_actions) != len(set(self.allowed_actions)):
            raise ValueError("allowed_actions 必须非空且不能重复")
        terminal = self.status is not HumanReviewStatus.PENDING
        if terminal != (self.resolved_at is not None):
            raise ValueError("审核终态与 resolved_at 必须同时出现")
        if (
            self.status
            in {
                HumanReviewStatus.APPROVED,
                HumanReviewStatus.CHANGES_REQUESTED,
            }
            and self.decision_artifact_id is None
        ):
            raise ValueError("批准或提交修改必须关联 decision_artifact_id")
        if self.status is HumanReviewStatus.PENDING and self.decision_artifact_id is not None:
            raise ValueError("待审核请求不能预先关联决策 Artifact")
        return self
