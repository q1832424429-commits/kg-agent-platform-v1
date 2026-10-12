"""校验总控计划中的 Artifact 交接边界。

本模块只检查引用、类型、生命周期和任务归属，不替 Agent 决定业务流程，也不执行 Skill。
"""

from collections import Counter
from uuid import UUID

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.enums import ArtifactStatus
from kg_agent.exceptions import ArtifactValidationError
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.validators.subtask_validator import SubTaskValidationContext, ValidationIssue


class HandoffValidator:
    """保证交给专业 Agent 的输入是当前任务中可用且注册过的 Artifact。"""

    def __init__(self, artifact_registry: ArtifactRegistry) -> None:
        """注入 Artifact 契约目录，不在总控代码中复制类型清单。"""

        self._registry = artifact_registry

    def validate(
        self,
        result: TaskDecompositionResult,
        context: SubTaskValidationContext,
    ) -> list[ValidationIssue]:
        """返回阻止计划发布的引用冲突、越界和交接契约问题。"""

        issues: list[ValidationIssue] = []
        if result.requires_clarification:
            if result.reused_artifact_ids:
                issues.append(
                    ValidationIssue(
                        code="CLARIFICATION_WITH_REUSE",
                        message="澄清控制结果不能同时声明已执行的 Artifact 复用",
                    )
                )
            return issues

        historical = context.current_context.artifact_refs if context.current_context else []
        artifacts: dict[UUID, ArtifactRef] = {}
        for artifact in [*historical, *context.available_artifacts]:
            previous = artifacts.get(artifact.artifact_id)
            if previous is not None and previous != artifact:
                issues.append(
                    ValidationIssue(
                        code="CONFLICTING_ARTIFACT_REFERENCE",
                        message=f"同一 Artifact ID 出现不同元数据：{artifact.artifact_id}",
                    )
                )
            artifacts[artifact.artifact_id] = artifact

        used_ids = set(result.task_spec.source_artifact_ids)
        for subtask in result.subtasks:
            used_ids.update(subtask.input_artifact_ids)
            self._validate_types(subtask.expected_output_types, issues, subtask.subtask_id)
            for artifact_id in subtask.input_artifact_ids:
                self._validate_reference(
                    artifact_id, artifacts, context, issues, subtask.subtask_id
                )
        self._validate_types(result.task_spec.target_output_types, issues)
        for artifact_id in result.task_spec.source_artifact_ids:
            self._validate_reference(artifact_id, artifacts, context, issues)

        reused_ids = set(result.reused_artifact_ids)
        if len(reused_ids) != len(result.reused_artifact_ids):
            issues.append(ValidationIssue(code="DUPLICATE_REUSE_ID", message="复用列表中 ID 重复"))
        for artifact_id in reused_ids:
            self._validate_reference(artifact_id, artifacts, context, issues)
            if artifact_id not in used_ids:
                issues.append(
                    ValidationIssue(
                        code="UNUSED_REUSED_ARTIFACT",
                        message=f"声明复用的 Artifact 未被任务使用：{artifact_id}",
                    )
                )

        # 历史输入不能通过省略 reused_artifact_ids 绕过 ArtifactReuseService。
        historical_ids = {item.artifact_id for item in historical}
        for subtask in result.subtasks:
            undeclared = set(subtask.input_artifact_ids) & historical_ids - reused_ids
            for artifact_id in sorted(undeclared, key=str):
                issues.append(
                    ValidationIssue(
                        code="UNDECLARED_ARTIFACT_REUSE",
                        message=f"使用历史 Artifact 时必须声明复用：{artifact_id}",
                        subtask_id=subtask.subtask_id,
                    )
                )
            if any(count > 1 for count in Counter(subtask.input_artifact_ids).values()):
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_INPUT_ARTIFACT",
                        message="同一子任务不能重复引用同一个输入 Artifact",
                        subtask_id=subtask.subtask_id,
                    )
                )
        return issues

    def _validate_reference(
        self,
        artifact_id: UUID,
        artifacts: dict[UUID, ArtifactRef],
        context: SubTaskValidationContext,
        issues: list[ValidationIssue],
        subtask_id: UUID | None = None,
    ) -> None:
        """检查一条输入引用的存在性、任务隔离、状态和 Schema 版本。"""

        artifact = artifacts.get(artifact_id)
        if artifact is None:
            issues.append(
                ValidationIssue(
                    code="UNKNOWN_ARTIFACT_REFERENCE",
                    message=f"Artifact 引用不存在：{artifact_id}",
                    subtask_id=subtask_id,
                )
            )
            return
        if artifact.task_id != context.task_id:
            issues.append(
                ValidationIssue(
                    code="CROSS_TASK_ARTIFACT",
                    message=f"禁止引用其他 MasterTask 的 Artifact：{artifact_id}",
                    subtask_id=subtask_id,
                )
            )
        if artifact.status is not ArtifactStatus.AVAILABLE:
            issues.append(
                ValidationIssue(
                    code="ARTIFACT_NOT_AVAILABLE",
                    message=f"Artifact 当前不可用：{artifact_id}",
                    subtask_id=subtask_id,
                )
            )
        try:
            registration = self._registry.require(artifact.artifact_type)
        except ArtifactValidationError:
            issues.append(
                ValidationIssue(
                    code="UNREGISTERED_ARTIFACT_TYPE",
                    message=f"Artifact 类型未注册：{artifact.artifact_type}",
                    subtask_id=subtask_id,
                )
            )
        else:
            if artifact.schema_version != registration.schema_version:
                issues.append(
                    ValidationIssue(
                        code="UNSUPPORTED_ARTIFACT_SCHEMA",
                        message=f"Artifact Schema 版本不兼容：{artifact_id}",
                        subtask_id=subtask_id,
                    )
                )

    def _validate_types(
        self,
        artifact_types: list[str],
        issues: list[ValidationIssue],
        subtask_id: UUID | None = None,
    ) -> None:
        """检查声明输出是否存在正式 Artifact 契约。"""

        for artifact_type in artifact_types:
            try:
                self._registry.require(artifact_type)
            except ArtifactValidationError:
                issues.append(
                    ValidationIssue(
                        code="UNREGISTERED_OUTPUT_TYPE",
                        message=f"输出 Artifact 类型未注册：{artifact_type}",
                        subtask_id=subtask_id,
                    )
                )
