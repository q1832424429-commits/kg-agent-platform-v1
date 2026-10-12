"""提供总控 Agent 的公开运行入口。"""

from typing import Any

from kg_agent.agents.controller.graph import build_controller_graph
from kg_agent.agents.controller.nodes import ControllerNodes
from kg_agent.agents.controller.prompts import PromptRepository
from kg_agent.agents.controller.state import ControllerRequest, ControllerRunResult
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from kg_agent.services.context_service import ContextService
from kg_agent.validators.artifact_reuse_validator import (
    ArtifactReuseEvaluator,
    ArtifactReuseValidator,
)
from kg_agent.validators.handoff_validator import HandoffValidator
from kg_agent.validators.subtask_validator import SubTaskValidator


class ControllerAgent:
    """执行意图识别、TaskSpec、子任务拆解、分配和发布的真实总控 Agent。"""

    def __init__(
        self,
        *,
        model_gateway: ModelGateway,
        agent_registry: AgentRegistry,
        context_service: ContextService,
        prompt_repository: PromptRepository,
        max_repairs: int = 2,
        artifact_registry: ArtifactRegistry | None = None,
        skill_registry: SkillRegistry | None = None,
        artifact_reuse_evaluator: ArtifactReuseEvaluator | None = None,
    ) -> None:
        """初始化总控 Agent 及其固定 LangGraph。"""

        if max_repairs < 0:
            raise ValueError("max_repairs 不能小于 0")
        artifact_registry = artifact_registry or ArtifactRegistry.v1()
        validator = SubTaskValidator(agent_registry, skill_registry)
        nodes = ControllerNodes(
            model_gateway=model_gateway,
            agent_registry=agent_registry,
            context_service=context_service,
            prompt_repository=prompt_repository,
            skill_registry=skill_registry,
            validator=validator,
            handoff_validator=HandoffValidator(artifact_registry),
            reuse_validator=ArtifactReuseValidator(artifact_registry, artifact_reuse_evaluator),
            max_repairs=max_repairs,
        )
        self._graph = build_controller_graph(nodes)

    async def run(self, request: ControllerRequest) -> ControllerRunResult:
        """根据用户对话生成并发布合法的结构化任务拆解结果。"""

        raw_result: dict[str, Any] = await self._graph.ainvoke({"request": request})
        return ControllerRunResult(
            decomposition=raw_result["candidate"],
            shared_context=raw_result.get("published_context"),
            validation_issues=raw_result.get("validation_issues", []),
        )
