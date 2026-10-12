"""把总控 Agent 的复用建议转换为确定性的 ArtifactReuseRequest。

LLM 只提出 ID；Schema、TaskSpec 影响、本体和父输入 checksum 的要求由本模块计算。
TaskSpec.reuse_policy 中的自然语言建议不会覆盖这些门禁。
"""

from typing import Protocol

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRef, ArtifactReuseDecision, ArtifactReuseRequest
from kg_agent.domain.task_models import TaskSpec
from kg_agent.exceptions import ArtifactValidationError
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.services.artifact_reuse_service import ArtifactReuseService
from kg_agent.validators.subtask_validator import SubTaskValidationContext, ValidationIssue


def is_task_spec_compatible(
    ref: ArtifactRef,
    old: TaskSpec | None,
    new: TaskSpec,
) -> bool:
    """保守判断 Artifact 在两个 TaskSpec 版本之间是否仍具备复用语义。

    此函数只判断 TaskSpec 对产物语义的影响，不替代 ArtifactReuseService 对状态、
    Schema、本体、父级血缘和 checksum 的权威校验。总控构造提示词时也复用同一规则
    预筛选候选项，避免把已经明确不兼容的历史产物再次交给模型选择。
    """

    if ref.task_spec_revision == new.revision:
        return True
    if ref.artifact_type in {"RawDocument", "ExternalOntology", "StructuredDataset"}:
        return (
            ref.artifact_id in new.source_artifact_ids
            or ref.artifact_id == new.ontology_strategy.external_ontology_artifact_id
        )
    if ref.artifact_type == "PublishedOntology":
        return (
            ref.ontology_id is not None
            and ref.ontology_id == new.ontology_strategy.selected_ontology_id
            and ref.ontology_version == new.ontology_strategy.selected_ontology_version
        )
    if old is None:
        return False
    if set(old.source_artifact_ids) != set(new.source_artifact_ids) or old.domain != new.domain:
        return False

    # 文档类产物只受原始输入和领域理解影响；后续是否抽取、对齐、构图或发布
    # 是消费方流程，不能让已完成的解析结果在每轮目标扩展时失效。
    if ref.artifact_type in {
        "DocumentManifest",
        "ParsedDocument",
        "SemanticDocument",
    }:
        return True

    # 本任务从文档建设并发布本体后，实例/图谱 Artifact 会绑定真实 ontology_id，
    # 但 TaskSpec 仍保持 BUILD_FROM_DOCUMENTS。纯续办发布时允许在同一来源和语义
    # 范围内复用这些不可变结果；显式改选已发布本体则必须严格匹配 ID 与版本。
    if ref.ontology_id is not None:
        if new.ontology_strategy.mode == "REUSE_PUBLISHED":
            if (
                new.ontology_strategy.selected_ontology_id != ref.ontology_id
                or new.ontology_strategy.selected_ontology_version != ref.ontology_version
            ):
                return False
        elif not (
            old.ontology_strategy.mode == "BUILD_FROM_DOCUMENTS"
            and new.ontology_strategy.mode == "BUILD_FROM_DOCUMENTS"
        ):
            return False

    # 这些字段只表示本轮要执行到哪个阶段，不描述已生成数据集的语义内容。
    # 去掉它们后仍有其他抽取参数变化（例如实体类型过滤）时继续保守拒绝。
    workflow_scope_keys = {
        "parse_document",
        "knowledge_extract",
        "requires_alignment_fusion",
        "requires_relation_alignment",
    }
    old_semantic_scope = {
        key: value for key, value in old.extraction_scope.items() if key not in workflow_scope_keys
    }
    new_semantic_scope = {
        key: value for key, value in new.extraction_scope.items() if key not in workflow_scope_keys
    }
    return old_semantic_scope == new_semantic_scope


