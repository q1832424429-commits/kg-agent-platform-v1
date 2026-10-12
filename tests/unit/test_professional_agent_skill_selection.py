"""验证专业 Agent 在 Skill 契约唯一匹配时不调用模型做重复选择。"""

from pathlib import Path
from uuid import uuid4

from kg_agent.agents.professional.agent import BaseProfessionalAgent
from kg_agent.agents.professional.models import ProfessionalAgentRequest
from kg_agent.agents.professional.prompts import ProfessionalPromptRepository
from kg_agent.domain.artifact_models import ArtifactCreateCommand, ArtifactRecord
from kg_agent.domain.assignment_models import AgentAssignment
from kg_agent.domain.context_view_models import (
    ArtifactContextItem,
    AssignmentContextItem,
    ProfessionalAgentContextView,
)
from kg_agent.domain.enums import AssignmentStatus
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from tests.factories import NOW, make_artifact, make_subtask


class _UnusedWriter:
    """唯一匹配单元测试不执行 Skill，因此禁止意外写入。"""

    async def create(self, command: ArtifactCreateCommand) -> ArtifactRecord:
        """若测试意外进入 Artifact 写入则立即失败。"""

        raise AssertionError(f"不应写入 {command.artifact_type}")


def test_unique_skill_contract_builds_plan_without_model_call(project_root: Path) -> None:
    """文档解析输出唯一匹配 parse_document 时应直接形成调用计划。"""

    task_id = uuid4()
    raw = make_artifact(task_id, artifact_type="RawDocument")
    subtask = make_subtask(
        task_id,
        outputs=["ParsedDocument", "SemanticDocument"],
        inputs=[raw.artifact_id],
    )
    assignment = AgentAssignment(
        assignment_id=uuid4(),
        subtask_id=subtask.subtask_id,
        assigned_agent_type="document_parsing_agent",
        claimed_by="unit-worker",
        status=AssignmentStatus.RUNNING,
        fencing_token=1,
        created_at=NOW,
    )
    context_artifact = ArtifactContextItem(
        artifact_id=raw.artifact_id,
        artifact_type=raw.artifact_type,
        schema_version=raw.schema_version,
        content_summary=raw.content_summary,
        checksum=raw.checksum,
        parent_artifact_ids=raw.parent_artifact_ids,
    )
    request = ProfessionalAgentRequest(
        assignment=assignment,
        subtask=subtask,
        task_spec_revision=1,
        context_view=ProfessionalAgentContextView(
            task_id=task_id,
            context_version=1,
            assignment=AssignmentContextItem(
                assignment_id=assignment.assignment_id,
                subtask_id=subtask.subtask_id,
                assigned_agent_type=assignment.assigned_agent_type,
                status=assignment.status,
                attempt=assignment.attempt,
            ),
            subtask_id=subtask.subtask_id,
            subtask_type=subtask.subtask_type,
            goal=subtask.goal,
            completion_criteria=subtask.completion_criteria,
            input_artifacts=[context_artifact],
            business_goal="解析用户上传文档",
            domain="manufacturing",
        ),
        input_artifacts=[raw],
        worker_id="unit-worker",
    )
    artifacts = ArtifactRegistry.v1()
    agents = AgentRegistry.from_directory(project_root / "configs" / "agents")
    skills = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=artifacts,
    )
    model = FakeModelAdapter([])
    agent = BaseProfessionalAgent(
        registration=agents.require("document_parsing_agent"),
        model_gateway=model,
        agent_registry=agents,
        skill_registry=skills,
        artifact_registry=artifacts,
        artifact_writer=_UnusedWriter(),
        prompt_repository=ProfessionalPromptRepository(
            project_root / "configs" / "prompts" / "professional"
        ),
    )

    decision = agent._deterministic_skill_decision(request)

    assert decision is not None
    assert [call.skill_name for call in decision.skill_calls] == ["parse_document"]
    assert decision.skill_calls[0].input_artifact_ids == [raw.artifact_id]
    assert model.calls == []
