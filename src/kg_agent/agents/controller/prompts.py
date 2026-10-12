"""加载版本化总控 Prompt，并构造经过裁剪的模型输入。"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from pydantic import BaseModel

from kg_agent.agents.controller.draft_models import TaskPlanDraft
from kg_agent.agents.controller.state import ControllerRequest
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import ArtifactStatus
from kg_agent.domain.task_models import AgentSubTask, TaskSpec
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.skill_registry import SkillRegistry, skill_input_type_groups
from kg_agent.validators.artifact_reuse_validator import is_task_spec_compatible
from kg_agent.validators.subtask_validator import ValidationIssue

# 总控只需从每类最近少量成果中选择复用；完整 Artifact 清单仍由确定性服务保存和校验。
_MAX_REUSE_CANDIDATES_PER_TYPE = 2
_MAX_ARTIFACT_SUMMARY_CHARS = 240


class PromptRepository:
    """从明确目录读取总控 Agent 的版本化 Markdown Prompt。"""

    def __init__(self, prompt_directory: Path) -> None:
        """保存 Prompt 目录，不在初始化时执行文件写入。"""

        self._prompt_directory = prompt_directory

    def load_system_prompt(self) -> str:
        """读取总控 Agent v1 系统指令。"""

        return self._read("system.v1.md")

    def load_decomposition_prompt(self) -> str:
        """读取总控 Agent v1 拆解任务指令。"""

        return self._read("decomposition.v1.md")

    def _read(self, filename: str) -> str:
        """按固定文件名读取 UTF-8 Prompt。"""

        return (self._prompt_directory / filename).read_text(encoding="utf-8")


def build_decomposition_prompt(
    *,
    request: ControllerRequest,
    current_context: SharedTaskContext | None,
    known_subtasks: list[AgentSubTask],
    registry: AgentRegistry,
    skill_registry: SkillRegistry | None = None,
    task_spec_history: dict[int, TaskSpec] | None = None,
    excluded_reuse_artifact_ids: set[UUID] | None = None,
) -> str:
    """构造只包含总控 Agent 所需事实的任务拆解提示。

    继续或修改任务时，完整 Artifact 清单可能随执行轮次持续增长。这里不再把同一
    清单通过 SharedContext 和 ``provided_artifacts`` 重复注入模型，而是只投影当前
    TaskSpec 下可复用的少量候选；最终复用仍必须经过 ArtifactReuseValidator。
    """

    current_view: dict[str, Any] | None = None
    excluded_ids = excluded_reuse_artifact_ids or set()
    current_artifact_ids: set[UUID] = set()
    if current_context is not None:
        current_artifact_ids = {item.artifact_id for item in current_context.artifact_refs}
        current_view = {
            "task_id": str(current_context.task_id),
            "context_version": current_context.version,
            "conversation_summary": current_context.conversation.summary,
            "master_task": {
                "status": current_context.master_task.status.value,
                "current_task_spec_revision": (
                    current_context.master_task.current_task_spec_revision
                ),
                "current_decomposition_revision": (
                    current_context.master_task.current_decomposition_revision
                ),
            },
            "current_task_spec": _task_spec_prompt_view(current_context.current_task_spec),
            "known_subtasks": [_subtask_prompt_view(item) for item in known_subtasks],
            "human_review_request_ids": [
                str(item) for item in current_context.human_review_request_ids
            ],
            # 最近五轮公开对话已经由 conversation_history 单独提供。这里只保留
            # 非对话型任务备注，避免同一 USER/ASSISTANT 内容重复消耗 Prompt。
            "short_term_task_notes": [
                item.model_dump(mode="json")
                for item in current_context.short_term_memories
                if not any(ref.startswith("conversation-role:") for ref in item.source_refs)
            ][-3:],
            # 这里只让模型看见可选择的候选，而不是把所有历史元数据、血缘和 checksum
            # 反复塞入 Prompt。Artifact ID 之外的复用合法性由确定性服务负责。
            "reusable_artifact_candidates": _select_reuse_candidates(
                current_context.artifact_refs,
                current_task_spec=current_context.current_task_spec,
                task_spec_history=task_spec_history or {},
                excluded_artifact_ids=excluded_ids,
            ),
        }

    # ``existing_artifacts`` 在续办时包含 SharedContext 的同一批 Artifact，以及可能
    # 新上传的输入。前者已经以候选形式给出，因此这里只保留当前请求新增的输入，避免
    # 23 个历史产物在 Prompt 中出现两次。
    newly_provided_artifacts = [
        item for item in request.existing_artifacts if item.artifact_id not in current_artifact_ids
    ]

    payload = {
        "current_time": datetime.now(UTC).isoformat(),
        "operation": request.operation,
        "operation_constraints": _operation_constraints(request, current_context),
        "requested_task_id": str(request.task_id),
        "conversation_id": str(request.conversation_id),
        "user_message": request.user_message,
        "conversation_history": request.conversation_history,
        "recalled_long_term_memories": [
            item.model_dump(mode="json") for item in request.recalled_long_term_memories
        ],
        "current_context_view": current_view,
        # 总控只需知道最近两版目标差异；传递完整历史会在 MODIFY/失败返工后持续
        # 放大提示词，且不增加本轮可执行计划的事实信息。
        "task_spec_history": {
            str(revision): _task_spec_prompt_view(spec)
            for revision, spec in sorted((task_spec_history or {}).items())[-2:]
        },
        "provided_artifacts": [
            _provided_artifact_prompt_view(item) for item in newly_provided_artifacts
        ],
        "agent_catalog": [
            {
                "agent_type": item.agent_type,
                "name": item.name,
                # 总控必须依据该字段选择合法 subtask_type；省略它会让模型只能猜测
                # 专业 Agent 的职责，随后被 SubTaskValidator 全量拒绝。
                "accepted_subtask_types": item.accepted_subtask_types,
            }
            for item in registry.list_all()
        ],
        # Skill 契约由 Registry 自动投影，不在 Prompt Markdown 中维护第二份规则。
        "subtask_contract_catalog": (
            [
                {
                    "skill_name": skill.name,
                    "skill_display_name": skill.display_name or skill.name,
                    "subtask_types": skill.supported_subtask_types,
                    "owner_agent": skill.owner_agent,
                    "required_input_types": [
                        " 或 ".join(group) for group in skill_input_type_groups(skill)
                    ],
                    "atomic_output_types": skill.output_artifact_types,
                }
                for skill in skill_registry.list_all()
                if skill.lifecycle == "ACTIVE"
            ]
            if skill_registry is not None
            else []
        ),
        # 模型只输出语义计划，完整公共契约由运行时确定性编译。
        "output_contract": _decomposition_output_contract(),
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


def _operation_constraints(
    request: ControllerRequest,
    current_context: SharedTaskContext | None,
) -> dict[str, Any]:
    """为内部最终验收提供不可歧义的顶层意图和 TaskSpec 约束。"""

    if request.operation == "SYSTEM_CONTINUATION":
        return {
            "mode": "SYSTEM_CONTINUATION",
            "required_top_level_task_intent": "CONTINUE",
            "must_preserve_current_task_spec_exactly": True,
            "current_task_spec": (
                _task_spec_prompt_view(current_context.current_task_spec)
                if current_context is not None
                else None
            ),
            "instruction": (
                "这是审核修改、失败恢复等系统续办，不是新用户任务。顶层 task_intent "
                "必须为 CONTINUE，task_spec 只需保持当前业务语义；运行时会精确沿用当前"
                "TaskSpec 的 ID、revision 和其他系统事实。按照系统消息"
                "发布完成续办所需的子任务，不得误判为 START 或直接当作最终完成。"
            ),
        }
    if request.operation != "FINAL_VERIFICATION":
        return {
            "mode": "USER_TURN",
            "instruction": "根据用户消息和当前上下文判断 START、CONTINUE 或 MODIFY。",
        }
    return {
        "mode": "FINAL_VERIFICATION",
        "required_top_level_task_intent": "CONTINUE",
        "must_preserve_current_task_spec_exactly": True,
        "current_task_spec": (
            _task_spec_prompt_view(current_context.current_task_spec)
            if current_context is not None
            else None
        ),
        "instruction": (
            "这是协调器发起的内部最终验收，不是新用户任务。顶层 task_intent 必须为 "
            "CONTINUE，task_spec 只需保持当前业务语义，运行时会精确沿用当前 TaskSpec。"
            "目标已完成时返回空 subtasks；仅在确有缺失或失败时"
            "发布必要修复子任务，不得改写 TaskSpec。"
        ),
    }


def build_repair_prompt(
    *,
    base_prompt: str,
    issues: list[ValidationIssue],
    previous_result: BaseModel | None,
    forbidden_reuse_artifact_ids: set[UUID] | None = None,
) -> str:
    """把确定性校验错误加入提示，要求模型返回完整修复结果。"""

    repair_payload = {
        "validation_issues": [item.model_dump(mode="json") for item in issues],
        "previous_result": (
            previous_result.model_dump(mode="json") if previous_result is not None else None
        ),
        # 已明确不兼容的 ID 不应被模型从上一版草案复制回去；本轮 Prompt 的候选区
        # 也不会再展示它们。
        "forbidden_reuse_artifact_ids": [
            str(item) for item in sorted(forbidden_reuse_artifact_ids or set(), key=str)
        ],
        "repair_instruction": "只修复列出的问题，并重新返回完整 TaskPlanDraft JSON。",
    }
    repair_json = json.dumps(repair_payload, ensure_ascii=False, indent=2)
    return f"{base_prompt}\n\n修复信息：\n{repair_json}"


def _select_reuse_candidates(
    artifact_refs: list[ArtifactRef],
    *,
    current_task_spec: TaskSpec,
    task_spec_history: dict[int, TaskSpec],
    excluded_artifact_ids: set[UUID],
) -> list[dict[str, Any]]:
    """按类型保留当前规格下可复用的最近 Artifact 候选。

    这是 Prompt 预算和模型选择范围的预筛选，而不是复用授权：父级血缘、checksum、
    Schema 与权威存储状态仍在 ArtifactReuseValidator 中重新检查。当前轮尚未生成新的
    TaskSpec，因此以当前不可变版本筛掉“已知不兼容”的历史产物；用户改变语义范围后，
    最终校验仍可能进一步拒绝该候选。
    """

    selected: list[dict[str, Any]] = []
    count_by_type: dict[str, int] = {}
    compatible = [
        item
        for item in artifact_refs
        if item.status is ArtifactStatus.AVAILABLE
        and item.artifact_id not in excluded_artifact_ids
        and is_task_spec_compatible(
            item,
            task_spec_history.get(item.task_spec_revision),
            current_task_spec,
        )
    ]
    for item in sorted(compatible, key=lambda value: value.created_at, reverse=True):
        current_count = count_by_type.get(item.artifact_type, 0)
        if current_count >= _MAX_REUSE_CANDIDATES_PER_TYPE:
            continue
        selected.append(_reuse_candidate_prompt_view(item))
        count_by_type[item.artifact_type] = current_count + 1
    return selected


def _reuse_candidate_prompt_view(ref: ArtifactRef) -> dict[str, Any]:
    """返回总控选择复用候选所需的最小可读信息。"""

    return {
        "artifact_id": str(ref.artifact_id),
        "artifact_type": ref.artifact_type,
        "task_spec_revision": ref.task_spec_revision,
        "ontology_id": str(ref.ontology_id) if ref.ontology_id else None,
        "ontology_version": ref.ontology_version,
        "content_summary": _short_summary(ref.content_summary),
    }


def _provided_artifact_prompt_view(ref: ArtifactRef) -> dict[str, Any]:
    """返回本轮新输入的最小标识信息，不把存储位置或血缘传给总控。"""

    return {
        "artifact_id": str(ref.artifact_id),
        "artifact_type": ref.artifact_type,
        "content_summary": _short_summary(ref.content_summary),
    }


def _short_summary(summary: str) -> str:
    """限制单个 Artifact 摘要长度，避免异常长摘要重新撑大总控 Prompt。"""

    normalized = " ".join(summary.split())
    if len(normalized) <= _MAX_ARTIFACT_SUMMARY_CHARS:
        return normalized
    return f"{normalized[:_MAX_ARTIFACT_SUMMARY_CHARS]}…"


def _task_spec_prompt_view(spec: TaskSpec) -> dict[str, Any]:
    """向总控提供 TaskSpec 的决策字段，不重复传递时间和固定默认字段。"""

    return {
        "revision": spec.revision,
        "task_intent": spec.task_intent.value,
        "business_goal": spec.business_goal,
        "domain": spec.domain,
        "source_artifact_ids": [str(item) for item in spec.source_artifact_ids],
        "target_output_types": spec.target_output_types,
        "ontology_strategy": spec.ontology_strategy.model_dump(mode="json"),
        "extraction_scope": spec.extraction_scope,
        "constraints": spec.constraints,
    }


def _subtask_prompt_view(subtask: AgentSubTask) -> dict[str, Any]:
    """保留总控继续或返工时需要的子任务状态、依赖与交付物。"""

    return {
        "subtask_id": str(subtask.subtask_id),
        "subtask_type": subtask.subtask_type,
        "assigned_agent_type": subtask.assigned_agent_type,
        "dependency_subtask_ids": [str(item) for item in subtask.dependency_subtask_ids],
        "expected_output_types": subtask.expected_output_types,
        "human_review_required": subtask.human_review_required,
        "status": subtask.status.value,
    }


def _decomposition_output_contract() -> dict[str, Any]:
    """返回轻量语义计划的真实 Pydantic JSON Schema。"""

    return TaskPlanDraft.model_json_schema()
