"""确定性校验专业 Agent 的 Skill 选择计划。"""

from collections import Counter
from uuid import UUID

from pydantic import BaseModel

from kg_agent.agents.professional.models import ProfessionalAgentDecision
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.registry_models import AgentRegistration
from kg_agent.exceptions import ArtifactValidationError, RegistryValidationError
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry, missing_skill_input_types


class ProfessionalPlanIssue(BaseModel):
    """描述一个阻止专业 Agent 执行的结构化计划问题。"""

    code: str
    message: str
    skill_name: str | None = None


class ProfessionalPlanValidator:
    """校验角色、allowlist、输入、预期输出和建议子任务边界。"""

    def __init__(
        self,
        *,
        agent_registry: AgentRegistry,
        skill_registry: SkillRegistry,
        artifact_registry: ArtifactRegistry,
    ) -> None:
        """注入三个 Registry 事实来源，不复制能力名称分支。"""

        self._agents = agent_registry
        self._skills = skill_registry
        self._artifacts = artifact_registry

    def validate(
        self,
        *,
        decision: ProfessionalAgentDecision,
        registration: AgentRegistration,
        input_artifacts: list[ArtifactRef],
        expected_output_types: list[str],
    ) -> list[ProfessionalPlanIssue]:
        """返回全部可确定的越权、输入和输出覆盖问题。"""

        issues: list[ProfessionalPlanIssue] = []
        available = {item.artifact_id: item for item in input_artifacts}
        if decision.requires_user_input:
            # 等待用户输入不会调用 Skill，但 ProposedSubTask 仍属于可持久化公开结果，
            # 必须继续校验其 Agent、输入和输出引用。
            self._validate_proposals(decision, available, issues)
            return issues
        selected_outputs: set[str] = set()
        names = [item.skill_name for item in decision.skill_calls]
        if any(count > 1 for count in Counter(names).values()):
            issues.append(
                ProfessionalPlanIssue(
                    code="DUPLICATE_SKILL_CALL", message="同一次运行不能重复调用同名 Skill"
                )
            )
        for call in decision.skill_calls:
            try:
                skill = self._skills.require(call.skill_name)
            except RegistryValidationError:
                issues.append(
                    ProfessionalPlanIssue(
                        code="UNKNOWN_SKILL",
                        message=f"Skill 未注册：{call.skill_name}",
                        skill_name=call.skill_name,
                    )
                )
                continue
            if call.skill_name not in registration.allowed_skills:
                issues.append(
                    ProfessionalPlanIssue(
                        code="SKILL_NOT_ALLOWED",
                        message="Skill 不在当前 Agent allowlist",
                        skill_name=call.skill_name,
                    )
                )
            if registration.agent_type not in skill.allowed_agents:
                issues.append(
                    ProfessionalPlanIssue(
                        code="SKILL_AGENT_MISMATCH",
                        message="Skill Registry 未授权当前 Agent",
                        skill_name=call.skill_name,
                    )
                )
            if skill.lifecycle != "ACTIVE":
                issues.append(
                    ProfessionalPlanIssue(
                        code="INACTIVE_SKILL",
                        message="不能调用非 ACTIVE Skill",
                        skill_name=call.skill_name,
                    )
                )
            if skill.side_effect and not self._skills.side_effects_enabled:
                issues.append(
                    ProfessionalPlanIssue(
                        code="SIDE_EFFECT_SKILL_CLOSED",
                        message="发布类 Skill 必须先绑定 PublishGuard",
                        skill_name=call.skill_name,
                    )
                )
            selected = [available.get(item) for item in call.input_artifact_ids]
            if len(set(call.input_artifact_ids)) != len(call.input_artifact_ids):
                issues.append(
                    ProfessionalPlanIssue(
                        code="DUPLICATE_SKILL_INPUT",
                        message="Skill 输入 Artifact 重复",
                        skill_name=call.skill_name,
                    )
                )
            unknown = [
                item
                for item, ref in zip(call.input_artifact_ids, selected, strict=True)
                if ref is None
            ]
            if unknown:
                issues.append(
                    ProfessionalPlanIssue(
                        code="UNKNOWN_SKILL_INPUT",
                        message=f"Skill 引用了未分配输入：{unknown}",
                        skill_name=call.skill_name,
                    )
                )
                continue
            actual_types = {ref.artifact_type for ref in selected if ref is not None}
            missing = missing_skill_input_types(skill, actual_types)
            if missing:
                issues.append(
                    ProfessionalPlanIssue(
                        code="MISSING_SKILL_INPUT_TYPE",
                        message=f"Skill 缺少输入类型：{missing}",
                        skill_name=call.skill_name,
                    )
                )
            if skill.name == "publish_ontology":
                candidates = [
                    ref
                    for ref in selected
                    if ref is not None
                    and ref.artifact_type in {"CandidateOntology", "FusedCandidateOntology"}
                ]
                if len(candidates) != 1:
                    issues.append(
                        ProfessionalPlanIssue(
                            code="ONTOLOGY_CANDIDATE_AMBIGUOUS",
                            message="本体发布必须且只能选择一个候选或融合本体版本",
                            skill_name=call.skill_name,
                        )
                    )
            selected_outputs.update(skill.output_artifact_types)
        missing_outputs = set(expected_output_types) - selected_outputs
        if missing_outputs:
            issues.append(
                ProfessionalPlanIssue(
                    code="EXPECTED_OUTPUT_NOT_COVERED",
                    message=f"所选 Skill 无法覆盖预期输出：{sorted(missing_outputs)}",
                )
            )
        self._validate_proposals(decision, available, issues)
        return issues

    def _validate_proposals(
        self,
        decision: ProfessionalAgentDecision,
        available: dict[UUID, ArtifactRef],
        issues: list[ProfessionalPlanIssue],
    ) -> None:
        """建议只做契约检查；正式发布权仍由总控 Agent 独占。"""

        for proposal in decision.proposed_subtasks:
            if proposal.proposed_agent_type is not None:
                if self._agents.get(proposal.proposed_agent_type) is None:
                    issues.append(
                        ProfessionalPlanIssue(
                            code="UNKNOWN_PROPOSED_AGENT", message="建议的专业 Agent 未注册"
                        )
                    )
            if not set(proposal.input_artifact_ids) <= set(available):
                issues.append(
                    ProfessionalPlanIssue(
                        code="UNKNOWN_PROPOSED_INPUT", message="建议子任务引用了未分配 Artifact"
                    )
                )
            for artifact_type in proposal.expected_output_types:
                try:
                    self._artifacts.require(artifact_type)
                except ArtifactValidationError:
                    issues.append(
                        ProfessionalPlanIssue(
                            code="UNKNOWN_PROPOSED_OUTPUT",
                            message=f"建议输出类型未注册：{artifact_type}",
                        )
                    )
