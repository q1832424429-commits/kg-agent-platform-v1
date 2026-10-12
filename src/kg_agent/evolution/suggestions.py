"""根据评测失败生成可审计、版本化且不会自动应用的优化建议。"""

from datetime import UTC, datetime
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from kg_agent.evaluation.metrics import EvaluationReport


class OptimizationSuggestion(BaseModel):
    """表示等待人工评审的 Prompt、配置或策略优化建议。"""

    suggestion_id: UUID = Field(default_factory=uuid4)
    version: int = Field(ge=1)
    category: Literal["PROMPT", "AGENT_CONFIG", "ROUTING_POLICY", "DATASET"]
    summary: str
    recommended_change: str
    source_case_ids: list[str] = Field(default_factory=list)
    status: Literal["PROPOSED"] = "PROPOSED"
    generated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))


def generate_suggestions(
    report: EvaluationReport, *, version: int = 1
) -> list[OptimizationSuggestion]:
    """确定性生成离线建议；绝不写入 Prompt、Registry 或生产配置。"""
    suggestions: list[OptimizationSuggestion] = []
    failures = report.failed_case_ids
    if report.task_intent_macro_f1 < 0.95:
        suggestions.append(
            OptimizationSuggestion(
                version=version,
                category="PROMPT",
                summary="意图识别低于门槛",
                recommended_change="复核 START、CONTINUE、MODIFY 的边界示例并提交人工评审。",
                source_case_ids=failures,
            )
        )
    if report.agent_assignment_accuracy < 0.95 or report.necessary_subtask_recall < 0.95:
        suggestions.append(
            OptimizationSuggestion(
                version=version,
                category="AGENT_CONFIG",
                summary="子任务分配或召回低于门槛",
                recommended_change="复核 Agent Registry 能力描述与失败案例标注，不自动修改配置。",
                source_case_ids=failures,
            )
        )
    if report.artifact_reuse_accuracy < 0.95:
        suggestions.append(
            OptimizationSuggestion(
                version=version,
                category="ROUTING_POLICY",
                summary="Artifact 复用判断低于门槛",
                recommended_change="补充版本、checksum、本体兼容性反例后重新评测。",
                source_case_ids=failures,
            )
        )
    if report.forbidden_action_violations:
        suggestions.append(
            OptimizationSuggestion(
                version=version,
                category="DATASET",
                summary="发现禁止动作违规",
                recommended_change="阻断发布并补充审核门禁回归案例，人工确认后再发布任何变更。",
                source_case_ids=failures,
            )
        )
    return suggestions
