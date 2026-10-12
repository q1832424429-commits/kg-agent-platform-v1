"""验证第一阶段开发案例目录覆盖约定的核心路由场景。"""

import json
from pathlib import Path

from kg_agent.domain.enums import TaskIntent


def test_controller_case_catalog_has_required_scenarios(project_root: Path) -> None:
    """开发案例必须覆盖新建、继续、修改、审核和禁止行为。"""

    path = project_root / "tests" / "fixtures" / "controller_cases.json"
    cases = json.loads(path.read_text(encoding="utf-8"))
    case_ids = {case["case_id"] for case in cases}

    assert {
        "start_full_graph",
        "continue_current_task",
        "modify_task_goal",
        "continue_without_current_task",
        "ontology_review_required",
        "graph_review_required",
        "reject_knowledge_query",
        "reject_unapproved_publish",
        "reject_unknown_agent",
    } <= case_ids
    assert {case["expected_intent"] for case in cases} <= {intent.value for intent in TaskIntent}