class ArtifactReuseEvaluator(Protocol):
    """约束内存测试或 PostgreSQL 适配器提供的复用判定入口。"""

    async def evaluate(self, request: ArtifactReuseRequest) -> ArtifactReuseDecision:
        """读取权威 Artifact 元数据并返回兼容性判定。"""
        ...


class ArtifactReuseValidator:
    """逐个审批复用建议，并把拒绝原因反馈给总控 Agent 修复。"""

    def __init__(
        self,
        artifact_registry: ArtifactRegistry,
        evaluator: ArtifactReuseEvaluator | None = None,
    ) -> None:
        """注入契约目录和可选的正式存储校验入口。"""

        self._registry = artifact_registry
        self._evaluator = evaluator

    async def validate(
        self,
        result: TaskDecompositionResult,
        context: SubTaskValidationContext,
        task_spec_history: dict[int, TaskSpec],
    ) -> list[ValidationIssue]:
        """根据历史 TaskSpec 和当前引用计算复用要求；不信任模型自报兼容性。"""

        if result.requires_clarification:
            return []
        historical = context.current_context.artifact_refs if context.current_context else []
        artifacts = {item.artifact_id: item for item in [*historical, *context.available_artifacts]}
        issues: list[ValidationIssue] = []
        for artifact_id in dict.fromkeys(result.reused_artifact_ids):
            ref = artifacts.get(artifact_id)
            if ref is None:
                continue  # 引用存在性由 HandoffValidator 给出稳定错误。
            try:
                registration = self._registry.require(ref.artifact_type)
            except ArtifactValidationError:
                continue
            old_spec = task_spec_history.get(ref.task_spec_revision)
            compatible = self._compatible(ref, old_spec, result.task_spec)
            parents = [artifacts[item] for item in ref.parent_artifact_ids if item in artifacts]
            strategy = result.task_spec.ontology_strategy
            # 无本体依赖的文档不被本体选择误伤；有本体血缘的结果必须匹配目标版本。
            expected_ontology_id = ref.ontology_id
            expected_ontology_version = ref.ontology_version
            if ref.ontology_id is not None and strategy.selected_ontology_id is not None:
                expected_ontology_id = strategy.selected_ontology_id
                expected_ontology_version = strategy.selected_ontology_version
            request = ArtifactReuseRequest(
                artifact_id=artifact_id,
                expected_artifact_type=ref.artifact_type,
                expected_schema_version=registration.schema_version,
                compatible_task_spec_revisions={ref.task_spec_revision} if compatible else set(),
                expected_parent_checksums={item.checksum for item in parents},
                expected_ontology_id=expected_ontology_id,
                expected_ontology_version=expected_ontology_version,
            )
            if self._evaluator is None:
                decision = ArtifactReuseService.evaluate_snapshot(request, ref, parents)
            else:
                decision = await self._evaluator.evaluate(request)
            for reason in decision.reason_codes:
                issues.append(
                    ValidationIssue(
                        code=f"REUSE_{reason}",
                        message=f"Artifact {artifact_id} 不可复用：{reason}",
                    )
                )
            if not decision.reusable and not decision.reason_codes:
                issues.append(
                    ValidationIssue(
                        code="ARTIFACT_REUSE_REJECTED",
                        message=f"Artifact 复用校验拒绝：{artifact_id}",
                    )
                )
        return issues

    @staticmethod
    def _compatible(ref: ArtifactRef, old: TaskSpec | None, new: TaskSpec) -> bool:
        """保守计算 revision 影响，不让 LLM 声明任意历史版本兼容。

        原始输入的内容不可变且仍被明确选用时可保留；已发布本体按 ID/版本匹配。
        生成结果跨 revision 按 Artifact 的真实上游影响字段判断，而不是比较整份
        TaskSpec。业务目标、最终输出、流程开关或文字约束发生变化，不会改变已经生成
        Artifact 的内容；例如先抽取实体关系、下一轮再构图时，应复用已有数据集。
        """
        return is_task_spec_compatible(ref, old, new)
