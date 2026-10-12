"""加载专业 Agent 版本化 Prompt，并构造最小角色上下文。"""

import json
from pathlib import Path

from kg_agent.agents.professional.models import (
    ProfessionalAgentDecision,
    ProfessionalAgentRequest,
)
from kg_agent.domain.registry_models import AgentRegistration, SkillRegistration
from kg_agent.validators.professional_plan_validator import ProfessionalPlanIssue


class ProfessionalPromptRepository:
    """读取所有专业 Agent 共用约束和单角色职责 Prompt。"""

    def __init__(self, directory: Path) -> None:
        """保存版本化 Prompt 根目录。"""

        self._directory = directory

    def load_system_prompt(self, registration: AgentRegistration | str) -> str:
        """组合公共规则与内置文件或已发布自定义职责说明。

        保留字符串 Agent 类型入参，兼容只读取内置 Prompt 的既有调用方和测试；实际
        专业 Agent 运行始终传入完整注册信息，因而能读取自定义 AGENT.md 正文。
        """

        common = (self._directory / "system.v1.md").read_text(encoding="utf-8")
        agent_type = registration if isinstance(registration, str) else registration.agent_type
        role_path = self._directory / f"{agent_type}.v1.md"
        if role_path.is_file():
            role = role_path.read_text(encoding="utf-8")
        elif (
            isinstance(registration, AgentRegistration)
            and registration.instruction_markdown.strip()
        ):
            # 自定义 Agent 没有仓库内角色文件；只加载已发布 AGENT.md 的正文，不让
            # 用户定义覆盖平台公共安全规则或修改 Agent 的真实权限 YAML。
            role = (
                f"# {registration.name}（用户自定义专业 Agent）\n\n"
                f"{registration.instruction_markdown.strip()}"
            )
        else:
            raise FileNotFoundError(f"未找到 Agent 职责说明：{agent_type}")
        return f"{common}\n\n{role}"


def build_professional_prompt(
    *,
    request: ProfessionalAgentRequest,
    registration: AgentRegistration,
    skills: list[SkillRegistration],
) -> str:
    """只把角色视图、输入摘要、allowlist 和目标 Schema 提供给模型。"""

    payload = {
        "agent": registration.model_dump(mode="json"),
        "assignment": request.context_view.model_dump(mode="json"),
        "available_skills": [item.model_dump(mode="json") for item in skills],
        "input_artifacts": [
            {
                "artifact_id": str(item.artifact_id),
                "artifact_type": item.artifact_type,
                "schema_version": item.schema_version,
                "content_summary": item.content_summary,
                "checksum": item.checksum,
                "ontology_id": str(item.ontology_id) if item.ontology_id else None,
                "ontology_version": item.ontology_version,
                "parent_artifact_ids": [str(parent) for parent in item.parent_artifact_ids],
            }
            for item in request.input_artifacts
        ],
        "output_json_schema": ProfessionalAgentDecision.model_json_schema(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def build_professional_repair_prompt(
    *,
    base_prompt: str,
    previous: ProfessionalAgentDecision | None,
    issues: list[ProfessionalPlanIssue],
) -> str:
    """将确定性校验错误反馈给同一个专业 Agent 重新生成完整计划。"""

    payload = {
        "previous_decision": previous.model_dump(mode="json") if previous else None,
        "validation_issues": [item.model_dump(mode="json") for item in issues],
        "instruction": "修复全部错误并返回完整 ProfessionalAgentDecision JSON。",
    }
    return f"{base_prompt}\n\n修复信息：\n{json.dumps(payload, ensure_ascii=False, indent=2)}"
