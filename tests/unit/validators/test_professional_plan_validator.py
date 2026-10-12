"""验证专业 Agent 在调用发布 Skill 前必须携带完整审核输入。"""

from pathlib import Path
from uuid import uuid4

import pytest

from kg_agent.agents.professional.models import ProfessionalAgentDecision, SkillCallPlan
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.validators.professional_plan_validator import ProfessionalPlanValidator
from tests.factories import make_artifact


def test_publish_ontology_plan_requires_candidate_and_approval(project_root: Path) -> None:
    """发布本体的 Skill 计划漏传候选本体时，必须在执行前被确定性拒绝。

    PublishGuard 需要候选本体和对应 Approval 的一对一组合。本测试确保 Registry
    也声明相同输入，使专业 Agent 可在计划修复阶段补齐输入，而不是实际发布后触发
    不可恢复的门禁错误。
    """

    task_id = uuid4()
    candidate = make_artifact(task_id, "CandidateOntology")
    approval = make_artifact(task_id, "OntologyApproval")
    agents = AgentRegistry.from_directory(project_root / "configs" / "agents")
    artifacts = ArtifactRegistry.v1()
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog", artifact_registry=artifacts
    )
    validator = ProfessionalPlanValidator(
        agent_registry=agents,
        skill_registry=skills,
        artifact_registry=artifacts,
    )
    decision = ProfessionalAgentDecision(
        decision_summary="错误地只提交审核批准记录",
        skill_calls=[
            SkillCallPlan(
                skill_name="publish_ontology",
                input_artifact_ids=[approval.artifact_id],
            )
        ],
    )

    issues = validator.validate(
        decision=decision,
        registration=agents.require("graph_management_agent"),
        input_artifacts=[candidate, approval],
        expected_output_types=["PublishedOntology"],
    )

    assert any(
        issue.code == "MISSING_SKILL_INPUT_TYPE"
        and issue.skill_name == "publish_ontology"
        and "CandidateOntology" in issue.message
        for issue in issues
    )


@pytest.mark.parametrize("candidate_type", ["CandidateOntology", "FusedCandidateOntology"])
def test_publish_ontology_accepts_either_reviewed_candidate(
    project_root: Path, candidate_type: str
) -> None:
    """普通或融合候选本体均可与对应 Approval 组成发布输入。"""

    task_id = uuid4()
    candidate = make_artifact(task_id, candidate_type)
    approval = make_artifact(task_id, "OntologyApproval")
    agents = AgentRegistry.from_directory(project_root / "configs" / "agents")
    artifacts = ArtifactRegistry.v1()
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog", artifact_registry=artifacts
    )
    validator = ProfessionalPlanValidator(
        agent_registry=agents,
        skill_registry=skills,
        artifact_registry=artifacts,
    )
    decision = ProfessionalAgentDecision(
        decision_summary="发布当前审核通过的本体版本",
        skill_calls=[
            SkillCallPlan(
                skill_name="publish_ontology",
                input_artifact_ids=[candidate.artifact_id, approval.artifact_id],
            )
        ],
    )
    issues = validator.validate(
        decision=decision,
        registration=agents.require("graph_management_agent"),
        input_artifacts=[candidate, approval],
        expected_output_types=["PublishedOntology"],
    )

    assert not any(
        issue.code in {"MISSING_SKILL_INPUT_TYPE", "ONTOLOGY_CANDIDATE_AMBIGUOUS"}
        for issue in issues
    )


def test_publish_ontology_rejects_ambiguous_candidate_selection(project_root: Path) -> None:
    """两类候选同时传入时必须先选定一个，不能由发布 Skill 猜测。"""

    task_id = uuid4()
    candidates = [
        make_artifact(task_id, "CandidateOntology"),
        make_artifact(task_id, "FusedCandidateOntology"),
    ]
    approval = make_artifact(task_id, "OntologyApproval")
    agents = AgentRegistry.from_directory(project_root / "configs" / "agents")
    artifacts = ArtifactRegistry.v1()
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog", artifact_registry=artifacts
    )
    validator = ProfessionalPlanValidator(
        agent_registry=agents,
        skill_registry=skills,
        artifact_registry=artifacts,
    )
    decision = ProfessionalAgentDecision(
        decision_summary="错误地同时发布两个候选本体",
        skill_calls=[
            SkillCallPlan(
                skill_name="publish_ontology",
                input_artifact_ids=[
                    *(candidate.artifact_id for candidate in candidates),
                    approval.artifact_id,
                ],
            )
        ],
    )
    issues = validator.validate(
        decision=decision,
        registration=agents.require("graph_management_agent"),
        input_artifacts=[*candidates, approval],
        expected_output_types=["PublishedOntology"],
    )

    assert any(issue.code == "ONTOLOGY_CANDIDATE_AMBIGUOUS" for issue in issues)
