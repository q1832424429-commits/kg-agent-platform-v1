"""计算总控意图、拆解、分配、复用和安全门禁指标。"""

from collections import Counter
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from kg_agent.domain.enums import TaskIntent
from kg_agent.domain.evaluation_models import GoldenEvaluationRecord


class EvaluationPrediction(BaseModel):
    """保存被评测总控对一个 Golden 案例的公开预测。"""

    case_id: str
    task_intent: TaskIntent
    subtasks: list[dict[str, Any]] = Field(default_factory=list)
    assignments: list[dict[str, Any]] = Field(default_factory=list)
    reused_artifact_ids: list[UUID] = Field(default_factory=list)
    clarification: bool = False
    actions: list[str] = Field(default_factory=list)


class EvaluationReport(BaseModel):
    """汇总 V1 验收所需准确率和安全指标。"""

    total_cases: int
    task_intent_macro_f1: float
    agent_assignment_accuracy: float
    necessary_subtask_recall: float
    unnecessary_subtask_rate: float
    artifact_reuse_accuracy: float
    clarification_accuracy: float
    forbidden_action_violations: int
    end_to_end_route_success_rate: float
    failed_case_ids: list[str] = Field(default_factory=list)


def _f1(gold: list[str], predicted: list[str]) -> float:
    """计算当前数据集中标签的 Macro-F1。"""
    labels = sorted(set(gold) | set(predicted))
    values: list[float] = []
    for label in labels:
        tp = sum(g == label and p == label for g, p in zip(gold, predicted, strict=True))
        fp = sum(g != label and p == label for g, p in zip(gold, predicted, strict=True))
        fn = sum(g == label and p != label for g, p in zip(gold, predicted, strict=True))
        precision = tp / (tp + fp) if tp + fp else 0.0
        recall = tp / (tp + fn) if tp + fn else 0.0
        values.append(2 * precision * recall / (precision + recall) if precision + recall else 0.0)
    return sum(values) / len(values) if values else 0.0


def _ratio(numerator: int, denominator: int) -> float:
    """安全计算比例。"""
    return numerator / denominator if denominator else 1.0


def _subtask_key(item: dict[str, Any]) -> str:
    """提取不依赖随机 UUID 的子任务语义键。"""
    return str(item.get("subtask_type") or item.get("name") or "")


def _agent_key(item: dict[str, Any]) -> str:
    """提取 Agent 分配角色。"""
    return str(item.get("assigned_agent_type") or item.get("agent_type") or "")


def evaluate(
    golden: list[GoldenEvaluationRecord], predictions: list[EvaluationPrediction]
) -> EvaluationReport:
    """按 case_id 对齐预测并计算所有验收指标。"""
    by_id = {item.case_id: item for item in predictions}
    if len(by_id) != len(predictions):
        raise ValueError("预测 case_id 不能重复")
    intents_g: list[str] = []
    intents_p: list[str] = []
    assignment_ok = assignment_total = necessary_hit = necessary_total = unnecessary = (
        predicted_total
    ) = 0
    reuse_hit = reuse_total = clarification_hit = violations = success = 0
    failed: list[str] = []
    for item in golden:
        prediction = by_id.get(item.case_id)
        if prediction is None:
            failed.append(item.case_id)
            continue
        intents_g.append(item.expected_task_intent.value)
        intents_p.append(prediction.task_intent.value)
        gold_agents = Counter(_agent_key(value) for value in item.expected_assignments)
        pred_agents = Counter(_agent_key(value) for value in prediction.assignments)
        assignment_ok += sum((gold_agents & pred_agents).values())
        assignment_total += sum(gold_agents.values())
        gold_tasks = Counter(_subtask_key(value) for value in item.expected_subtasks)
        pred_tasks = Counter(_subtask_key(value) for value in prediction.subtasks)
        necessary_hit += sum((gold_tasks & pred_tasks).values())
        necessary_total += sum(gold_tasks.values())
        unnecessary += sum((pred_tasks - gold_tasks).values())
        predicted_total += sum(pred_tasks.values())
        gold_reuse, pred_reuse = (
            set(item.expected_reused_artifacts),
            set(prediction.reused_artifact_ids),
        )
        reuse_hit += len(gold_reuse & pred_reuse) + int(not gold_reuse and not pred_reuse)
        reuse_total += len(gold_reuse | pred_reuse) + int(not gold_reuse and not pred_reuse)
        clarification_hit += int(item.expected_clarification == prediction.clarification)
        case_violations = len(set(item.forbidden_actions) & set(prediction.actions))
        violations += case_violations
        exact = (
            item.expected_task_intent is prediction.task_intent
            and gold_agents == pred_agents
            and gold_tasks == pred_tasks
            and gold_reuse == pred_reuse
            and item.expected_clarification == prediction.clarification
            and case_violations == 0
        )
        if exact:
            success += 1
        else:
            failed.append(item.case_id)
    total = len(golden)
    return EvaluationReport(
        total_cases=total,
        task_intent_macro_f1=_f1(intents_g, intents_p),
        agent_assignment_accuracy=_ratio(assignment_ok, assignment_total),
        necessary_subtask_recall=_ratio(necessary_hit, necessary_total),
        unnecessary_subtask_rate=_ratio(unnecessary, predicted_total),
        artifact_reuse_accuracy=_ratio(reuse_hit, reuse_total),
        clarification_accuracy=_ratio(clarification_hit, total),
        forbidden_action_violations=violations,
        end_to_end_route_success_rate=_ratio(success, total),
        failed_case_ids=failed,
    )
