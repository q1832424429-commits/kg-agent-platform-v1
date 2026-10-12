"""定义 Agent Registry 与 Skill Registry 的公开元数据契约。

本模块只描述注册信息结构；业务 Skill 运行由 SkillRegistry 委托给独立实现。
"""

from typing import Any, Literal

from pydantic import BaseModel


class AgentRegistration(BaseModel):
    """描述专业 Agent 向总控 Agent 暴露的能力和权限。"""

    agent_type: str
    name: str
    version: str
    description: str
    accepted_subtask_types: list[str]
    accepted_input_types: list[str]
    produced_output_types: list[str]
    allowed_skills: list[str]
    can_publish_subtasks: bool = False
    can_publish_ontology: bool = False
    can_publish_graph: bool = False
    lifecycle: str
    # 内置 Agent 从仓库 Prompt 文件取得职责；用户自定义 Agent 的职责正文来自当前
    # 已发布 AGENT.md。该字段不会进入普通总控的能力目录投影。
    instruction_markdown: str = ""


class SkillRegistration(BaseModel):
    """描述 Skill Registry 保存的能力、契约和权限声明。"""

    name: str
    # ``name`` 是不可变的机器标识，用于权限 YAML、SkillInvocation 与 Artifact 血缘。
    # 业务用户填写的名称独立保存为 display_name，避免中文名称或改名破坏运行契约。
    display_name: str | None = None
    version: str
    description: str
    owner_agent: str | None
    supported_subtask_types: list[str]
    allowed_agents: list[str]
    input_artifact_types: list[str]
    output_artifact_types: list[str]
    parameters_schema: dict[str, Any]
    # 默认 Skill 可以在同一 AgentSubTask 内与同类 Skill 组合。少数带有明确
    # Artifact 流转边界的 Skill 则必须独占一个子任务，避免模型把“提出建议”和
    # “应用建议”混成一次不可审计、且无法传递中间 Artifact 的操作。
    requires_dedicated_subtask: bool = False
    side_effect: bool
    timeout_seconds: int
    max_retries: int
    lifecycle: str
    # 内置 Skill 不需要填写；自定义 Skill 用它向总控和校验器公开受控使用方式。
    contract_profile_id: str | None = None
    integration_mode: Literal["PIPELINE", "SIDE_OUTPUT", "STANDALONE"] | None = None
    result_semantics: Literal["DERIVE", "TRANSFORM", "REPORT"] | None = None
