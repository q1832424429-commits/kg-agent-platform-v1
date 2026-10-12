"""验证离线自进化只生成建议，不修改运行配置。"""

from kg_agent.evaluation.metrics import EvaluationReport
from kg_agent.evolution.suggestions import generate_suggestions


def test_low_metric_creates_proposed_suggestion() -> None:
    """低于阈值时应产生带版本和来源的待审核建议。"""
    report = EvaluationReport(
        total_cases=1,
        task_intent_macro_f1=0.5,
        agent_assignment_accuracy=1,
        necessary_subtask_recall=1,
        unnecessary_subtask_rate=0,
        artifact_reuse_accuracy=1,
        clarification_accuracy=1,
        forbidden_action_violations=0,
        end_to_end_route_success_rate=0,
        failed_case_ids=["case-1"],
    )
    suggestions = generate_suggestions(report, version=2)
    assert suggestions[0].version == 2
    assert suggestions[0].status == "PROPOSED"
    assert suggestions[0].source_case_ids == ["case-1"]
