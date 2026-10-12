"""确定性校验总控 Agent 产生的 TaskSpec 和 AgentSubTask。

本校验器不会重新规划任务；它只返回结构化错误，供总控 Agent 修复或终止发布。
"""

from collections import Counter
from uuid import UUID

from pydantic import BaseModel, Field

from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import SubTaskStatus, TaskIntent
from kg_agent.domain.task_models import AgentSubTask
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.skill_registry import SkillRegistry, missing_skill_input_types


class ValidationIssue(BaseModel):
    """描述一个阻止任务拆解结果发布的确定性问题。"""

    code: str
    message: str
    subtask_id: UUID | None = None


class SubTaskValidationContext(BaseModel):
    """保存校验当前拆解结果所需的最小事实。"""

    task_id: UUID
    user_message: str = ""
    current_context: SharedTaskContext | None = None
    available_artifacts: list[ArtifactRef] = Field(default_factory=list)
    known_subtask_ids: set[UUID] = Field(default_factory=set)
    known_subtask_statuses: dict[UUID, SubTaskStatus] = Field(default_factory=dict)


class SubTaskValidator:
    """检查任务意图、Agent 能力、依赖和人工审核门禁。"""

    _PLANNABLE_STATUSES = {
        SubTaskStatus.PENDING,
        SubTaskStatus.BLOCKED,
        SubTaskStatus.READY,
    }

    def __init__(
        self,
        agent_registry: AgentRegistry,
        skill_registry: SkillRegistry | None = None,
    ) -> None:
        """绑定专业 Agent 能力目录及可选的 Skill 可执行性事实来源。"""

        self._agent_registry = agent_registry
        self._skill_registry = skill_registry

    def validate(
        self,
        result: TaskDecompositionResult,
        context: SubTaskValidationContext,
    ) -> list[ValidationIssue]:
        """返回候选拆解结果中的全部可确定检查问题。"""

        issues: list[ValidationIssue] = []
        self._validate_task_header(result, context, issues)

        if result.requires_clarification:
            if result.subtasks:
                issues.append(
                    ValidationIssue(
                        code="CLARIFICATION_WITH_SUBTASKS",
                        message="请求澄清时不能同时发布子任务",
                    )
                )
            return issues

        self._validate_ontology_strategy(result, context, issues)
        self._validate_user_goal_boundary(result, context, issues)
        self._validate_subtasks(result, context, issues)
        self._validate_dependencies(result.subtasks, context.known_subtask_ids, issues)
        self._validate_review_gates(result.subtasks, context, issues)
        self._validate_ontology_prerequisites(result.subtasks, context, issues)
        self._validate_extraction_input_prerequisites(result.subtasks, context, issues)
        self._validate_skill_executability(result.subtasks, context, issues)
        return issues

    def _validate_skill_executability(
        self,
        subtasks: list[AgentSubTask],
        context: SubTaskValidationContext,
        issues: list[ValidationIssue],
    ) -> None:
        """确保每个完整计划节点都能由所属 Agent 的 Skill 真实完成。

        校验以显式输入和全部依赖祖先的输出类型为起点，在同一个专业 Agent 的
        allowlist 内计算可执行 Skill 闭包。Human Review 不是 Skill，因此带审核标记
        的候选本体祖先或图谱质量祖先向后续发布任务提供计划态 Approval 类型。
        """

        if self._skill_registry is None:
            return
        artifact_types = self._artifact_types(context)
        by_id = {item.subtask_id: item for item in subtasks}
        for subtask in subtasks:
            ancestors = self._ancestor_ids(subtask, by_id)
            reachable = {
                artifact_types[artifact_id]
                for artifact_id in subtask.input_artifact_ids
                if artifact_id in artifact_types
            }
            for ancestor_id in ancestors:
                ancestor = by_id.get(ancestor_id)
                if ancestor is None:
                    continue
                reachable.update(ancestor.expected_output_types)
                reachable.update(
                    artifact_types[artifact_id]
                    for artifact_id in ancestor.input_artifact_ids
                    if artifact_id in artifact_types
                )
                if ancestor.human_review_required:
                    if set(ancestor.expected_output_types).intersection(
                        {"CandidateOntology", "FusedCandidateOntology"}
                    ):
                        reachable.add("OntologyApproval")
                    elif ancestor.subtask_type == "graph_quality_evaluate":
                        reachable.add("GraphApproval")

            registration = self._agent_registry.get(subtask.assigned_agent_type)
            if registration is None:
                continue
            # 一个 Agent 的 allowlist 只是权限上界，不代表该 Agent 可以在任意子任务
            # 中调用其中的所有 Skill。执行性推演必须限定为当前 subtask_type 的候选
            # Skill；否则会把“生成变更集”与“应用变更集”等不同职责错误串在一起。
            allowed_skills = [
                self._skill_registry.require(name) for name in registration.allowed_skills
            ]
            registered_skills = [
                skill
                for skill in allowed_skills
                if subtask.subtask_type in skill.supported_subtask_types
            ]
            remaining_skills = [skill for skill in registered_skills if skill.lifecycle == "ACTIVE"]

            # 某些 Skill 的输出是后续执行或人工审核前的独立 Artifact。它们不能与任何
            # 后续应用/发布结果声明在同一 AgentSubTask 中，否则专业 Agent 无法在
            # 运行时把尚未持久化的新 Artifact 重新作为下一次 Skill 的输入，也会绕开
            # 用户对草案的可见审核。该规则由 Skill 元数据声明，而不硬编码具体名称。
            dedicated_skills = [
                skill for skill in remaining_skills if skill.requires_dedicated_subtask
            ]
            expected_outputs = set(subtask.expected_output_types)
            for skill in dedicated_skills:
                required_outputs = set(skill.output_artifact_types)
                if expected_outputs != required_outputs:
                    issues.append(
                        ValidationIssue(
                            code="DEDICATED_SKILL_OUTPUT_MISMATCH",
                            message=(
                                f"{subtask.subtask_type} 必须作为 {skill.name} 的独立子任务，"
                                f"expected_output_types 必须且只能为 {sorted(required_outputs)}；"
                                "请将后续产物拆成依赖此子任务的独立 AgentSubTask"
                            ),
                            subtask_id=subtask.subtask_id,
                        )
                    )
                # 同一 subtask_type 同时存在多个独占 Skill 时无法安全选择，属于
                # Registry 配置错误；保守地阻止总控发布该计划。
            if len(dedicated_skills) > 1:
                issues.append(
                    ValidationIssue(
                        code="AMBIGUOUS_DEDICATED_SKILL",
                        message=(
                            f"{subtask.subtask_type} 存在多个要求独占子任务的 Skill："
                            f"{sorted(skill.name for skill in dedicated_skills)}"
                        ),
                        subtask_id=subtask.subtask_id,
                    )
                )
            produced: set[str] = set()
            changed = True
            while changed:
                changed = False
                for skill in remaining_skills:
                    if not missing_skill_input_types(skill, reachable):
                        new_types = set(skill.output_artifact_types) - reachable
                        produced.update(skill.output_artifact_types)
                        if new_types:
                            reachable.update(new_types)
                            changed = True
            missing = set(subtask.expected_output_types) - produced
            if missing:
                # 仅报告“无法生成目标输出”不足以指导模型修复。这里进一步列出能够
                # 生成目标输出的候选 Skill 及其输入缺口，使总控可以明确补充上游
                # AgentSubTask 的 expected_output_types 或依赖边。
                candidate_input_gaps = {
                    skill.name: missing_skill_input_types(skill, reachable)
                    for skill in remaining_skills
                    if missing.intersection(skill.output_artifact_types)
                }
                if candidate_input_gaps:
                    gap_detail = f"；候选 Skill 输入缺口={candidate_input_gaps}"
                else:
                    gap_detail = "；所属 Agent 没有能生成这些输出的 ACTIVE Skill"
                issues.append(
                    ValidationIssue(
                        code="UNEXECUTABLE_SUBTASK_PLAN",
                        message=(
                            f"{subtask.subtask_type} 的所属 Agent 无法利用当前输入或依赖"
                            f"祖先产物生成 {sorted(missing)}；请补充缺失的前置 AgentSubTask"
                            f"或修正预期输出{gap_detail}"
                        ),
                        subtask_id=subtask.subtask_id,
                    )
                )

    def _validate_user_goal_boundary(
        self,
        result: TaskDecompositionResult,
        context: SubTaskValidationContext,
        issues: list[ValidationIssue],
    ) -> None:
        """阻止总控把抽取目标擅自扩大为构图或发布。

        该检查只实施保守的副作用和范围上界，不代替 LLM 理解业务目标：总控仍负责
        识别用户要抽取哪些知识。规则只在用户没有表达构图/发布目标时拒绝明显越界项，
        并把结构化问题交回同一个总控 Agent 修复。
        """

        message = self._latest_user_message(context.user_message)
        normalized = "".join(message.lower().split())
        explicitly_forbids_publish = any(
            phrase in normalized
            for phrase in (
                "不发布",
                "不要发布",
                "无需发布",
                "不用发布",
                "不入库",
                "不要入库",
                "无需入库",
                "不用入库",
                "只抽取",
                "仅抽取",
            )
        )
        explicitly_requests_publish = not explicitly_forbids_publish and any(
            phrase in normalized
            for phrase in (
                "发布图谱",
                "图谱发布",
                "确认入库",
                "图谱入库",
                "并入库",
                "入图",
                "写入图谱",
            )
        )
        # “抽取实体关系并入库”没有再次写出“图谱”二字，但在本平台语境中明确表示
        # 把实例知识形成并发布为图谱。不能因为模型只看到“抽取”就降级成数据集交付。
        if not explicitly_forbids_publish and "入库" in normalized:
            knowledge_subjects = ("实体", "关系", "三元组", "知识", "图谱")
            ontology_only = "本体" in normalized and not any(
                subject in normalized for subject in knowledge_subjects
            )
            if not ontology_only and any(subject in normalized for subject in knowledge_subjects):
                explicitly_requests_publish = True
        requests_graph_build = any(
            phrase in normalized
            for phrase in (
                "构建图谱",
                "构建知识图谱",
                "生成图谱",
                "形成图谱",
                "候选图谱",
                "图谱融合",
                "构图",
            )
        )
        extraction_only = (
            any(phrase in normalized for phrase in ("抽取", "提取", "识别"))
            and not requests_graph_build
            and not explicitly_requests_publish
        )

        # 领域知识实例由实体、关系和属性共同组成。用户请求实体、关系或三元组抽取时，
        # 默认也要求形成 PropertyDataset，避免后续图谱只保留无属性的骨架。只有用户
        # 明确排除属性时，才允许省略属性抽取。
        explicitly_requests_knowledge_extraction = any(
            phrase in normalized for phrase in ("抽取", "提取", "识别")
        ) and any(subject in normalized for subject in ("实体", "关系", "三元组"))
        explicitly_forbids_properties = any(
            phrase in normalized
            for phrase in (
                "不抽属性",
                "不提取属性",
                "不要属性",
                "无需属性",
                "忽略属性",
            )
        )
        has_property_output = "PropertyDataset" in result.task_spec.target_output_types or any(
            "PropertyDataset" in item.expected_output_types for item in result.subtasks
        )
        if (
            explicitly_requests_knowledge_extraction
            and not explicitly_forbids_properties
            and not has_property_output
        ):
            issues.append(
                ValidationIssue(
                    code="MISSING_PROPERTY_EXTRACTION",
                    message=(
                        "用户要求实体、关系或三元组抽取时，计划必须同时包含 "
                        "PropertyDataset；如用户明确要求不抽属性才可省略"
                    ),
                )
            )

        has_graph_publish = "PublishedGraph" in result.task_spec.target_output_types or any(
            item.subtask_type == "graph_publish" for item in result.subtasks
        )
        previously_authorized_publish = (
            context.current_context is not None
            and "PublishedGraph" in context.current_context.current_task_spec.target_output_types
        )
        if has_graph_publish and not (explicitly_requests_publish or previously_authorized_publish):
            issues.append(
                ValidationIssue(
                    code="EXPLICIT_GRAPH_PUBLISH_REQUIRED",
                    message=(
                        "用户当前消息未明确要求发布、入库或入图，不能把目标扩大为 "
                        "PublishedGraph 或创建 graph_publish 子任务"
                    ),
                )
            )

        if (
            explicitly_requests_publish
            and "PublishedGraph" not in result.task_spec.target_output_types
        ):
            issues.append(
                ValidationIssue(
                    code="EXPLICIT_GRAPH_PUBLISH_TARGET_MISSING",
                    message=(
                        "用户当前明确要求将实体、关系或图谱入库，TaskSpec 最终目标必须包含 "
                        "PublishedGraph；完整 Agent 计划不能删除或缩短最终目标"
                    ),
                )
            )

        if not extraction_only:
            return
        graph_output_types = {
            "CandidateGraph",
            "GraphQualityReport",
            "GraphChangeSet",
            "GraphApproval",
            "PublishedGraph",
        }
        graph_subtask_types = {
            "graph_build",
            "graph_quality_evaluate",
            "graph_changeset_apply",
            "graph_publish",
        }
        expanded_outputs = graph_output_types.intersection(result.task_spec.target_output_types)
        expanded_subtasks = {
            item.subtask_type
            for item in result.subtasks
            if item.subtask_type in graph_subtask_types
        }
        if expanded_outputs or expanded_subtasks:
            issues.append(
                ValidationIssue(
                    code="EXTRACTION_SCOPE_EXPANDED_TO_GRAPH",
                    message=(
                        "用户当前只要求抽取，不能增加候选图谱、图谱质量评估或图谱发布；"
                        f"越界目标={sorted(expanded_outputs)}，越界子任务={sorted(expanded_subtasks)}"
                    ),
                )
            )

    @staticmethod
    def _latest_user_message(message: str) -> str:
        """从生产多轮包装文本中提取当前用户消息，避免历史目标误授权副作用。"""

        marker = "当前用户消息："
        return message.rsplit(marker, maxsplit=1)[-1] if marker in message else message

    def _validate_task_header(
        self,
        result: TaskDecompositionResult,
        context: SubTaskValidationContext,
        issues: list[ValidationIssue],
    ) -> None:
        """校验任务标识、意图和 TaskSpec revision。"""

        if result.task_spec.task_id != context.task_id:
            issues.append(
                ValidationIssue(code="TASK_ID_MISMATCH", message="TaskSpec.task_id 与请求不一致")
            )
        current = context.current_context
        unchanged_continue = (
            result.task_intent is TaskIntent.CONTINUE
            and current is not None
            and result.task_spec == current.current_task_spec
        )
        if result.task_spec.task_intent is not result.task_intent and not unchanged_continue:
            issues.append(
                ValidationIssue(code="INTENT_MISMATCH", message="TaskSpec 与拆解结果意图不一致")
            )

        if result.requires_clarification and current is None:
            return
        if result.task_intent is TaskIntent.START:
            if current is not None:
                issues.append(
                    ValidationIssue(
                        code="START_WITH_CURRENT_TASK", message="START 不应覆盖当前任务"
                    )
                )
            if result.task_spec.revision != 1:
                issues.append(
                    ValidationIssue(
                        code="INVALID_START_REVISION", message="START revision 必须为 1"
                    )
                )
            return

        if current is None:
            issues.append(
                ValidationIssue(
                    code="MISSING_CURRENT_TASK",
                    message=f"{result.task_intent.value} 必须存在当前任务",
                )
            )
            return

        current_revision = current.current_task_spec.revision
        if result.task_spec.revision == current_revision and not unchanged_continue:
            issues.append(
                ValidationIssue(
                    code="IMMUTABLE_TASK_SPEC_REVISION",
                    message=(
                        "沿用 revision 时必须原样使用当前 TaskSpec；修改内容必须创建新 revision"
                    ),
                )
            )
        if (
            result.task_intent is TaskIntent.MODIFY
            and result.task_spec.revision != current_revision + 1
        ):
            issues.append(
                ValidationIssue(
                    code="INVALID_MODIFY_REVISION",
                    message="MODIFY 必须创建下一个 TaskSpec revision",
                )
            )
        if result.task_intent is TaskIntent.CONTINUE and result.task_spec.revision not in {
            current_revision,
            current_revision + 1,
        }:
            issues.append(
                ValidationIssue(
                    code="INVALID_CONTINUE_REVISION",
                    message="CONTINUE 只能沿用当前 revision 或创建下一个 revision",
                )
            )

    def _validate_ontology_strategy(
        self,
        result: TaskDecompositionResult,
        context: SubTaskValidationContext,
        issues: list[ValidationIssue],
    ) -> None:
        """校验三种本体来源所需的确定字段和输入引用。"""

        strategy = result.task_spec.ontology_strategy
        artifacts = self._artifact_refs(context)
        available_types = {item.artifact_type for item in artifacts.values()}
        published_ontologies = [
            item for item in artifacts.values() if item.artifact_type == "PublishedOntology"
        ]
        if "ExternalOntology" in available_types and strategy.mode != "IMPORT_EXTERNAL_JSON":
            issues.append(
                ValidationIssue(
                    code="EXTERNAL_ONTOLOGY_STRATEGY_REQUIRED",
                    message="当前输入包含 ExternalOntology，必须使用 IMPORT_EXTERNAL_JSON",
                )
            )
        if (
            not available_types.intersection({"ExternalOntology", "PublishedOntology"})
            and strategy.mode != "BUILD_FROM_DOCUMENTS"
        ):
            issues.append(
                ValidationIssue(
                    code="BUILD_ONTOLOGY_STRATEGY_REQUIRED",
                    message=(
                        "当前没有 ExternalOntology 或 PublishedOntology，必须使用 "
                        "BUILD_FROM_DOCUMENTS"
                    ),
                )
            )
        if strategy.mode == "REUSE_PUBLISHED":
            if strategy.selected_ontology_id is None or not strategy.selected_ontology_version:
                issues.append(
                    ValidationIssue(
                        code="INCOMPLETE_PUBLISHED_ONTOLOGY_SELECTION",
                        message="复用已发布本体必须明确 ontology_id 和 version",
                    )
                )
            matching_ontology = next(
                (
                    item
                    for item in published_ontologies
                    if item.ontology_id == strategy.selected_ontology_id
                    and item.ontology_version == strategy.selected_ontology_version
                ),
                None,
            )
            if matching_ontology is None:
                issues.append(
                    ValidationIssue(
                        code="SELECTED_PUBLISHED_ONTOLOGY_NOT_AVAILABLE",
                        message="复用的已发布本体必须存在于当前任务输入或 SharedContext",
                    )
                )
        elif strategy.mode == "IMPORT_EXTERNAL_JSON":
            artifact_id = strategy.external_ontology_artifact_id
            artifact = artifacts.get(artifact_id) if artifact_id is not None else None
            if artifact is None or artifact.artifact_type != "ExternalOntology":
                issues.append(
                    ValidationIssue(
                        code="MISSING_EXTERNAL_ONTOLOGY",
                        message="导入外部本体必须引用当前输入中的 ExternalOntology Artifact",
                    )
                )

        # 只有 TaskSpec 来源中明确选中的已发布本体才触发复用约束。当前任务自行
        # 发布的本体也会出现在 SharedContext，但它不是用户最初选择的输入；
        # 最终验收必须沿用原始 BUILD_FROM_DOCUMENTS 策略，不能因产物出现而改写。
        selected_published_ontologies = [
            item
            for item in published_ontologies
            if item.artifact_id in result.task_spec.source_artifact_ids
        ]
        if len(selected_published_ontologies) == 1:
            selected = selected_published_ontologies[0]
            if (
                strategy.mode != "REUSE_PUBLISHED"
                or strategy.selected_ontology_id != selected.ontology_id
                or strategy.selected_ontology_version != selected.ontology_version
            ):
                issues.append(
                    ValidationIssue(
                        code="SELECTED_PUBLISHED_ONTOLOGY_MUST_BE_REUSED",
                        message=(
                            "当前已选择 PublishedOntology，TaskSpec 必须使用同版本 REUSE_PUBLISHED"
                        ),
                    )
                )
            unexpected_ontology_tasks = {
                "ontology_extract",
                "ontology_align",
                "ontology_fuse",
                "ontology_changeset_apply",
                "ontology_publish",
            }
            if any(item.subtask_type in unexpected_ontology_tasks for item in result.subtasks):
                issues.append(
                    ValidationIssue(
                        code="UNREQUESTED_ONTOLOGY_REBUILD",
                        message="已选择 PublishedOntology 的实例抽取任务不能重建、修改或发布本体",
                    )
                )

    def _validate_subtasks(
        self,
        result: TaskDecompositionResult,
        context: SubTaskValidationContext,
        issues: list[ValidationIssue],
    ) -> None:
        """校验每个子任务的归属、能力、输入和初始状态。"""

        artifact_types = (
            {
                artifact.artifact_id: artifact.artifact_type
                for artifact in [
                    *context.current_context.artifact_refs,
                    *context.available_artifacts,
                ]
            }
            if context.current_context
            else {
                artifact.artifact_id: artifact.artifact_type
                for artifact in context.available_artifacts
            }
        )

        duplicate_ids = {
            subtask_id
            for subtask_id, count in Counter(
                subtask.subtask_id for subtask in result.subtasks
            ).items()
            if count > 1
        }

        expected_revision = (
            context.current_context.master_task.current_decomposition_revision + 1
            if context.current_context
            else 1
        )
        for subtask in result.subtasks:
            if subtask.subtask_type == "ontology_quality_evaluate":
                issues.append(
                    ValidationIssue(
                        code="ONTOLOGY_QUALITY_EVALUATION_NOT_ALLOWED",
                        message=(
                            "候选本体不执行自动质量评估；请在产生 CandidateOntology 或 "
                            "FusedCandidateOntology 的任务上直接设置 human_review_required=true"
                        ),
                        subtask_id=subtask.subtask_id,
                    )
                )
            if subtask.decomposition_revision != expected_revision:
                issues.append(
                    ValidationIssue(
                        code="INVALID_DECOMPOSITION_REVISION",
                        message=f"新子任务必须使用 decomposition_revision={expected_revision}",
                        subtask_id=subtask.subtask_id,
                    )
                )
            if subtask.subtask_id in context.known_subtask_ids:
                issues.append(
                    ValidationIssue(
                        code="SUBTASK_ID_ALREADY_EXISTS",
                        message="历史子任务只能依赖或复用，不能用同一 ID 再次发布",
                        subtask_id=subtask.subtask_id,
                    )
                )
            for dependency in subtask.dependency_subtask_ids:
                if context.known_subtask_statuses.get(dependency) in {
                    SubTaskStatus.FAILED,
                    SubTaskStatus.CANCELLED,
                    SubTaskStatus.SUPERSEDED,
                }:
                    issues.append(
                        ValidationIssue(
                            code="UNSATISFIABLE_DEPENDENCY",
                            message="不能依赖失败、取消或已被替代的历史子任务",
                            subtask_id=subtask.subtask_id,
                        )
                    )
            if subtask.subtask_id in duplicate_ids:
                issues.append(
                    ValidationIssue(
                        code="DUPLICATE_SUBTASK_ID",
                        message="同一拆解结果中 subtask_id 重复",
                        subtask_id=subtask.subtask_id,
                    )
                )
            if subtask.master_task_id != context.task_id:
                issues.append(
                    ValidationIssue(
                        code="SUBTASK_TASK_ID_MISMATCH",
                        message="子任务 master_task_id 与当前任务不一致",
                        subtask_id=subtask.subtask_id,
                    )
                )
            if subtask.status not in self._PLANNABLE_STATUSES:
                issues.append(
                    ValidationIssue(
                        code="INVALID_INITIAL_STATUS",
                        message="新子任务状态只能是 PENDING、BLOCKED 或 READY",
                        subtask_id=subtask.subtask_id,
                    )
                )

            registration = self._agent_registry.get(subtask.assigned_agent_type)
            if registration is None:
                issues.append(
                    ValidationIssue(
                        code="UNKNOWN_AGENT",
                        message=f"未知专业 Agent：{subtask.assigned_agent_type}",
                        subtask_id=subtask.subtask_id,
                    )
                )
                continue
            if registration.lifecycle != "ACTIVE":
                issues.append(
                    ValidationIssue(
                        code="INACTIVE_AGENT",
                        message="不能向非 ACTIVE 的专业 Agent 分配任务",
                        subtask_id=subtask.subtask_id,
                    )
                )
            if (
                subtask.subtask_type == "ontology_publish" and not registration.can_publish_ontology
            ) or (subtask.subtask_type == "graph_publish" and not registration.can_publish_graph):
                issues.append(
                    ValidationIssue(
                        code="ILLEGAL_PUBLISH_AGENT",
                        message="负责 Agent 未在 Registry 中获得对应发布权限",
                        subtask_id=subtask.subtask_id,
                    )
                )
            if subtask.subtask_type not in registration.accepted_subtask_types:
                issues.append(
                    ValidationIssue(
                        code="UNSUPPORTED_SUBTASK_TYPE",
                        message="负责 Agent 不支持当前 subtask_type",
                        subtask_id=subtask.subtask_id,
                    )
                )
            unsupported_outputs = set(subtask.expected_output_types) - set(
                registration.produced_output_types
            )
            if unsupported_outputs:
                issues.append(
                    ValidationIssue(
                        code="UNSUPPORTED_OUTPUT_TYPE",
                        message=f"负责 Agent 不能产生：{sorted(unsupported_outputs)}",
                        subtask_id=subtask.subtask_id,
                    )
                )

            for artifact_id in subtask.input_artifact_ids:
                artifact_type = artifact_types.get(artifact_id)
                if artifact_type is None:
                    issues.append(
                        ValidationIssue(
                            code="UNKNOWN_INPUT_ARTIFACT",
                            message=f"输入 Artifact 不存在：{artifact_id}",
                            subtask_id=subtask.subtask_id,
                        )
                    )
                elif artifact_type not in registration.accepted_input_types:
                    issues.append(
                        ValidationIssue(
                            code="UNSUPPORTED_INPUT_TYPE",
                            message=f"负责 Agent 不接受输入类型：{artifact_type}",
                            subtask_id=subtask.subtask_id,
                        )
                    )

    def _validate_dependencies(
        self,
        subtasks: list[AgentSubTask],
        known_subtask_ids: set[UUID],
        issues: list[ValidationIssue],
    ) -> None:
        """校验依赖是否存在、自依赖和新子任务内部是否成环。"""

        new_ids = {subtask.subtask_id for subtask in subtasks}
        valid_ids = new_ids | known_subtask_ids
        graph: dict[UUID, list[UUID]] = {}

        for subtask in subtasks:
            graph[subtask.subtask_id] = [
                dependency for dependency in subtask.dependency_subtask_ids if dependency in new_ids
            ]
            for dependency in subtask.dependency_subtask_ids:
                if dependency == subtask.subtask_id:
                    issues.append(
                        ValidationIssue(
                            code="SELF_DEPENDENCY",
                            message="子任务不能依赖自身",
                            subtask_id=subtask.subtask_id,
                        )
                    )
                elif dependency not in valid_ids:
                    issues.append(
                        ValidationIssue(
                            code="UNKNOWN_DEPENDENCY",
                            message=f"依赖子任务不存在：{dependency}",
                            subtask_id=subtask.subtask_id,
                        )
                    )

        visiting: set[UUID] = set()
        visited: set[UUID] = set()

        def visit(node: UUID) -> bool:
            if node in visiting:
                return True
            if node in visited:
                return False
            visiting.add(node)
            if any(visit(parent) for parent in graph.get(node, [])):
                return True
            visiting.remove(node)
            visited.add(node)
            return False

        if any(visit(node) for node in graph):
            issues.append(ValidationIssue(code="CYCLIC_DEPENDENCY", message="子任务依赖存在环"))

    def _validate_review_gates(
        self,
        subtasks: list[AgentSubTask],
        context: SubTaskValidationContext,
        issues: list[ValidationIssue],
    ) -> None:
        """审核标记必须位于发布任务的真实祖先链，不能用无关质量任务充当门禁。

        已有 Approval 可作为后续轮次的发布输入；这里仅验证计划结构，审批内容、
        目标版本和实际副作用仍由第六阶段 HumanReviewService/PublishGuard 校验。
        """

        artifact_types = self._artifact_types(context)
        by_id = {item.subtask_id: item for item in subtasks}
        gate_pairs = {
            "ontology_publish": (
                {"CandidateOntology", "FusedCandidateOntology"},
                "OntologyApproval",
                "ONTOLOGY",
            ),
            "graph_publish": ({"GraphQualityReport"}, "GraphApproval", "GRAPH"),
        }
        for subtask in subtasks:
            gate = gate_pairs.get(subtask.subtask_type)
            if gate is None:
                continue
            review_output_types, approval_type, label = gate
            has_approval = any(
                artifact_types.get(item) == approval_type for item in subtask.input_artifact_ids
            )
            ancestors = self._ancestor_ids(subtask, by_id)
            has_review_gate = any(
                by_id[item].human_review_required
                and bool(set(by_id[item].expected_output_types).intersection(review_output_types))
                for item in ancestors
                if item in by_id
            )
            if not has_approval and not has_review_gate:
                issues.append(
                    ValidationIssue(
                        code=f"MISSING_{label}_REVIEW_GATE",
                        message=(
                            f"{subtask.subtask_type} 必须依赖正确的人工审核目标或已有 Approval"
                        ),
                        subtask_id=subtask.subtask_id,
                    )
                )

    def _validate_ontology_prerequisites(
        self,
        subtasks: list[AgentSubTask],
        context: SubTaskValidationContext,
        issues: list[ValidationIssue],
    ) -> None:
        """实例抽取必须使用已有 PublishedOntology 或依赖本体发布任务。"""

        artifact_types = self._artifact_types(context)
        by_id = {item.subtask_id: item for item in subtasks}
        for subtask in subtasks:
            if subtask.subtask_type != "knowledge_extract":
                continue
            ancestors = self._ancestor_ids(subtask, by_id)
            planned_types = {
                output_type
                for ancestor_id in ancestors
                if ancestor_id in by_id
                for output_type in by_id[ancestor_id].expected_output_types
            }
            # 完整计划中的联合抽取任务还应继承祖先显式使用的稳定输入。例如本体
            # 发布任务的祖先输入与输出共同证明后续可获得 PublishedOntology。
            planned_types.update(
                artifact_types[artifact_id]
                for ancestor_id in ancestors
                if ancestor_id in by_id
                for artifact_id in by_id[ancestor_id].input_artifact_ids
                if artifact_id in artifact_types
            )
            has_ontology = (
                any(
                    artifact_types.get(item) == "PublishedOntology"
                    for item in subtask.input_artifact_ids
                )
                or "PublishedOntology" in planned_types
            )
            has_publish_dependency = any(
                by_id[item].subtask_type == "ontology_publish"
                and "PublishedOntology" in by_id[item].expected_output_types
                for item in ancestors
                if item in by_id
            )
            if not has_ontology and not has_publish_dependency:
                issues.append(
                    ValidationIssue(
                        code="MISSING_PUBLISHED_ONTOLOGY",
                        message=(
                            "实例抽取必须输入 PublishedOntology，或依赖通过审核门禁的本体发布任务"
                        ),
                        subtask_id=subtask.subtask_id,
                    )
                )

    def _validate_extraction_input_prerequisites(
        self,
        subtasks: list[AgentSubTask],
        context: SubTaskValidationContext,
        issues: list[ValidationIssue],
    ) -> None:
        """校验实例抽取输入可由当前 Artifact 或依赖祖先输出共同满足。

        完整计划发布时未来 Artifact ID 尚不存在，依赖边声明执行顺序，Worker 在上游
        成功后根据不可变 AgentRunResult 血缘绑定真实 ID。没有对应依赖产出的类型仍被拒绝。
        """

        required_types = {
            "knowledge_extract": {"SemanticDocument", "PublishedOntology"},
        }
        artifact_types = self._artifact_types(context)
        by_id = {item.subtask_id: item for item in subtasks}
        for subtask in subtasks:
            required = required_types.get(subtask.subtask_type)
            if required is None:
                continue
            actual = {
                artifact_types[artifact_id]
                for artifact_id in subtask.input_artifact_ids
                if artifact_id in artifact_types
            }
            for ancestor_id in self._ancestor_ids(subtask, by_id):
                if ancestor_id in by_id:
                    actual.update(by_id[ancestor_id].expected_output_types)
                    actual.update(
                        artifact_types[artifact_id]
                        for artifact_id in by_id[ancestor_id].input_artifact_ids
                        if artifact_id in artifact_types
                    )
            missing = required - actual
            if missing:
                issues.append(
                    ValidationIssue(
                        code="MISSING_EXTRACTION_INPUT_ARTIFACT",
                        message=(
                            f"{subtask.subtask_type} 的当前输入或依赖祖先无法提供 Artifact："
                            f"{sorted(missing)}；请在完整计划中补充生产这些输入的前置任务"
                        ),
                        subtask_id=subtask.subtask_id,
                    )
                )

    @classmethod
    def _artifact_types(cls, context: SubTaskValidationContext) -> dict[UUID, str]:
        """返回 Artifact 类型索引；归属、状态和版本由 HandoffValidator 检查。"""

        return {
            artifact_id: item.artifact_type
            for artifact_id, item in cls._artifact_refs(context).items()
        }

    @staticmethod
    def _artifact_refs(context: SubTaskValidationContext) -> dict[UUID, ArtifactRef]:
        """合并当前快照和请求输入，供本体策略与任务前置条件校验。"""

        historical = context.current_context.artifact_refs if context.current_context else []
        return {item.artifact_id: item for item in [*historical, *context.available_artifacts]}

    @staticmethod
    def _ancestor_ids(subtask: AgentSubTask, by_id: dict[UUID, AgentSubTask]) -> set[UUID]:
        """迭代收集依赖祖先；visited 防止非法环在错误收集阶段导致递归失控。"""

        ancestors: set[UUID] = set()
        pending = list(subtask.dependency_subtask_ids)
        while pending:
            current = pending.pop()
            if current in ancestors:
                continue
            ancestors.add(current)
            if current in by_id:
                pending.extend(by_id[current].dependency_subtask_ids)
        return ancestors
