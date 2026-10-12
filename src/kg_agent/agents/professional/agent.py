"""实现五类专业 Agent 共用的真实 LLM 运行骨架。

角色能力完全来自 Registry 和版本化 Prompt；本类不硬编码具体专业 Agent 或 Skill 路由。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from itertools import combinations
from time import perf_counter
from typing import Any, Protocol, TypedDict
from uuid import NAMESPACE_URL, UUID, uuid5

from langgraph.graph import END, START, StateGraph

from kg_agent.agents.professional.models import (
    ProfessionalAgentDecision,
    ProfessionalAgentRequest,
    ProfessionalAgentRunOutput,
    SkillCallPlan,
)
from kg_agent.agents.professional.prompts import (
    ProfessionalPromptRepository,
    build_professional_prompt,
    build_professional_repair_prompt,
)
from kg_agent.domain.agent_models import AgentRunResult, QualitySignal, SkillArtifactBinding
from kg_agent.domain.artifact_models import ArtifactCreateCommand, ArtifactRecord
from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.memory_models import MemoryRef
from kg_agent.domain.registry_models import AgentRegistration, SkillRegistration
from kg_agent.domain.skill_models import SkillInvocation, SkillRunResult
from kg_agent.exceptions import ModelGatewayError, SubTaskValidationError
from kg_agent.model_gateway.base import ModelCallContext, ModelGateway
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry, skill_input_type_groups
from kg_agent.validators.professional_plan_validator import (
    ProfessionalPlanIssue,
    ProfessionalPlanValidator,
)

Clock = Callable[[], datetime]


class ArtifactWriter(Protocol):
    """约束专业 Agent 保存 Skill 输出所需的安全 Artifact 入口。"""

    async def create(self, command: ArtifactCreateCommand) -> ArtifactRecord:
        """使用 Assignment 凭证创建不可变 Artifact。"""
        ...


class ProfessionalAgentState(TypedDict, total=False):
    """定义专业 Agent 固定 LangGraph 的内部状态。"""

    request: ProfessionalAgentRequest
    decision: ProfessionalAgentDecision
    issues: list[ProfessionalPlanIssue]
    repair_attempts: int
    plan_mode: str
    result: AgentRunResult


class BaseProfessionalAgent:
    """以注册角色运行专业计划、Skill 和 Artifact 保存。"""

    def __init__(
        self,
        *,
        registration: AgentRegistration,
        model_gateway: ModelGateway,
        agent_registry: AgentRegistry,
        skill_registry: SkillRegistry,
        artifact_registry: ArtifactRegistry,
        artifact_writer: ArtifactWriter,
        prompt_repository: ProfessionalPromptRepository,
        max_repairs: int = 2,
        clock: Clock | None = None,
    ) -> None:
        """绑定一个注册角色及其确定性执行依赖。"""

        if max_repairs < 0:
            raise ValueError("max_repairs 不能小于 0")
        self.registration = registration
        self._model = model_gateway
        self._skills = skill_registry
        self._writer = artifact_writer
        self._prompts = prompt_repository
        self._max_repairs = max_repairs
        self._clock = clock or (lambda: datetime.now(UTC))
        self._validator = ProfessionalPlanValidator(
            agent_registry=agent_registry,
            skill_registry=skill_registry,
            artifact_registry=artifact_registry,
        )
        self._graph = self._build_graph()

    async def run(self, request: ProfessionalAgentRequest) -> ProfessionalAgentRunOutput:
        """执行当前角色的固定 Agent 循环并返回未持久化的标准结果。"""

        if request.assignment.assigned_agent_type != self.registration.agent_type:
            raise SubTaskValidationError("Assignment 不属于当前专业 Agent")
        agent_started = perf_counter()
        state: dict[str, Any] = await self._graph.ainvoke({"request": request})
        result = state["result"]
        metrics = {
            **result.metrics,
            "agent_total_duration_ms": round((perf_counter() - agent_started) * 1000, 3),
        }
        return ProfessionalAgentRunOutput(result=result.model_copy(update={"metrics": metrics}))

    def _build_graph(self) -> Any:
        """编译计划、校验、修复和执行节点；该图不是全局业务 DAG。"""

        graph = StateGraph(ProfessionalAgentState)
        graph.add_node("plan", self._plan)
        graph.add_node("validate", self._validate)
        graph.add_node("repair", self._repair)
        graph.add_node("execute", self._execute)
        graph.add_node("fail", self._fail)
        graph.add_edge(START, "plan")
        graph.add_edge("plan", "validate")
        graph.add_conditional_edges(
            "validate",
            self._route,
            {"execute": "execute", "repair": "repair", "fail": "fail"},
        )
        graph.add_edge("repair", "validate")
        graph.add_edge("execute", END)
        graph.add_edge("fail", END)
        return graph.compile()

    async def _plan(self, state: ProfessionalAgentState) -> ProfessionalAgentState:
        """优先按已发布契约唯一匹配 Skill；只有存在语义歧义时才调用模型选择。"""

        deterministic = self._deterministic_skill_decision(state["request"])
        if deterministic is not None:
            return {
                "decision": deterministic,
                "issues": [],
                "repair_attempts": 0,
                "plan_mode": "DETERMINISTIC_CONTRACT",
            }
        try:
            decision = await self._invoke_model(state["request"])
        except ModelGatewayError as exc:
            return {
                "issues": [ProfessionalPlanIssue(code="MODEL_OUTPUT_INVALID", message=str(exc))],
                "repair_attempts": 0,
                "plan_mode": "MODEL_SELECTION",
            }
        return {
            "decision": decision,
            "issues": [],
            "repair_attempts": 0,
            "plan_mode": "MODEL_SELECTION",
        }

    def _deterministic_skill_decision(
        self, request: ProfessionalAgentRequest
    ) -> ProfessionalAgentDecision | None:
        """在输入、输出和子任务类型唯一确定时生成最小 Skill 调用计划。

        此处只解析 Registry 已发布的权限和 Artifact 契约，不做业务语义推理。若存在
        多个同样合法的最小方案、没有可执行方案或缺少正式输入，则返回 ``None``，继续
        交由专业 Agent 模型判断或向用户请求补充信息。
        """

        expected_outputs = set(request.subtask.expected_output_types)
        if not expected_outputs:
            return None
        available_types = {item.artifact_type for item in request.input_artifacts}
        candidates: list[tuple[SkillRegistration, SkillCallPlan]] = []
        for skill_name in self.registration.allowed_skills:
            skill = self._skills.require(skill_name)
            if request.subtask.subtask_type not in skill.supported_subtask_types:
                continue
            if self.registration.agent_type not in skill.allowed_agents:
                continue
            if skill.lifecycle != "ACTIVE":
                continue
            input_groups = skill_input_type_groups(skill)
            if any(not available_types.intersection(group) for group in input_groups):
                continue
            if not expected_outputs.intersection(skill.output_artifact_types):
                continue
            if skill.name == "publish_ontology":
                # 候选与融合候选互斥；同时出现多份时不能替用户猜选哪个审核版本。
                selected_groups = [
                    [item for item in request.input_artifacts if item.artifact_type in group]
                    for group in input_groups
                ]
                if any(len(items) != 1 for items in selected_groups):
                    continue
                input_ids = [items[0].artifact_id for items in selected_groups]
            else:
                required_inputs = {
                    artifact_type
                    for group in input_groups
                    for artifact_type in group
                    if artifact_type in available_types
                }
                input_ids = [
                    item.artifact_id
                    for item in request.input_artifacts
                    if item.artifact_type in required_inputs
                ]
            candidates.append(
                (
                    skill,
                    SkillCallPlan(skill_name=skill.name, input_artifact_ids=input_ids),
                )
            )
        for size in range(1, min(len(candidates), 8) + 1):
            plans: list[tuple[tuple[SkillRegistration, SkillCallPlan], ...]] = []
            for group in combinations(candidates, size):
                registrations = [item[0] for item in group]
                if size > 1 and any(item.requires_dedicated_subtask for item in registrations):
                    continue
                produced = {
                    output
                    for registration in registrations
                    for output in registration.output_artifact_types
                }
                if expected_outputs <= produced:
                    plans.append(group)
            if len(plans) != 1:
                if plans:
                    return None
                continue
            calls = [item[1] for item in plans[0]]
            selected_names = [item[0].display_name or item[0].name for item in plans[0]]
            return ProfessionalAgentDecision(
                decision_summary=(
                    "根据子任务类型、Agent 权限、输入 Artifact 与预期输出唯一匹配："
                    + "、".join(selected_names)
                ),
                skill_calls=calls,
            )
        return None

    async def _validate(self, state: ProfessionalAgentState) -> ProfessionalAgentState:
        """校验 Skill allowlist、输入类型、输出覆盖和建议子任务引用。"""

        decision = state.get("decision")
        if decision is None:
            return {
                "issues": state.get("issues")
                or [
                    ProfessionalPlanIssue(
                        code="MISSING_DECISION", message="模型没有生成专业 Agent 决策"
                    )
                ]
            }
        request = state["request"]
        return {
            "issues": self._validator.validate(
                decision=decision,
                registration=self.registration,
                input_artifacts=request.input_artifacts,
                expected_output_types=request.subtask.expected_output_types,
            )
        }

    async def _repair(self, state: ProfessionalAgentState) -> ProfessionalAgentState:
        """携带结构化错误调用 LLM 修复完整 Skill 计划。"""

        attempts = state.get("repair_attempts", 0) + 1
        try:
            decision = await self._invoke_model(
                state["request"], previous=state.get("decision"), issues=state.get("issues", [])
            )
        except ModelGatewayError as exc:
            return {
                "repair_attempts": attempts,
                "issues": [ProfessionalPlanIssue(code="MODEL_OUTPUT_INVALID", message=str(exc))],
                "plan_mode": "MODEL_SELECTION",
            }
        return {
            "decision": decision,
            "issues": [],
            "repair_attempts": attempts,
            "plan_mode": "MODEL_SELECTION",
        }

    async def _execute(self, state: ProfessionalAgentState) -> ProfessionalAgentState:
        """顺序调用已校验 Skill，安全保存输出并构造 AgentRunResult。"""

        request = state["request"]
        decision = state["decision"]
        started = self._clock()
        run_id = uuid5(NAMESPACE_URL, f"kg-agent:run:{request.assignment.assignment_id}")
        trace_id = uuid5(NAMESPACE_URL, f"kg-agent:trace:{request.assignment.assignment_id}")
        if decision.requires_user_input:
            result = AgentRunResult(
                task_id=request.context_view.task_id,
                subtask_id=request.context_view.subtask_id,
                assignment_id=request.assignment.assignment_id,
                agent_name=self.registration.agent_type,
                run_id=run_id,
                status=AgentRunStatus.NEEDS_USER_INPUT,
                input_artifact_ids=[item.artifact_id for item in request.input_artifacts],
                requires_user_input=True,
                user_question=decision.user_question,
                proposed_subtasks=decision.proposed_subtasks,
                memory_entries=[
                    MemoryRef(
                        memory_id=uuid5(NAMESPACE_URL, f"kg-agent:memory:{run_id}"),
                        memory_type="SHORT_TERM",
                        summary=decision.decision_summary[:500],
                        source_refs=[str(run_id), str(request.context_view.subtask_id)],
                    )
                ],
                execution_trace_id=trace_id,
                started_at=started,
                finished_at=self._clock(),
            )
            return {"result": result}

        available = {item.artifact_id: item for item in request.input_artifacts}
        newest_by_input_id: dict[UUID, UUID] = {}
        output_ids: list[UUID] = []
        results: list[SkillRunResult] = []
        artifact_bindings: list[SkillArtifactBinding] = []
        skill_durations: dict[str, float] = {}
        for index, call in enumerate(decision.skill_calls):
            invocation_id = uuid5(
                NAMESPACE_URL, f"kg-agent:skill:{request.assignment.assignment_id}:{index}"
            )
            # TaskSpec 中已经固化的抽取范围属于运行时事实，统一以保护参数传给 Skill。
            # 同名键覆盖专业 Agent 的自由参数，避免模型自行扩大范围或绕过校验；具体
            # Skill 是否使用该事实由自己的实现决定，通用 Agent 不硬编码 Skill 名称。
            parameters = {
                **call.parameters,
                "_task_extraction_scope": dict(request.context_view.extraction_scope),
                # 无 Artifact 的独立型 Skill 仍必须获得公开任务目标；这些保护参数来自
                # SharedContext 裁剪视图，模型或用户脚本不能覆盖。
                "_task_goal": request.context_view.goal,
                "_business_goal": request.context_view.business_goal,
                "_completion_criteria": list(request.context_view.completion_criteria),
                "_domain": request.context_view.domain,
                "_constraints": list(request.context_view.constraints),
            }
            invocation = SkillInvocation(
                invocation_id=invocation_id,
                agent_type=self.registration.agent_type,
                skill_name=call.skill_name,
                input_artifacts=[
                    available[newest_by_input_id.get(item, item)]
                    for item in call.input_artifact_ids
                ],
                parameters=parameters,
            )
            skill_started = perf_counter()
            skill_result = await self._skills.invoke(invocation)
            skill_durations[f"skill.{call.skill_name}.duration_ms"] = round(
                (perf_counter() - skill_started) * 1000,
                3,
            )
            results.append(skill_result)
            call_output_ids: list[UUID] = []
            call_output_types: list[str] = []
            for output_index, output in enumerate(skill_result.outputs):
                artifact_id = uuid5(
                    NAMESPACE_URL,
                    f"kg-agent:artifact:{request.assignment.assignment_id}:{index}:{output_index}",
                )
                record = await self._writer.create(
                    ArtifactCreateCommand(
                        artifact_id=artifact_id,
                        artifact_type=output.artifact_type,
                        schema_version=output.schema_version,
                        task_id=request.context_view.task_id,
                        task_spec_revision=request.task_spec_revision,
                        producer_agent=self.registration.agent_type,
                        producer_skill=call.skill_name,
                        producer_run_id=run_id,
                        parent_artifact_ids=output.parent_artifact_ids,
                        ontology_id=output.ontology_id,
                        ontology_version=output.ontology_version,
                        publication_title=output.publication_title,
                        content_summary=output.content_summary,
                        media_type=output.media_type,
                        content=output.content,
                        assignment_id=request.assignment.assignment_id,
                        worker_id=request.worker_id,
                        fencing_token=request.assignment.fencing_token,
                    )
                )
                output_ids.append(record.ref.artifact_id)
                call_output_ids.append(record.ref.artifact_id)
                call_output_types.append(record.ref.artifact_type)
                # 同一 Agent 连续调用 Skill 时，将同类型变换的最新产物绑定到后续输入。
                available[record.ref.artifact_id] = record.ref
                for parent_id in record.ref.parent_artifact_ids:
                    parent = available.get(parent_id)
                    if parent is not None and parent.artifact_type == record.ref.artifact_type:
                        newest_by_input_id[parent_id] = record.ref.artifact_id
                        for original_id, latest_id in list(newest_by_input_id.items()):
                            if latest_id == parent_id:
                                newest_by_input_id[original_id] = record.ref.artifact_id
            artifact_bindings.append(
                SkillArtifactBinding(
                    invocation_id=invocation_id,
                    skill_name=call.skill_name,
                    input_artifact_ids=[item.artifact_id for item in invocation.input_artifacts],
                    output_artifact_ids=call_output_ids,
                    output_artifact_types=call_output_types,
                )
            )
        status = self._aggregate_status(results)
        expected = set(request.subtask.expected_output_types)
        actual_types = {output.artifact_type for item in results for output in item.outputs}
        blocking = [item.error_message for item in results if item.error_message]
        # 预期输出的最终确定性检查在 Worker 加载完整 AgentSubTask 后再次执行。
        if expected - actual_types and status is AgentRunStatus.SUCCESS:
            status = AgentRunStatus.PARTIAL_SUCCESS
            blocking.append("实际输出未覆盖 AgentSubTask.expected_output_types")
        quality = [
            QualitySignal(name=name, value=value)
            for item in results
            for name, value in item.quality_signals.items()
        ]
        # 长期经验不能只保存“选择了哪个 Skill”一类决策话术。这里用运行时已经
        # 验证的目标、Agent、Skill 和真实输出类型组成可检索摘要，不让模型补造结果。
        experience_summary = (
            f"任务目标：{request.context_view.goal[:240]}；"
            f"执行 Agent：{self.registration.name}；"
            f"成功 Skill：{', '.join(item.skill_name for item in results)}；"
            f"输出类型：{', '.join(sorted(actual_types))}；"
            f"执行摘要：{decision.decision_summary[:300]}"
        )
        result = AgentRunResult(
            task_id=request.context_view.task_id,
            subtask_id=request.context_view.subtask_id,
            assignment_id=request.assignment.assignment_id,
            agent_name=self.registration.agent_type,
            run_id=run_id,
            status=status,
            input_artifact_ids=[item.artifact_id for item in request.input_artifacts],
            output_artifact_ids=output_ids,
            skill_artifact_bindings=artifact_bindings,
            skills_used=[item.skill_name for item in results],
            quality_signals=quality,
            blocking_issues=blocking,
            proposed_subtasks=decision.proposed_subtasks,
            retryable=any(item.retryable for item in results),
            metrics={
                "skill_calls": len(results),
                "output_artifacts": len(output_ids),
                "skill_selection_mode": state.get("plan_mode", "MODEL_SELECTION"),
                **skill_durations,
            },
            # 只有完整成功并产出真实 Artifact 的运行才是可复用经验。失败、部分成功
            # 和阻塞结果仍保留在执行日志与 AgentRunResult 中，但不得污染长期记忆。
            memory_entries=(
                [
                    MemoryRef(
                        memory_id=uuid5(NAMESPACE_URL, f"kg-agent:memory:{run_id}"),
                        memory_type="EXPERIENCE",
                        summary=experience_summary[:800],
                        source_refs=[str(run_id), *[str(item) for item in output_ids]],
                    )
                ]
                if status is AgentRunStatus.SUCCESS and output_ids and not blocking
                else []
            ),
            execution_trace_id=trace_id,
            started_at=started,
            finished_at=self._clock(),
        )
        return {"result": result}

    async def _route(self, state: ProfessionalAgentState) -> str:
        """选择执行、修复或在修复次数耗尽后失败。

        与总控 Agent 一样使用异步条件路由，避免 Skill 计划修复环反复切换到
        同步线程池，从而让同一事件循环稳定推进专业 Agent 状态机。
        """

        if not state.get("issues"):
            return "execute"
        return "repair" if state.get("repair_attempts", 0) < self._max_repairs else "fail"

    async def _fail(self, state: ProfessionalAgentState) -> ProfessionalAgentState:
        """计划持续非法时终止，确保任何 Skill 和 Artifact 都未执行。"""

        detail = "; ".join(f"{item.code}: {item.message}" for item in state.get("issues", []))
        raise SubTaskValidationError(f"专业 Agent Skill 计划无法通过校验：{detail}")

    async def _invoke_model(
        self,
        request: ProfessionalAgentRequest,
        *,
        previous: ProfessionalAgentDecision | None = None,
        issues: list[ProfessionalPlanIssue] | None = None,
    ) -> ProfessionalAgentDecision:
        """把角色化 Context View 和当前 allowlist 提交给 ModelGateway。"""

        skills = [self._skills.require(name) for name in self.registration.allowed_skills]
        prompt = build_professional_prompt(
            request=request, registration=self.registration, skills=skills
        )
        if issues:
            prompt = build_professional_repair_prompt(
                base_prompt=prompt, previous=previous, issues=issues
            )
        return await self._model.generate_structured(
            system_prompt=self._prompts.load_system_prompt(self.registration),
            user_prompt=prompt,
            output_type=ProfessionalAgentDecision,
            call_context=ModelCallContext(
                component="PROFESSIONAL_AGENT",
                actor_name=self.registration.agent_type,
                task_id=request.context_view.task_id,
                subtask_id=request.subtask.subtask_id,
                assignment_id=request.assignment.assignment_id,
                attempt=1 if issues else 0,
            ),
        )

    @staticmethod
    def _aggregate_status(results: list[SkillRunResult]) -> AgentRunStatus:
        """按失败优先级聚合多次 Skill 调用的公开运行状态。"""

        statuses = {item.status for item in results}
        for status in (
            AgentRunStatus.FAILED,
            AgentRunStatus.RETRYABLE_FAILED,
            AgentRunStatus.REWORK_REQUIRED,
            AgentRunStatus.PARTIAL_SUCCESS,
        ):
            if status in statuses:
                return status
        return AgentRunStatus.SUCCESS
