"""验证 Golden Dataset 指标和安全门禁计算。"""

from pathlib import Path

from kg_agent.domain.evaluation_models import GoldenEvaluationRecord
from kg_agent.evaluation.metrics import EvaluationPrediction, evaluate


def test_baseline_reaches_all_thresholds() -> None:
    """完全匹配的基线预测必须通过全部数值和安全门禁。"""
    root = Path(__file__).resolve().parents[2]
    golden = [
        GoldenEvaluationRecord.model_validate_json(line)
        for line in (root / "evaluation/golden_dataset.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    predictions = [
        EvaluationPrediction.model_validate_json(line)
        for line in (root / "evaluation/baseline_predictions.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    ]
    report = evaluate(golden, predictions)
    assert report.task_intent_macro_f1 == 1
    assert report.agent_assignment_accuracy == 1
    assert report.forbidden_action_violations == 0
    assert report.end_to_end_route_success_rate == 1
