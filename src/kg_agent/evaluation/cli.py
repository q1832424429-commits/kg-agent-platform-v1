"""从 JSONL 文件运行可重复的总控准确率评测。"""

import argparse
import json
from pathlib import Path

from pydantic import BaseModel

from kg_agent.domain.evaluation_models import GoldenEvaluationRecord
from kg_agent.evaluation.metrics import EvaluationPrediction, evaluate


def _read[ModelT: BaseModel](path: Path, model: type[ModelT]) -> list[ModelT]:
    """逐行读取 JSONL 并执行 Pydantic 校验。"""
    return [
        model.model_validate_json(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def main() -> None:
    """解析命令行参数、计算报告并按可选门禁设置退出码。"""
    parser = argparse.ArgumentParser(description="KG Agent V1 Golden Dataset 评测")
    parser.add_argument("--golden", type=Path, required=True)
    parser.add_argument("--predictions", type=Path, required=True)
    parser.add_argument("--enforce-thresholds", action="store_true")
    args = parser.parse_args()
    golden = _read(args.golden, GoldenEvaluationRecord)
    predictions = _read(args.predictions, EvaluationPrediction)
    report = evaluate(golden, predictions)
    print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    if args.enforce_thresholds:
        passed = (
            report.task_intent_macro_f1 >= 0.95
            and report.agent_assignment_accuracy >= 0.95
            and report.necessary_subtask_recall >= 0.95
            and report.unnecessary_subtask_rate <= 0.05
            and report.artifact_reuse_accuracy >= 0.95
            and report.forbidden_action_violations == 0
            and report.end_to_end_route_success_rate >= 0.90
        )
        if not passed:
            raise SystemExit(1)


if __name__ == "__main__":
    main()
