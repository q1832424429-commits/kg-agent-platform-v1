"""实现总控 Agent 小型 LangGraph 的各个节点。

节点只读取和写回 ControllerState，不直接操作数据库、专业 Skill 或发布副作用。
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

from kg_agent.agents.controller.draft_models import TaskPlanDraft
from kg_agent.agents.controller.prompts import (
    PromptRepository,
    build_decomposition_prompt,
    build_repair_prompt,
)
from kg_agent.agents.controller.state import ControllerRequest, ControllerState
from kg_agent.domain.agent_models import TaskDecompositionResult
from kg_agent.domain.artifact_models import ArtifactRef
from kg_agent.domain.context_models import SharedTaskContext
from kg_agent.domain.enums import ArtifactStatus, SubTaskStatus, TaskIntent
from kg_agent.domain.task_models import AgentSubTask, OntologyStrategy, TaskSpec
from kg_agent.exceptions import ContextConflictError, ModelGatewayError, SubTaskValidationError
from kg_agent.model_gateway.base import ModelCallContext, ModelGateway
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.context_service import ContextService
from kg_agent.validators.artifact_reuse_validator import ArtifactReuseValidator
from kg_agent.validators.handoff_validator import HandoffValidator
from kg_agent.validators.subtask_validator import (
    SubTaskValidationContext,
    SubTaskValidator,
    ValidationIssue,
)


class ControllerNodes:
    """集中实现总控 Agent 图节点及其确定性依赖。"""

    _NON_MODEL_REPAIRABLE_CODES = {
        "REUSE_PARENT_LINEAGE_MISMATCH",
        "REUSE_PARENT_CHECKSUM_MISMATCH",
        "REUSE_PARENT_TASK_MISMATCH",
        "REUSE_PARENT_NOT_AVAILABLE",
    }

    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        agent_registry: AgentRegistry,
        context_service: ContextService,
        prompt_repository: PromptRepository,
        skill_registry: SkillRegistry | None,
        validator: SubTaskValidator,
        handoff_validator: HandoffValidator,
        reuse_validator: ArtifactReuseValidator,
        max_repairs: int,
    ) -> None:
        """注入模型、Registry、上下文服务、Prompt和校验器。"""

        self._model_gateway = model_gateway
        self._agent_registry = agent_registry
        self._context_service = context_service
        self._prompt_repository = prompt_repository
        self._skill_registry = skill_registry
        self._validator = validator
        self._handoff_validator = handoff_validator
        self._reuse_validator = reuse_validator
        self._max_repairs = max_repairs

    async def load_context(self, state: ControllerState) -> ControllerState:
        """读取当前任务快照和已发布子任务，写回内部状态。"""

        request = state["request"]
        # task_id 是任务事实主键；即使调用方漏传 current_task_id，也不能把已存在任务
        # 当作 START 绕过 CONTINUE/MODIFY 校验。
        current_context = await self._context_service.get(request.task_id)
        known_subtasks = []
        task_spec_history: dict[int, TaskSpec] = {}
        if current_context is not None:
            if current_context.master_task.conversation_id != request.conversation_id:
                raise ContextConflictError("conversation_id 与当前任务不一致")
            known_subtasks = await self._context_service.list_subtasks(request.task_id)
            for revision in current_context.task_spec_revision_refs:
                spec = await self._context_service.get_task_spec(request.task_id, revision)
                if spec is not None:
                    task_spec_history[revision] = spec
        return {
            "current_context": current_context,
            "known_subtasks": known_subtasks,
            "task_spec_history": task_spec_history,
            "repair_attempts": 0,
            "validation_issues": [],
            "excluded_reuse_artifact_ids": set(),
        }

    async def decompose(self, state: ControllerState) -> ControllerState:
        """调用模型生成轻量计划，并编译为完整候选契约。"""

        try:
            draft = await self._invoke_model(state)
            candidate = self._apply_operation_constraints(self._compile_draft(draft, state), state)
        except ModelGatewayError as exc:
            return {
                "validation_issues": [
                    ValidationIssue(code="MODEL_OUTPUT_INVALID", message=str(exc))
                ]
            }
        return {"draft": draft, "candidate": candidate, "validation_issues": []}

    async def validate_candidate(self, state: ControllerState) -> ControllerState:
        """使用确定性 SubTaskValidator 检查模型候选结果。"""

        candidate = state.get("candidate")
        if candidate is None:
            return {
                "validation_issues": state.get("validation_issues")
                or [
                    ValidationIssue(
                        code="MISSING_MODEL_RESULT",
                        message="模型没有产生可校验的 TaskDecompositionResult",
                    )
                ]
            }

        request = state["request"]
        # 人工审核不是可由模型自由省略的步骤，而是发布的固定安全门禁。本体发布只
        # 补全候选任务的审核标记；已有 CandidateGraph 的续办发布还会按冻结架构补齐
        # 质量评估 AgentSubTask，使 HumanReviewService 获得可绑定的审核材料和依赖锚点。
        current_context = state.get("current_context")
        context_artifacts = current_context.artifact_refs if current_context is not None else []
        candidate = self._complete_required_review_gates(
            candidate,
            available_artifacts=[*request.existing_artifacts, *context_artifacts],
        )
        candidate = self._normalize_historical_reuse(
            candidate,
            current_context=current_context,
            available_artifacts=request.existing_artifacts,
        )
        known_subtasks = state.get("known_subtasks", [])
        validation_context = SubTaskValidationContext(
            task_id=request.task_id,
            user_message=request.user_message,
            current_context=state.get("current_context"),
            available_artifacts=request.existing_artifacts,
            known_subtask_ids={item.subtask_id for item in known_subtasks},
            known_subtask_statuses={item.subtask_id: item.status for item in known_subtasks},
        )
        issues = self._validator.validate(candidate, validation_context)
        issues.extend(self._handoff_validator.validate(candidate, validation_context))
        # 模型只提出复用 ID；正式发布前必须经过与模型无关的兼容性门禁。
        issues.extend(
            await self._reuse_validator.validate(
                candidate, validation_context, state.get("task_spec_history", {})
            )
        )
        # 本轮已经被确定性校验拒绝的复用 ID 不应再次作为修复候选传给模型。这个
        # 仅缩小当前请求的模型选择空间；最终 ArtifactReuseValidator 仍保留为门禁。
        excluded_ids = set(state.get("excluded_reuse_artifact_ids", set()))
        excluded_ids.update(_rejected_reuse_artifact_ids(issues))
        return {
            "candidate": candidate,
            "validation_issues": issues,
            "excluded_reuse_artifact_ids": excluded_ids,
        }

    async def repair_candidate(self, state: ControllerState) -> ControllerState:
        """携带结构化校验错误再次调用模型，生成完整修复候选。"""

        attempts = state.get("repair_attempts", 0) + 1
        try:
            draft = await self._invoke_model(
                state,
                issues=state.get("validation_issues", []),
                previous_result=state.get("draft"),
            )
            candidate = self._apply_operation_constraints(
                self._compile_draft(draft, state),
                state,
            )
        except ModelGatewayError as exc:
            return {
                "repair_attempts": attempts,
                "validation_issues": [
                    ValidationIssue(code="MODEL_OUTPUT_INVALID", message=str(exc))
                ],
            }
        return {
            "draft": draft,
            "candidate": candidate,
            "repair_attempts": attempts,
            "validation_issues": [],
        }

    async def publish(self, state: ControllerState) -> ControllerState:
        """把已校验结果通过 ContextService 追加到 SharedContext。"""

        candidate = state["candidate"]
        request = state["request"]
        # 测试端人工计划审核使用“两阶段发布”：总控仍完成模型推理、确定性编译和
        # 全部校验，但在用户批准前不向 SharedContext 写入 AgentSubTask/Assignment。
        if candidate.requires_clarification or not request.publish_decomposition:
            return {"published_context": None}

        current_context = state.get("current_context")
        published = await self._context_service.publish_decomposition(
            conversation_id=request.conversation_id,
            result=candidate,
            source_artifacts=request.existing_artifacts,
            recalled_long_term_memories=request.recalled_long_term_memories,
            expected_version=current_context.version if current_context else None,
        )
        return {"published_context": published}

    async def fail(self, state: ControllerState) -> ControllerState:
        """在修复次数耗尽后终止发布并抛出结构化校验异常。"""

        issues = state.get("validation_issues", [])
        detail = "; ".join(f"{item.code}: {item.message}" for item in issues)
        raise SubTaskValidationError(f"总控 Agent 拆解结果无法通过校验：{detail}")

    async def route_after_validation(self, state: ControllerState) -> str:
        """根据校验问题和修复次数选择发布、修复或失败。

        条件路由保持为异步函数，使它与其余总控节点运行在同一事件循环中，避免
        校验修复环每次经过同步线程池调度，降低修复路径的等待与线程资源占用。
        """

        if not state.get("validation_issues"):
            return "publish"
        # 父血缘、checksum、来源任务和可用状态来自权威存储。模型既看不到完整
        # 数据库快照，也不能修复已损坏的资产；再次调用只会增加等待时间并产生
        # 不可信的“修复”计划，因此直接保留稳定错误交给调用方处理。
        if any(
            issue.code in self._NON_MODEL_REPAIRABLE_CODES
            for issue in state.get("validation_issues", [])
        ):
            return "fail"
        if state.get("repair_attempts", 0) < self._max_repairs:
            return "repair"
        return "fail"

    async def _invoke_model(
        self,
        state: ControllerState,
        *,
        issues: list[ValidationIssue] | None = None,
        previous_result: TaskPlanDraft | None = None,
    ) -> TaskPlanDraft:
        """构造受控上下文并调用模型网关。"""

        request = state["request"]
        base_payload = build_decomposition_prompt(
            request=request,
            current_context=state.get("current_context"),
            known_subtasks=state.get("known_subtasks", []),
            registry=self._agent_registry,
            skill_registry=self._skill_registry,
            task_spec_history=state.get("task_spec_history", {}),
            excluded_reuse_artifact_ids=state.get("excluded_reuse_artifact_ids", set()),
        )
        user_prompt = self._prompt_repository.load_decomposition_prompt() + "\n\n" + base_payload
        if issues:
            user_prompt = build_repair_prompt(
                base_prompt=user_prompt,
                issues=issues,
                previous_result=previous_result,
                forbidden_reuse_artifact_ids=state.get("excluded_reuse_artifact_ids", set()),
            )
        return await self._model_gateway.generate_structured(
            system_prompt=self._prompt_repository.load_system_prompt(),
            user_prompt=user_prompt,
            output_type=TaskPlanDraft,
            call_context=ModelCallContext(
                component="CONTROLLER",
                actor_name="controller_agent",
                task_id=request.task_id,
                attempt=state.get("repair_attempts", 0),
            ),
        )

    def _apply_operation_constraints(
        self,
        candidate: TaskDecompositionResult,
        state: ControllerState,
    ) -> TaskDecompositionResult:
        """补全运行时拥有唯一事实来源的任务标识与内部操作约束。

        模型仍负责用户意图、业务目标和 Agent 级拆解；Task ID、TaskSpec revision、
        decomposition revision，以及新子任务的持久化 ID 则由当前请求与 SharedContext
        确定，不能让模型猜测。

        LLM 结构化输出为了兼容公开 AgentSubTask 契约，仍会携带 subtask_id 字段；该
        字段只在单次模型响应内用来表达候选节点之间的依赖关系，绝不能直接作为数据库
        主键使用。这里为每个候选节点重新分配 UUID，并同步改写计划内部依赖；指向已
        发布历史子任务的依赖不在映射中，会原样保留。这样模型复制历史 UUID 不会覆盖
        或冲突已有任务，同时 CONTINUE/MODIFY 仍能等待有效历史任务。
        内部续办/验收还必须固定为 CONTINUE 并沿用当前不可变 TaskSpec，防止系统指令
        被误识别为用户 START。
        """

        request = state["request"]
        current = state.get("current_context")
        now = datetime.now(UTC)
        decomposition_revision = (
            current.master_task.current_decomposition_revision + 1 if current else 1
        )
        selected_ontology = ControllerNodes._selected_published_ontology(
            request=request,
            current_context=current,
        )
        # 本任务此前建设并发布的本体可继续作为知识抽取输入，但这与用户本轮
        # 显式改选本体不同，不能借此把 BUILD_FROM_DOCUMENTS 改为 REUSE_PUBLISHED。
        historical_published = (
            [item for item in current.artifact_refs if item.artifact_type == "PublishedOntology"]
            if current is not None
            and current.current_task_spec.ontology_strategy.mode == "BUILD_FROM_DOCUMENTS"
            else []
        )
        extraction_ontology = selected_ontology or (
            historical_published[0] if len(historical_published) == 1 else None
        )
        selected_external_ontology = ControllerNodes._selected_external_ontology(
            request=request,
            current_context=current,
        )

        # 澄清状态代表当前轮尚不能安全发布工作单。模型偶尔会同时给出澄清问题和一
        # 组猜测的子任务；猜测任务不能进入 SharedContext，也不值得为此再发起一次模型
        # 修复调用。保留澄清问题和 TaskSpec，确定性地清空尚未获确认的任务计划即可。
        # 用户补充信息后的下一轮会重新得到完整计划。
        if candidate.requires_clarification:
            candidate = candidate.model_copy(update={"subtasks": [], "reused_artifact_ids": []})

        planned_subtasks = [self._complete_registered_outputs(item) for item in candidate.subtasks]
        if selected_ontology is not None:
            # “选择已发布本体”是用户已经确认的输入事实，而不是模型可自行改写的
            # 规划选项。V1 暂不支持更新该本体：它只能作为不可变约束参与实例抽取。
            # 因而移除模型误加的本体重建、融合、变更和发布任务；若未来支持本体更新，
            # 必须走新的候选版本、人工审核和发布流程，不能复用本分支。
            ontology_build_subtask_types = {
                "ontology_extract",
                "ontology_align",
                "ontology_fuse",
                "ontology_changeset_apply",
                "ontology_publish",
            }
            planned_subtasks = [
                item
                for item in planned_subtasks
                if item.subtask_type not in ontology_build_subtask_types
            ]

        original_plan_ids = {item.subtask_id for item in candidate.subtasks}
        generated_ids = {item.subtask_id: uuid4() for item in planned_subtasks}
        subtasks = [
            item.model_copy(
                update={
                    "subtask_id": generated_ids[item.subtask_id],
                    "master_task_id": request.task_id,
                    "decomposition_revision": decomposition_revision,
                    "dependency_subtask_ids": [
                        generated_ids.get(dependency_id, dependency_id)
                        for dependency_id in item.dependency_subtask_ids
                        # 被移除的候选本体建设节点不能继续阻塞实例抽取；已发布本体
                        # 会作为显式输入注入对应抽取任务。
                        if not (
                            selected_ontology is not None
                            and dependency_id in original_plan_ids
                            and dependency_id not in generated_ids
                        )
                    ],
                    "input_artifact_ids": ControllerNodes._with_selected_ontology_input(
                        item.input_artifact_ids,
                        subtask_type=item.subtask_type,
                        selected_ontology=extraction_ontology,
                    ),
                    # 新计划只能由确定性服务从 PENDING 推进到 READY/BLOCKED，模型的
                    # 状态和创建时间不能成为任务事实。
                    "status": SubTaskStatus.PENDING,
                    "created_at": now,
                }
            )
            for item in planned_subtasks
        ]
        # CONTINUE 可以合法地逐字段沿用不可变的当前 TaskSpec。此时绝不能重写其
        # created_at，否则会把“原样沿用”误判为同一 revision 内容被篡改；只有新建
        # 任务或创建下一 revision 时，才由运行时写入新的创建事实。
        task_spec_updates: dict[str, object] = {"task_id": request.task_id}
        if current is None or candidate.task_spec.revision != current.current_task_spec.revision:
            task_spec_updates.update(
                {
                    "created_by": "controller_agent",
                    "created_at": now,
                }
            )
        task_spec = candidate.task_spec.model_copy(update=task_spec_updates)
        reused_artifact_ids = list(candidate.reused_artifact_ids)
        if selected_ontology is not None:
            # 已发布本体进入 TaskSpec 的来源集合和复用集合，保证后续续办、Artifact
            # 兼容性判断与审计都能明确知道本轮实例抽取采用了哪个本体版本。
            source_ids = list(
                dict.fromkeys([*task_spec.source_artifact_ids, selected_ontology.artifact_id])
            )
            task_spec = task_spec.model_copy(
                update={
                    "source_artifact_ids": source_ids,
                    "ontology_strategy": OntologyStrategy(
                        mode="REUSE_PUBLISHED",
                        selected_ontology_id=selected_ontology.ontology_id,
                        selected_ontology_version=selected_ontology.ontology_version,
                    ),
                    "target_output_types": [
                        item
                        for item in task_spec.target_output_types
                        if item
                        not in {
                            "CandidateOntology",
                            "FusedCandidateOntology",
                            "OntologyChangeSet",
                            "OntologyApproval",
                            "PublishedOntology",
                        }
                    ],
                }
            )
            # TaskSpec 中保留本体来源并不代表每一轮都重新消费该 Artifact。只有本轮
            # knowledge_extract 子任务真正引用它时才声明复用，避免纯图谱发布续办被
            # 无关的本体父血缘校验阻塞。
            if (
                any(selected_ontology.artifact_id in item.input_artifact_ids for item in subtasks)
                and selected_ontology.artifact_id not in reused_artifact_ids
            ):
                reused_artifact_ids.append(selected_ontology.artifact_id)
        elif selected_external_ontology is not None:
            # 唯一外部本体是请求输入中已经存在的客观事实，不能由模型改写成“复用
            # 已发布本体”或“从文档建设”。多个外部本体时不擅自选择，由总控向用户
            # 澄清，避免把错误本体静默带入实例抽取。
            source_ids = list(
                dict.fromkeys(
                    [*task_spec.source_artifact_ids, selected_external_ontology.artifact_id]
                )
            )
            task_spec = task_spec.model_copy(
                update={
                    "source_artifact_ids": source_ids,
                    "ontology_strategy": OntologyStrategy(
                        mode="IMPORT_EXTERNAL_JSON",
                        external_ontology_artifact_id=selected_external_ontology.artifact_id,
                    ),
                }
            )
        elif not ControllerNodes._has_available_ontology(request=request, current_context=current):
            # 没有外部或已发布本体时，只能从当前文档建设候选本体。该来源判断属于
            # Artifact 事实，不交给 LLM 猜测，避免出现空 ID 的 REUSE_PUBLISHED。
            task_spec = task_spec.model_copy(
                update={"ontology_strategy": OntologyStrategy(mode="BUILD_FROM_DOCUMENTS")}
            )

        if (
            current is not None
            and request.operation == "USER_TURN"
            and self._existing_graph_publish_only(request, current)
        ):
            # 纯“把现有候选图谱发布”只扩展交付终点。即使模型把该轮说成 MODIFY，
            # 已产生图谱的来源、本体与抽取范围也不能被模型草案顺手改写。
            task_spec = current.current_task_spec.model_copy(
                update={
                    "revision": current.current_task_spec.revision + 1,
                    "task_intent": TaskIntent.CONTINUE,
                    "business_goal": "发布当前候选图谱",
                    "created_by": "controller_agent",
                    "created_at": now,
                }
            )
            candidate = candidate.model_copy(update={"task_intent": TaskIntent.CONTINUE})

        if request.operation == "USER_TURN" and self._explicit_graph_publish_request(
            request.user_message
        ):
            # “抽取实体关系并入库”与“发布图谱”具有同一发布含义。最终交付类型是
            # 用户的显式目标，不应因为模型只写了中间数据集而在校验阶段反复重试。
            # 对已有任务创建下一 revision；新任务则仍保持 START/revision 1。
            publish_updates: dict[str, object] = {
                "target_output_types": list(
                    dict.fromkeys([*task_spec.target_output_types, "PublishedGraph"])
                )
            }
            if current is not None:
                publish_updates.update(
                    {
                        "revision": current.current_task_spec.revision + 1,
                        "task_intent": TaskIntent.CONTINUE,
                        "created_by": "controller_agent",
                        "created_at": now,
                    }
                )
                candidate = candidate.model_copy(update={"task_intent": TaskIntent.CONTINUE})
            task_spec = task_spec.model_copy(update=publish_updates)
        if current is None and candidate.task_intent is TaskIntent.START:
            task_spec = task_spec.model_copy(
                update={"revision": 1, "task_intent": TaskIntent.START}
            )
        normalized = candidate.model_copy(
            update={
                "task_spec": task_spec,
                "subtasks": subtasks,
                "reused_artifact_ids": reused_artifact_ids,
            }
        )
        if request.operation == "USER_TURN" or current is None:
            return normalized
        return normalized.model_copy(
            update={
                "task_intent": TaskIntent.CONTINUE,
                "task_spec": current.current_task_spec,
            }
        )

    def _compile_draft(
        self,
        draft: TaskPlanDraft,
        state: ControllerState,
    ) -> TaskDecompositionResult:
        """把模型语义计划编译为现有公共任务契约。

        计划内别名只表达依赖。所有持久化 ID、revision、初始状态和时间均由运行时
        产生；模型不再生成或校验这些系统事实。
        """

        request = state["request"]
        current = state.get("current_context")
        now = datetime.now(UTC)
        if current is not None and (
            request.operation != "USER_TURN" or draft.task_intent is TaskIntent.CONTINUE
        ):
            task_spec = current.current_task_spec
        else:
            revision = 1 if current is None else current.current_task_spec.revision + 1
            spec = draft.task_spec
            task_spec = TaskSpec(
                task_id=request.task_id,
                revision=revision,
                task_intent=draft.task_intent,
                business_goal=spec.business_goal,
                domain=spec.domain,
                source_artifact_ids=spec.source_artifact_ids,
                target_output_types=spec.target_output_types,
                ontology_strategy=spec.ontology_strategy,
                extraction_scope=spec.extraction_scope,
                quality_requirements=spec.quality_requirements,
                reuse_policy=spec.reuse_policy,
                constraints=spec.constraints,
                created_by="controller_agent",
                created_at=now,
            )

        aliases = {item.node_id: uuid4() for item in draft.subtasks}

        def dependency_id(value: str) -> UUID:
            """解析计划内别名或历史 UUID；非法别名交给依赖校验拒绝。"""

            if value in aliases:
                return aliases[value]
            try:
                return UUID(value)
            except ValueError:
                return uuid5(NAMESPACE_URL, f"kg-agent:unresolved-plan-node:{value}")

        subtasks = [
            AgentSubTask(
                subtask_id=aliases[item.node_id],
                master_task_id=request.task_id,
                decomposition_revision=1,
                name=item.name,
                description=item.description or item.goal,
                goal=item.goal,
                subtask_type=item.subtask_type,
                assigned_agent_type=item.assigned_agent_type,
                dependency_subtask_ids=[
                    dependency_id(dependency) for dependency in item.dependency_node_ids
                ],
                input_artifact_ids=item.input_artifact_ids,
                expected_output_types=item.expected_output_types,
                completion_criteria=(
                    item.completion_criteria
                    or [f"产生声明的输出 Artifact：{', '.join(item.expected_output_types)}"]
                ),
                human_review_required=item.human_review_required,
                status=SubTaskStatus.PENDING,
                created_by="controller_agent",
                created_at=now,
            )
            for item in draft.subtasks
        ]
        return TaskDecompositionResult(
            task_intent=draft.task_intent,
            task_spec=task_spec,
            subtasks=subtasks,
            reused_artifact_ids=draft.reused_artifact_ids,
            requires_clarification=draft.requires_clarification,
            clarification_question=draft.clarification_question,
        )

    def _complete_registered_outputs(self, subtask: AgentSubTask) -> AgentSubTask:
        """根据 Skill 原子契约补齐模型已选任务必然产生的伴随输出。

        仅匹配与模型声明输出有交集的 Skill，因此不会新增 AgentSubTask、选择额外业务
        步骤或改变用户目标。例如模型声明关系抽取产生 ``RelationDataset`` 时，代码
        自动补入同一个 Skill 必然产生的 ``EvidenceDataset``。
        """

        if self._skill_registry is None:
            return subtask
        candidates = self._skill_registry.list_for_subtask(
            agent_type=subtask.assigned_agent_type,
            subtask_type=subtask.subtask_type,
        )
        declared = set(subtask.expected_output_types)
        # 一个 Agent + subtask_type 只对应一个 Skill 时，其原子输出是 Registry
        # 已确定的系统事实，不要求模型先正确写出其中任意一个字段才能识别。只有
        # document_parse 这类同类型下存在多个 Skill 时，才用模型声明的业务输出
        # 区分本轮要执行的具体能力。
        matched = (
            candidates
            if len(candidates) == 1
            else [
                skill for skill in candidates if declared.intersection(skill.output_artifact_types)
            ]
        )
        outputs = list(subtask.expected_output_types)
        for skill in matched:
            outputs.extend(skill.output_artifact_types)
        return subtask.model_copy(update={"expected_output_types": list(dict.fromkeys(outputs))})

    @staticmethod
    def _selected_published_ontology(
        *,
        request: ControllerRequest,
        current_context: SharedTaskContext | None,
    ) -> ArtifactRef | None:
        """确定本轮用户显式选择或当前任务沿用的唯一已发布本体。

        新用户轮次优先采用 ``existing_artifacts`` 中的 PublishedOntology，因为这是
        上传/资产选择接口刚刚带入的事实。继续任务未重复上传本体时，才根据当前
        TaskSpec 的 ``REUSE_PUBLISHED`` 选择上下文中同版本 Artifact。多个新输入本体
        不在 V1 自动选择范围内，交给模型澄清而不冒然指定其中一个。
        """

        # 内部续办和最终验收只能沿用已经冻结的 TaskSpec。刚刚由本任务发布的本体
        # 会进入可见 Artifact 集合，但不能因此把 BUILD_FROM_DOCUMENTS 改成
        # REUSE_PUBLISHED；用户新一轮显式选择才允许改变来源策略。
        if request.operation != "USER_TURN" and current_context is not None:
            strategy = current_context.current_task_spec.ontology_strategy
            if strategy.mode != "REUSE_PUBLISHED":
                return None
        historical_ids = (
            {item.artifact_id for item in current_context.artifact_refs}
            if current_context is not None
            else set()
        )
        # existing_artifacts 在续办时包含整个 SharedContext。只有本轮新导入的本体
        # 才表示用户显式改选；任务先前自己发布的本体不能反过来改写原 TaskSpec。
        requested = [
            item
            for item in request.existing_artifacts
            if item.artifact_type == "PublishedOntology" and item.artifact_id not in historical_ids
        ]
        if len(requested) == 1:
            return requested[0]
        if current_context is None:
            return None
        strategy = current_context.current_task_spec.ontology_strategy
        if strategy.mode != "REUSE_PUBLISHED":
            return None
        return next(
            (
                item
                for item in current_context.artifact_refs
                if item.artifact_type == "PublishedOntology"
                and item.ontology_id == strategy.selected_ontology_id
                and item.ontology_version == strategy.selected_ontology_version
            ),
            None,
        )

    @staticmethod
    def _selected_external_ontology(
        *,
        request: ControllerRequest,
        current_context: SharedTaskContext | None,
    ) -> ArtifactRef | None:
        """返回唯一可确定选择的外部本体，多个候选时保持未选择状态。"""

        requested = [
            item for item in request.existing_artifacts if item.artifact_type == "ExternalOntology"
        ]
        if len(requested) == 1:
            return requested[0]
        if requested:
            return None
        if current_context is None:
            return None
        historical = [
            item
            for item in current_context.artifact_refs
            if item.artifact_type == "ExternalOntology"
        ]
        return historical[0] if len(historical) == 1 else None

    @staticmethod
    def _has_available_ontology(
        *,
        request: ControllerRequest,
        current_context: SharedTaskContext | None,
    ) -> bool:
        """判断当前计划事实范围内是否存在任意本体输入。"""

        artifacts = [*request.existing_artifacts]
        if current_context is not None:
            artifacts.extend(current_context.artifact_refs)
        return any(
            item.artifact_type in {"ExternalOntology", "PublishedOntology"} for item in artifacts
        )

    @staticmethod
    def _with_selected_ontology_input(
        input_artifact_ids: list[UUID],
        *,
        subtask_type: str,
        selected_ontology: ArtifactRef | None,
    ) -> list[UUID]:
        """为实例抽取任务补入显式选用或本任务已发布的本体引用。"""

        if selected_ontology is None or subtask_type != "knowledge_extract":
            return input_artifact_ids
        return list(dict.fromkeys([*input_artifact_ids, selected_ontology.artifact_id]))

    @staticmethod
    def _complete_required_review_gates(
        candidate: TaskDecompositionResult,
        *,
        available_artifacts: list[ArtifactRef],
    ) -> TaskDecompositionResult:
        """为本体和图谱发布补全不可绕过的人工审核暂停点。

        总控仍由 LLM 决定是否需要构建、融合或发布本体；但一旦它已经决定发布，审核
        门禁是不可省略的确定性安全约束。若发布任务已有 ``OntologyApproval`` 则保持原样；
        否则优先选择它依赖链中最靠后的 FusedCandidateOntology/CandidateOntology，标记为
        人工审核并确保发布任务依赖该节点。找不到候选本体来源时不编造任务，由校验器返回
        明确错误并交给总控修复。

        对“已有 CandidateGraph 后继续发布”的场景，若当前候选版本已经存在 GraphApproval，
        发布任务直接复用两者；否则确定性增加或修正一个 graph_quality_evaluate 子任务，
        让它绑定最新候选图谱及同一血缘中的 ConflictReport，并作为 graph_publish 的人工审核
        祖先。HumanReviewService 随后根据质量报告创建审核请求，批准后才会解锁发布任务。
        """

        artifact_by_id = {
            item.artifact_id: item
            for item in available_artifacts
            if item.status is ArtifactStatus.AVAILABLE
        }
        artifact_types = {
            artifact_id: item.artifact_type for artifact_id, item in artifact_by_id.items()
        }
        by_id = {item.subtask_id: item for item in candidate.subtasks}

        def ancestor_ids(subtask_id: UUID) -> set[UUID]:
            """取得候选计划中一个节点的全部依赖祖先，异常环仍交由校验器拒绝。"""

            found: set[UUID] = set()
            pending = list(by_id[subtask_id].dependency_subtask_ids)
            while pending:
                dependency_id = pending.pop()
                if dependency_id in found or dependency_id not in by_id:
                    continue
                found.add(dependency_id)
                pending.extend(by_id[dependency_id].dependency_subtask_ids)
            return found

        updated = {item.subtask_id: item for item in candidate.subtasks}
        for publish in candidate.subtasks:
            if publish.subtask_type != "ontology_publish":
                continue
            if any(
                artifact_types.get(item) == "OntologyApproval"
                for item in publish.input_artifact_ids
            ):
                continue

            ancestors = ancestor_ids(publish.subtask_id)
            sources = [
                by_id[item]
                for item in ancestors
                if set(by_id[item].expected_output_types).intersection(
                    {"CandidateOntology", "FusedCandidateOntology"}
                )
            ]
            # 模型偶尔漏写发布依赖；仅在计划中已有候选来源时补上，不虚构新任务。
            if not sources:
                sources = [
                    item
                    for item in candidate.subtasks
                    if item.subtask_id != publish.subtask_id
                    and set(item.expected_output_types).intersection(
                        {"CandidateOntology", "FusedCandidateOntology"}
                    )
                    and publish.subtask_id not in ancestor_ids(item.subtask_id)
                ]
            if not sources:
                continue

            source = max(
                sources,
                key=lambda item: (
                    "FusedCandidateOntology" in item.expected_output_types,
                    len(ancestor_ids(item.subtask_id)),
                ),
            )
            updated[source.subtask_id] = source.model_copy(update={"human_review_required": True})
            dependencies = list(publish.dependency_subtask_ids)
            if source.subtask_id not in dependencies:
                dependencies.append(source.subtask_id)
            updated[publish.subtask_id] = publish.model_copy(
                update={"dependency_subtask_ids": dependencies}
            )

        graph_publishes = [
            item for item in candidate.subtasks if item.subtask_type == "graph_publish"
        ]
        graph_candidates = sorted(
            (
                item
                for item in artifact_by_id.values()
                if item.artifact_type == "CandidateGraph"
                and item.task_id == candidate.task_spec.task_id
            ),
            key=lambda item: (item.created_at, str(item.artifact_id)),
        )
        if graph_publishes and graph_candidates:
            latest_graph = graph_candidates[-1]

            def lineage_ids(root: ArtifactRef) -> set[UUID]:
                """读取当前上下文可见的完整父级血缘 ID，不跨任务猜测外部产物。"""

                found: set[UUID] = set()
                pending = list(root.parent_artifact_ids)
                while pending:
                    artifact_id = pending.pop()
                    if artifact_id in found:
                        continue
                    found.add(artifact_id)
                    parent = artifact_by_id.get(artifact_id)
                    if parent is not None:
                        pending.extend(parent.parent_artifact_ids)
                return found

            graph_lineage = lineage_ids(latest_graph)
            related_conflicts = sorted(
                (
                    artifact_by_id[item]
                    for item in graph_lineage
                    if item in artifact_by_id
                    and artifact_by_id[item].artifact_type == "ConflictReport"
                ),
                key=lambda item: (item.created_at, str(item.artifact_id)),
            )
            exact_approvals = sorted(
                (
                    item
                    for item in artifact_by_id.values()
                    if item.artifact_type == "GraphApproval"
                    and latest_graph.artifact_id in item.parent_artifact_ids
                ),
                key=lambda item: (item.created_at, str(item.artifact_id)),
            )

            shared_quality: AgentSubTask | None = None
            for publish in graph_publishes:
                current_publish = updated[publish.subtask_id]
                retained_inputs = [
                    artifact_id
                    for artifact_id in current_publish.input_artifact_ids
                    if artifact_types.get(artifact_id) not in {"CandidateGraph", "GraphApproval"}
                ]
                if exact_approvals:
                    # Approval 已经通过目标 ID 直接绑定当前不可变候选版本；PublishGuard
                    # 仍会读取内容并核验 target checksum，编译器这里只选择正确血缘。
                    updated[publish.subtask_id] = current_publish.model_copy(
                        update={
                            "input_artifact_ids": list(
                                dict.fromkeys(
                                    [
                                        *retained_inputs,
                                        latest_graph.artifact_id,
                                        exact_approvals[-1].artifact_id,
                                    ]
                                )
                            )
                        }
                    )
                    continue

                quality_ancestors = [
                    updated[item]
                    for item in ancestor_ids(publish.subtask_id)
                    if item in updated and updated[item].subtask_type == "graph_quality_evaluate"
                ]
                if shared_quality is None:
                    shared_quality = quality_ancestors[-1] if quality_ancestors else None
                if shared_quality is None:
                    quality_inputs = [latest_graph.artifact_id]
                    if related_conflicts:
                        quality_inputs.append(related_conflicts[-1].artifact_id)
                    shared_quality = AgentSubTask(
                        subtask_id=uuid4(),
                        master_task_id=publish.master_task_id,
                        decomposition_revision=publish.decomposition_revision,
                        name="评估候选图谱质量",
                        description="评估待发布候选图谱并准备人工审核材料",
                        goal="为当前候选图谱生成 GraphQualityReport",
                        subtask_type="graph_quality_evaluate",
                        assigned_agent_type="quality_evaluation_agent",
                        dependency_subtask_ids=[],
                        input_artifact_ids=quality_inputs,
                        expected_output_types=["GraphQualityReport"],
                        completion_criteria=["生成绑定当前 CandidateGraph 的 GraphQualityReport"],
                        human_review_required=True,
                        status=SubTaskStatus.PENDING,
                        created_by="controller_agent",
                        created_at=publish.created_at,
                    )
                    by_id[shared_quality.subtask_id] = shared_quality
                    updated[shared_quality.subtask_id] = shared_quality
                else:
                    quality_inputs = list(shared_quality.input_artifact_ids)
                    quality_inputs.append(latest_graph.artifact_id)
                    if related_conflicts:
                        quality_inputs.append(related_conflicts[-1].artifact_id)
                    shared_quality = shared_quality.model_copy(
                        update={
                            "input_artifact_ids": list(dict.fromkeys(quality_inputs)),
                            "human_review_required": True,
                        }
                    )
                    by_id[shared_quality.subtask_id] = shared_quality
                    updated[shared_quality.subtask_id] = shared_quality

                dependencies = list(current_publish.dependency_subtask_ids)
                if shared_quality.subtask_id not in dependencies:
                    dependencies.append(shared_quality.subtask_id)
                updated[publish.subtask_id] = current_publish.model_copy(
                    update={
                        "dependency_subtask_ids": dependencies,
                        # CandidateGraph 通过质量任务的显式输入沿祖先链交给发布任务，
                        # 此处不重复声明，避免同一 Artifact 被解析两次。
                        "input_artifact_ids": retained_inputs,
                    }
                )

        return candidate.model_copy(update={"subtasks": list(updated.values())})

    @staticmethod
    def _normalize_historical_reuse(
        candidate: TaskDecompositionResult,
        *,
        current_context: SharedTaskContext | None,
        available_artifacts: list[ArtifactRef],
    ) -> TaskDecompositionResult:
        """让复用声明只包含本轮新子任务实际读取的可信 Artifact。

        当前 TaskSpec 的来源清单会跨轮次保留，但来源存在不等于本轮再次消费。这里以
        子任务显式输入为准，自动补齐真实历史输入并移除模型泛化列出的无关产物。新任务
        尚无 SharedContext，但已选 PublishedOntology 是请求作用域中的受控导入副本；只要
        确定性编译器已把它声明为复用且子任务确实消费，就必须保留给复用校验。这样既不
        绕过 ArtifactReuseValidator，也不会错误清空新任务选择的已发布本体。
        """

        historical_ids = (
            {item.artifact_id for item in current_context.artifact_refs}
            if current_context is not None
            else set()
        )
        request_ids = {item.artifact_id for item in available_artifacts}
        declared_ids = set(candidate.reused_artifact_ids)
        consumed_ids = {
            artifact_id
            for subtask in candidate.subtasks
            for artifact_id in subtask.input_artifact_ids
        }
        eligible_ids = historical_ids.union(declared_ids.intersection(request_ids))
        reused_ids = sorted(eligible_ids.intersection(consumed_ids), key=str)
        return candidate.model_copy(update={"reused_artifact_ids": reused_ids})

    @staticmethod
    def _explicit_graph_publish_request(message: str) -> bool:
        """识别当前用户是否明确要求发布现有图谱，不从历史消息继承副作用授权。"""

        marker = "当前用户消息："
        current = message.rsplit(marker, maxsplit=1)[-1] if marker in message else message
        normalized = "".join(current.lower().split())
        if any(
            phrase in normalized
            for phrase in ("不发布", "不要发布", "无需发布", "不用发布", "不入库")
        ):
            return False
        if any(
            phrase in normalized
            for phrase in ("发布图谱", "图谱发布", "确认入库", "图谱入库", "写入图谱")
        ):
            return True
        # 与 SubTaskValidator 使用同一业务语义：实体、关系、三元组或知识“入库”
        # 表示用户明确要求实例知识图谱发布；只有“本体入库”不自动扩展到图谱。
        knowledge_subjects = ("实体", "关系", "三元组", "知识", "图谱")
        ontology_only = "本体" in normalized and not any(
            subject in normalized for subject in knowledge_subjects
        )
        return (
            "入库" in normalized
            and not ontology_only
            and any(subject in normalized for subject in knowledge_subjects)
        )

    @staticmethod
    def _existing_graph_publish_only(
        request: ControllerRequest, current_context: SharedTaskContext
    ) -> bool:
        """识别没有新输入、没有知识修改要求的现有候选图谱发布请求。"""

        existing_ids = {item.artifact_id for item in current_context.artifact_refs}
        if any(item.artifact_id not in existing_ids for item in request.existing_artifacts):
            return False
        if not any(
            item.artifact_type == "CandidateGraph" for item in current_context.artifact_refs
        ):
            return False
        message = "".join(request.user_message.lower().split())
        if any(
            term in message
            for term in (
                "修改",
                "新增",
                "更新",
                "重建",
                "重新",
                "解析",
                "抽取",
                "融合",
                "对齐",
                "上传",
            )
        ):
            return False
        return ControllerNodes._explicit_graph_publish_request(message)


def _rejected_reuse_artifact_ids(issues: list[ValidationIssue]) -> set[UUID]:
    """从复用校验错误中提取被拒绝的 Artifact ID，供同轮修复排除。

    ValidationIssue 当前是项目冻结的公共错误契约，未额外增加 Artifact ID 字段以避免
    改动数据契约；错误消息统一包含 ``Artifact <UUID>``，因此在总控内部用严格 UUID
    正则提取。无法识别的消息会安全忽略，最终门禁仍会拒绝非法复用。
    """

    rejected: set[UUID] = set()
    pattern = re.compile(r"Artifact ([0-9a-fA-F-]{36})")
    for issue in issues:
        if not issue.code.startswith("REUSE_"):
            continue
        match = pattern.search(issue.message)
        if match is not None:
            rejected.add(UUID(match.group(1)))
    return rejected
