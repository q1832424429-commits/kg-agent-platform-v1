"""定义自定义 Agent、Skill 在 HTTP 接口与持久化服务间使用的数据契约。

这里的模型描述“定义包”，不是任务运行时的 AgentSubTask 或 Artifact 内容。定义包
由用户维护；真正的执行输入输出仍受项目既有 Artifact Registry 与任务契约约束。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from kg_agent.extensions.script_field_mapping import (
    EntityRecordAdapter,
    ScriptFunctionAdapter,
    ScriptInputFieldBinding,
    ScriptOutputFieldBinding,
)

DefinitionStatus = Literal["DRAFT", "ACTIVE", "DISABLED"]
RevisionStatus = Literal["DRAFT", "PUBLISHED", "SUPERSEDED"]
IntegrationMode = Literal["PIPELINE", "SIDE_OUTPUT", "STANDALONE"]
ResultSemantics = Literal["DERIVE", "TRANSFORM", "REPORT"]
ContractCheckStatus = Literal["PASSED", "FAILED"]


class BusinessSkillDraftInput(BaseModel):
    """接收非开发者填写的自定义 Skill 业务需求。

    此模型刻意不暴露 Artifact、子任务类型、运行实现或内部标识。服务先让模型从
    代码注册的受控契约模板中推荐一种使用方式，再由用户确认并编译为内部契约。
    """

    name: str = Field(min_length=1, max_length=256)
    solves_problem: str = Field(min_length=1, max_length=8_000)
    applicable_materials: str = Field(min_length=1, max_length=2_000)
    expected_result: str = Field(min_length=1, max_length=8_000)
    example_request: str = Field(min_length=1, max_length=4_000)
    additional_requirements: str = Field(default="", max_length=8_000)
    # 该字段由受控“能力使用方式确认”页面回传，不要求业务用户理解或手工填写。
    # 为空仅用于兼容历史接口，并映射到旧版文档表格分析契约。
    contract_profile_id: str | None = Field(default=None, max_length=128)


class SkillContractOption(BaseModel):
    """向前端公开一个不含内部 Artifact 名称的业务契约选项。"""

    profile_id: str
    business_name: str
    integration_mode: IntegrationMode
    input_description: str
    output_description: str
    suggested_position: str


class SkillContractProposal(BaseModel):
    """返回模型建议与代码验证后的自定义 Skill 使用方式。"""

    selected: SkillContractOption
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=2_000)
    requires_confirmation: bool = True
    alternatives: list[SkillContractOption] = Field(default_factory=list)


class BusinessAgentDraftInput(BaseModel):
    """接收非开发者填写的自定义 Agent 业务职责与授权 Skill。

    任务类型、可接受输入和可产生输出不由用户填写，而是由选中的已发布 Skill 目录
    确定性推导；``allowed_skill_names`` 最终只写入该 Agent 的 permissions.yaml。
    """

    name: str = Field(min_length=1, max_length=256)
    responsibility: str = Field(min_length=1, max_length=8_000)
    applicable_scenarios: str = Field(min_length=1, max_length=4_000)
    example_request: str = Field(min_length=1, max_length=4_000)
    allowed_skill_names: list[str] = Field(min_length=1)

    @field_validator("allowed_skill_names")
    @classmethod
    def unique_nonempty_skill_names(cls, values: list[str]) -> list[str]:
        """拒绝空白或重复授权名称，避免生成含糊的权限 YAML。"""

        if any(not value.strip() for value in values) or len(values) != len(set(values)):
            raise ValueError("允许调用的 Skill 不能包含空值或重复值")
        return values


class CustomSkillDefinitionInput(BaseModel):
    """接收用户填写的一个自定义 Skill 草稿。

    ``instruction_markdown`` 是 SKILL.md 的正文。用户不选择执行类型；发布时根据
    当前草稿实际上传的脚本资源确定内部执行路径。
    """

    skill_key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=8000)
    supported_subtask_types: list[str] = Field(min_length=1)
    input_artifact_types: list[str] = Field(default_factory=list)
    output_artifact_types: list[str] = Field(min_length=1)
    parameters_schema: dict[str, Any] = Field(default_factory=dict)
    # 创建阶段确认后随不可变修订保存；运行时不允许模型重新猜测。
    script_input_bindings: list[ScriptInputFieldBinding] = Field(default_factory=list)
    script_output_bindings: list[ScriptOutputFieldBinding] = Field(default_factory=list)
    entity_record_adapter: EntityRecordAdapter | None = None
    function_adapter: ScriptFunctionAdapter | None = None
    # 仅由服务端填入；历史逐项适配修订没有该字段，仍走原 Runner 路径。
    generated_script_entrypoint: str | None = None
    instruction_markdown: str = Field(min_length=1, max_length=100_000)
    timeout_seconds: int = Field(default=180, ge=1, le=900)
    max_retries: int = Field(default=2, ge=0, le=2)
    # 仅由业务化创建/编辑入口写入，用于下次回显业务表单。它不参与运行期参数，
    # 也不向用户暴露内部 Artifact 契约。
    business_input: dict[str, str] | None = None
    contract_profile_id: str | None = Field(default=None, max_length=128)
    integration_mode: IntegrationMode | None = None
    result_semantics: ResultSemantics | None = None

    @field_validator("supported_subtask_types", "input_artifact_types", "output_artifact_types")
    @classmethod
    def unique_nonempty_names(cls, values: list[str]) -> list[str]:
        """拒绝空值或重复条目，避免定义包在发布后产生含糊契约。"""

        if any(not value.strip() for value in values) or len(values) != len(set(values)):
            raise ValueError("列表不能包含空值或重复值")
        return values


class CustomAgentDefinitionInput(BaseModel):
    """接收用户填写的一个自定义专业 Agent 草稿。

    Skill 调用权限只由 ``allowed_skill_names`` 生成的 permissions.yaml 表达；Skill
    自身元数据不保存反向 Agent allowlist。
    """

    agent_key: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    name: str = Field(min_length=1, max_length=256)
    description: str = Field(min_length=1, max_length=8000)
    accepted_subtask_types: list[str] = Field(min_length=1)
    accepted_input_types: list[str] = Field(default_factory=list)
    produced_output_types: list[str] = Field(default_factory=list)
    instruction_markdown: str = Field(min_length=1, max_length=100_000)
    allowed_skill_names: list[str] = Field(default_factory=list)
    routing_examples: list[dict[str, str]] = Field(default_factory=list)

    @field_validator(
        "accepted_subtask_types",
        "accepted_input_types",
        "produced_output_types",
        "allowed_skill_names",
    )
    @classmethod
    def unique_nonempty_names(cls, values: list[str]) -> list[str]:
        """拒绝权限和能力清单中的空值及重复项。"""

        if any(not value.strip() for value in values) or len(values) != len(set(values)):
            raise ValueError("列表不能包含空值或重复值")
        return values


class CustomRevisionSummary(BaseModel):
    """向前端返回一个修订的公开摘要，避免默认传回资源二进制内容。"""

    revision_id: UUID
    revision_number: int
    status: RevisionStatus
    checksum: str
    created_at: datetime
    published_at: datetime | None = None


class ScriptDependencyFinding(BaseModel):
    """描述脚本静态扫描发现的一个依赖及其受控运行环境支持状态。"""

    name: str
    source: Literal["IMPORT", "REQUIREMENTS"]
    supported: bool
    reason: str = ""


class CustomSkillContractCheck(BaseModel):
    """向前端公开一次脚本 Skill 契约试运行的可审计结果。"""

    check_id: UUID
    skill_revision_id: UUID
    contract_profile_id: str
    bundle_checksum: str
    runtime_profile_id: str
    protocol_version: str
    status: ContractCheckStatus
    dependency_findings: list[ScriptDependencyFinding] = Field(default_factory=list)
    diagnostics: list[str] = Field(default_factory=list)
    observed_output_fields: list[str] = Field(default_factory=list)
    observed_output_shapes: dict[str, str] = Field(default_factory=dict)
    duration_ms: float = Field(default=0, ge=0)
    checked_at: datetime


class CustomSkillDetail(BaseModel):
    """返回自定义 Skill 的稳定身份、当前定义和可见草稿状态。"""

    skill_id: UUID
    skill_key: str
    name: str
    description: str
    status: DefinitionStatus
    current_revision_id: UUID | None = None
    current_revision: CustomRevisionSummary | None = None
    draft_revision: CustomRevisionSummary | None = None
    metadata: dict[str, Any] | None = None
    skill_markdown: str | None = None
    resources: list[dict[str, Any]] = Field(default_factory=list)
    latest_contract_check: CustomSkillContractCheck | None = None
    created_at: datetime
    updated_at: datetime


class CustomAgentDetail(BaseModel):
    """返回自定义专业 Agent 的身份、职责正文与唯一权限 YAML。"""

    agent_id: UUID
    agent_key: str
    name: str
    description: str
    status: DefinitionStatus
    current_revision_id: UUID | None = None
    current_revision: CustomRevisionSummary | None = None
    draft_revision: CustomRevisionSummary | None = None
    metadata: dict[str, Any] | None = None
    agent_markdown: str | None = None
    permissions_yaml: str | None = None
    routing_examples: list[dict[str, str]] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class CustomResourceSummary(BaseModel):
    """描述 Skill 修订绑定的一个资源，不在列表接口直接返回内容。"""

    resource_id: UUID
    relative_path: str
    media_type: str
    checksum: str
    size_bytes: int
    created_at: datetime


class AvailableSkillOption(BaseModel):
    """向自定义 Agent 表单提供可选择的已发布 Skill 能力摘要。"""

    # 提交时仍传递 name 作为 permissions.yaml 内部引用；界面只展示 display_name。
    name: str
    display_name: str
    description: str
    source: Literal["BUILTIN", "CUSTOM"]
    supported_subtask_types: list[str]
    input_artifact_types: list[str]
    output_artifact_types: list[str]
