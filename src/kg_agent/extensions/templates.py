"""渲染自定义定义包的标准 Markdown 与 YAML 文件内容。

所有定义源文件最终保存在 PostgreSQL；本模块只负责稳定、可重建的文本表示和 checksum
计算，不接触文件系统，也不执行资源中的脚本。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

import yaml

from kg_agent.extensions.contracts import CustomAgentDefinitionInput, CustomSkillDefinitionInput


def checksum_for(*parts: str | bytes) -> str:
    """计算定义内容的 SHA-256，用于发布指针和运行期来源校验。"""

    digest = hashlib.sha256()
    for part in parts:
        digest.update(part.encode("utf-8") if isinstance(part, str) else part)
        digest.update(b"\x00")
    return digest.hexdigest()


def render_skill_markdown(value: CustomSkillDefinitionInput) -> tuple[dict[str, Any], str]:
    """将用户填写的元数据和说明渲染为标准 SKILL.md 全文。"""

    metadata: dict[str, Any] = {
        # name 是 Registry 的稳定机器标识，不能改为用户可编辑的中文业务名称；后者
        # 以 display_name 进入 SKILL.md，供预览、提示词和用户界面使用。
        "name": value.skill_key,
        "display_name": value.name,
        "version": "internal",
        "description": value.description,
        "owner_agent": None,
        "supported_subtask_types": value.supported_subtask_types,
        "input_artifact_types": value.input_artifact_types,
        "output_artifact_types": value.output_artifact_types,
        "parameters_schema": value.parameters_schema,
        "script_input_bindings": [
            binding.model_dump(mode="json") for binding in value.script_input_bindings
        ],
        "script_output_bindings": [
            binding.model_dump(mode="json") for binding in value.script_output_bindings
        ],
        "entity_record_adapter": (
            value.entity_record_adapter.model_dump(mode="json")
            if value.entity_record_adapter is not None
            else None
        ),
        "generated_script_entrypoint": value.generated_script_entrypoint,
        "function_adapter": (
            value.function_adapter.model_dump(mode="json")
            if value.function_adapter is not None
            else None
        ),
        "side_effect": False,
        "timeout_seconds": value.timeout_seconds,
        "max_retries": value.max_retries,
        "lifecycle": "ACTIVE",
    }
    # 保存业务表单原文只为后续编辑回显；运行时 Prompt 仍只读取正文，避免把界面状态
    # 混入 Skill 的实际执行参数。
    if value.business_input is not None:
        metadata["business_input"] = value.business_input
    if value.contract_profile_id is not None:
        metadata["contract_profile_id"] = value.contract_profile_id
    if value.integration_mode is not None:
        metadata["integration_mode"] = value.integration_mode
    if value.result_semantics is not None:
        metadata["result_semantics"] = value.result_semantics
    front_matter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    return metadata, f"---\n{front_matter}\n---\n\n{value.instruction_markdown.strip()}\n"


def render_agent_markdown(value: CustomAgentDefinitionInput) -> tuple[dict[str, Any], str, str]:
    """渲染 AGENT.md、公开能力元数据和唯一授权 permissions.yaml。"""

    metadata: dict[str, Any] = {
        "agent_type": value.agent_key,
        # Agent 的 name 本身就是用户输入名称；额外保留 display_name，使 Agent 与 Skill
        # 的定义结构一致，也让读取定义的调用方不必依赖字段差异。
        "display_name": value.name,
        "name": value.name,
        "version": "internal",
        "description": value.description,
        "accepted_subtask_types": value.accepted_subtask_types,
        "accepted_input_types": value.accepted_input_types,
        "produced_output_types": value.produced_output_types,
        "lifecycle": "ACTIVE",
    }
    front_matter = yaml.safe_dump(metadata, allow_unicode=True, sort_keys=False).strip()
    agent_markdown = f"---\n{front_matter}\n---\n\n{value.instruction_markdown.strip()}\n"
    permissions_yaml = yaml.safe_dump(
        {"agent_id": value.agent_key, "allowed_skills": value.allowed_skill_names},
        allow_unicode=True,
        sort_keys=False,
    )
    return metadata, agent_markdown, permissions_yaml


def stable_json(value: Any) -> str:
    """返回用于 checksum 的稳定 JSON 片段。"""

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
