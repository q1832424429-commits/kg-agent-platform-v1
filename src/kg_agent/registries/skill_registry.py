"""加载 Skill 配置、执行启动期一致性校验并实施调用权限控制。

Registry 不选择 Skill，也不承担专业 Agent 决策；它只保证已选择 Skill 的身份、权限、
输入输出契约和运行实现一致。
"""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
from typing import Protocol, cast

import yaml
from pydantic import ValidationError

from kg_agent.domain.enums import AgentRunStatus, ArtifactStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillInvocation, SkillRunResult
from kg_agent.exceptions import (
    RegistryValidationError,
    SkillExecutionError,
    SkillPermissionError,
)
from kg_agent.model_gateway.base import ModelGateway
from kg_agent.registries.agent_registry import AgentRegistry
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.skills.base import Skill
from kg_agent.skills.llm import ArtifactContentReader, LLMGeneratedSkill, UnconfiguredLLMSkill
from kg_agent.skills.prompt_repository import SkillPromptRepository


def _load_skill_permissions(directory: Path) -> dict[str, list[str]]:
    """读取全局权限清单，作为 Agent 调用 Skill 的唯一授权来源。"""

    permission_path = directory.parents[3] / "configs" / "skills" / "v1.yaml"
    try:
        payload = yaml.safe_load(permission_path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError, TypeError) as exc:
        raise RegistryValidationError(f"无法读取全局 Skill 权限清单：{permission_path}") from exc
    skills = payload.get("skills") if isinstance(payload, dict) else None
    if not isinstance(skills, dict):
        raise RegistryValidationError("全局 Skill 权限清单必须包含 skills 映射")
    permissions: dict[str, list[str]] = {}
    for skill_name, agents in skills.items():
        if not isinstance(skill_name, str) or not isinstance(agents, list):
            raise RegistryValidationError("全局 Skill 权限清单格式无效")
        if not agents or not all(isinstance(agent, str) and agent for agent in agents):
            raise RegistryValidationError(f"Skill {skill_name} 必须至少授权一个专业 Agent")
        permissions[skill_name] = list(agents)
    return permissions


def skill_input_type_groups(registration: SkillRegistration) -> tuple[tuple[str, ...], ...]:
    """返回 Skill 的必需输入组；每组满足其中一种类型即可。

    除已批准本体发布外，每组只有原注册类型一种选择；该发布 Skill 的
    CandidateOntology 与 FusedCandidateOntology 是同一审核目标位置的互斥替代。
    """

    if registration.name == "publish_ontology" and registration.input_artifact_types == [
        "CandidateOntology",
        "OntologyApproval",
    ]:
        return (
            ("FusedCandidateOntology", "CandidateOntology"),
            ("OntologyApproval",),
        )
    return tuple((artifact_type,) for artifact_type in registration.input_artifact_types)


def missing_skill_input_types(registration: SkillRegistration, actual_types: set[str]) -> list[str]:
    """列出没有任何可用类型的必需输入组，供规划与运行门禁共用。"""

    return [
        " 或 ".join(group)
        for group in skill_input_type_groups(registration)
        if not set(group).intersection(actual_types)
    ]


class SideEffectAuthorizer(Protocol):
    """约束发布类 Skill 在执行前必须调用的确定性授权门禁。"""

    async def authorize(self, invocation: SkillInvocation) -> None:
        """验证当前副作用调用；不合法时抛出业务异常。"""
        ...


class SkillRuntimeFactory(Protocol):
    """约束每个独立 Skill 模块暴露的运行实例构造函数。"""

    def __call__(
        self,
        registration: SkillRegistration,
        *,
        model_gateway: ModelGateway,
        artifact_reader: ArtifactContentReader,
        artifact_registry: ArtifactRegistry,
        skill_prompt: str,
    ) -> Skill:
        """以统一基础设施构造当前 Skill 的独立运行实现。"""


def _build_runtime(
    registration: SkillRegistration,
    package_directory: Path,
    *,
    model_gateway: ModelGateway,
    artifact_reader: ArtifactContentReader,
    artifact_registry: ArtifactRegistry,
    skill_prompt: str,
) -> Skill:
    """按 Skill 包目录约定加载当前 Skill 的独立实现工厂。

    模块路径由目录名固定推导为 ``kg_agent.skills.catalog.<skill>.implementation``，不再允许
    YAML 指定任意模块。每个包只负责自身业务策略；模型调用和 Schema 校验仍复用基础层。
    """

    if package_directory.name != registration.name or not registration.name.isidentifier():
        raise RegistryValidationError(f"Skill 包目录必须与合法 Skill 名称一致：{registration.name}")
    module_name = f"kg_agent.skills.catalog.{registration.name}.implementation"
    try:
        module = import_module(module_name)
        factory = cast(SkillRuntimeFactory, module.build_skill)
        runtime = factory(
            registration,
            model_gateway=model_gateway,
            artifact_reader=artifact_reader,
            artifact_registry=artifact_registry,
            skill_prompt=skill_prompt,
        )
    except (ImportError, AttributeError, TypeError, ValueError) as exc:
        raise RegistryValidationError(
            f"无法加载 Skill {registration.name} 的独立实现：{module_name}"
        ) from exc
    if not callable(getattr(runtime, "execute", None)) or not isinstance(
        getattr(runtime, "name", None), str
    ):
        raise RegistryValidationError(f"Skill {registration.name} 的 build_skill 未返回 Skill 实现")
    return runtime


class SkillRegistry:
    """管理 Skill 元数据、运行实例和 Agent allowlist。"""

    def __init__(
        self,
        artifact_registry: ArtifactRegistry,
        *,
        side_effect_authorizer: SideEffectAuthorizer | None = None,
    ) -> None:
        """创建 Registry，并绑定发布类 Skill 的确定性授权门禁。"""

        self._artifact_registry = artifact_registry
        self._side_effect_authorizer = side_effect_authorizer
        self._registrations: dict[str, SkillRegistration] = {}
        # 每个 Skill 只有一个当前执行器。无 Python 资源时该执行器加载 SKILL.md
        # 作为 Agent 的专业指令；有 Python 资源时执行器进入隔离 Runner。
        self._runtimes: dict[str, Skill] = {}

    @classmethod
    def from_directory(
        cls,
        directory: Path,
        *,
        artifact_registry: ArtifactRegistry,
        side_effect_authorizer: SideEffectAuthorizer | None = None,
        model_gateway: ModelGateway | None = None,
        artifact_reader: ArtifactContentReader | None = None,
    ) -> SkillRegistry:
        """从各 Skill 的 Markdown 定义加载契约、执行说明和可选专用实现。"""

        registry = cls(
            artifact_registry,
            side_effect_authorizer=side_effect_authorizer,
        )
        permissions = _load_skill_permissions(directory)
        prompt_repository = SkillPromptRepository()
        for path in sorted(directory.glob("*/SKILL.md")):
            try:
                package_directory = path.parent
                payload, skill_prompt = prompt_repository.load(package_directory)
                if not isinstance(payload, dict):
                    raise RegistryValidationError(f"Skill 元数据必须是对象：{path}")
                skill_name = payload.get("name")
                if not isinstance(skill_name, str) or skill_name not in permissions:
                    raise RegistryValidationError(f"Skill 未在全局权限清单中授权：{skill_name}")
                registration = SkillRegistration.model_validate(
                    {**payload, "allowed_agents": permissions[skill_name]}
                )
                if (
                    package_directory.name != registration.name
                    or not registration.name.isidentifier()
                ):
                    raise RegistryValidationError(
                        f"Skill 包目录必须与合法 Skill 名称一致：{registration.name}"
                    )
                # SKILL.md 是 Skill 的执行说明；只有确有专用算法的 Skill 才保留代码入口。
                runtime: Skill
                if model_gateway is None or artifact_reader is None:
                    # 纯 Registry 契约校验不应为了没有外部依赖而失败；若之后误把
                    # 该实例用于执行，会由 UnconfiguredLLMSkill 明确拒绝，绝不伪造输出。
                    runtime = UnconfiguredLLMSkill(registration)
                else:
                    if (package_directory / "implementation.py").is_file():
                        runtime = _build_runtime(
                            registration,
                            package_directory,
                            model_gateway=model_gateway,
                            artifact_reader=artifact_reader,
                            artifact_registry=artifact_registry,
                            skill_prompt=skill_prompt,
                        )
                    else:
                        runtime = LLMGeneratedSkill(
                            registration,
                            model_gateway=model_gateway,
                            artifact_reader=artifact_reader,
                            artifact_registry=artifact_registry,
                            skill_prompt=skill_prompt,
                        )
                registry.register(registration, runtime)
            except RegistryValidationError:
                raise
            except (OSError, yaml.YAMLError, ValidationError, TypeError) as exc:
                raise RegistryValidationError(f"无法加载 Skill 配置：{path}") from exc
        if not registry._registrations:
            raise RegistryValidationError(f"Skill 配置目录为空：{directory}")
        unregistered_permissions = sorted(set(permissions) - set(registry._registrations))
        if unregistered_permissions:
            raise RegistryValidationError(
                f"全局 Skill 权限清单包含不存在的 Skill：{unregistered_permissions}"
            )
        return registry

    @property
    def side_effects_enabled(self) -> bool:
        """返回发布类 Skill 是否已经绑定确定性授权门禁。"""

        return self._side_effect_authorizer is not None

    def register(
        self,
        registration: SkillRegistration,
        runtime: Skill,
    ) -> None:
        """注册 Skill 元数据及其唯一当前执行器；该扩展点不依赖总控 Agent。"""

        if registration.name in self._registrations:
            raise RegistryValidationError(f"Skill 名称重复注册：{registration.name}")
        if registration.timeout_seconds <= 0 or registration.max_retries < 0:
            raise RegistryValidationError("Skill 超时必须为正数，重试次数不能为负数")
        if not registration.allowed_agents:
            raise RegistryValidationError("Skill 必须声明 allowed_agents")
        if runtime.name != registration.name:
            raise RegistryValidationError("Skill 运行实例名称与注册名称不一致")
        for artifact_type in registration.input_artifact_types + registration.output_artifact_types:
            self._artifact_registry.require(artifact_type)
        self._registrations[registration.name] = registration.model_copy(deep=True)
        self._runtimes[registration.name] = runtime

    def clone_with_permission_extensions(
        self, permission_extensions: dict[str, list[str]]
    ) -> SkillRegistry:
        """复制 Registry，并仅在内存中追加由 Agent YAML 推导的调用权限。

        该方法用于将已发布自定义 Agent 的 permissions.yaml 合并到运行期视图。它不
        修改内置 Skill 文件或数据库中的任何 Skill 元数据，权限的持久化事实仍只有
        Agent 侧 YAML。
        """

        cloned = SkillRegistry(
            self._artifact_registry,
            side_effect_authorizer=self._side_effect_authorizer,
        )
        for name in sorted(self._registrations):
            registration = self._registrations[name]
            additions = permission_extensions.get(name, [])
            expanded = registration.model_copy(
                deep=True,
                update={
                    "allowed_agents": sorted(set(registration.allowed_agents) | set(additions))
                },
            )
            cloned.register(expanded, self._runtimes[name])
        return cloned

    def require(self, skill_name: str) -> SkillRegistration:
        """返回采用 Registry 默认运行模式的 Skill 注册信息。"""

        registration = self._registrations.get(skill_name)
        if registration is None:
            raise RegistryValidationError(f"未知 Skill：{skill_name}")
        return registration.model_copy(deep=True)

    def list_all(self) -> list[SkillRegistration]:
        """按名称稳定返回全部 Skill 注册信息。"""

        return [self.require(key) for key in sorted(self._registrations)]

    def list_for_subtask(
        self,
        *,
        agent_type: str,
        subtask_type: str,
    ) -> list[SkillRegistration]:
        """返回某专业 Agent 针对一个子任务类型可调用的 ACTIVE Skill。

        该查询只读取 Skill 自身声明与全局权限，不替专业 Agent 选择实际执行 Skill。
        总控编译器使用它补齐同一子任务不可拆分的 Artifact 输出集合，避免让模型
        记忆诸如 ``RelationDataset`` 必须伴随 ``EvidenceDataset`` 的机械契约。
        """

        return [
            skill
            for skill in self.list_all()
            if skill.lifecycle == "ACTIVE"
            and agent_type in skill.allowed_agents
            and subtask_type in skill.supported_subtask_types
        ]

    def validate_against_agents(self, agents: AgentRegistry) -> None:
        """启动时双向校验 Agent allowlist、Owner 和输入输出能力矩阵。"""

        for agent in agents.list_all():
            for skill_name in agent.allowed_skills:
                skill = self.require(skill_name)
                if agent.agent_type not in skill.allowed_agents:
                    raise RegistryValidationError(
                        f"Agent {agent.agent_type} 未出现在 Skill {skill_name} allowlist"
                    )
                # owner_agent 只说明内置 Skill 的维护归属，不是第二份调用权限。
                # 调用权唯一来自内置全局清单或自定义 Agent 的 permissions.yaml；否则
                # 用户即使在 YAML 中授权也无法复用既有 Skill，形成隐式反向权限规则。
                unsupported_subtasks = set(skill.supported_subtask_types) - set(
                    agent.accepted_subtask_types
                )
                if unsupported_subtasks:
                    raise RegistryValidationError(
                        f"Skill {skill_name} 的子任务类型超出 Agent {agent.agent_type} 能力声明"
                    )
                unsupported_inputs = set(skill.input_artifact_types) - set(
                    agent.accepted_input_types
                )
                unsupported_outputs = set(skill.output_artifact_types) - set(
                    agent.produced_output_types
                )
                if unsupported_inputs or unsupported_outputs:
                    raise RegistryValidationError(
                        f"Skill {skill_name} 的输入输出超出 Agent {agent.agent_type} 能力声明"
                    )
        for skill in self.list_all():
            if not skill.allowed_agents:
                raise RegistryValidationError(f"Skill {skill.name} 必须声明 allowed_agents")
            if skill.owner_agent is not None:
                agents.require(skill.owner_agent)
                if skill.owner_agent not in skill.allowed_agents:
                    raise RegistryValidationError(
                        f"Skill {skill.name} 的 Owner 必须位于 allowed_agents"
                    )
            for agent_type in skill.allowed_agents:
                agent = agents.require(agent_type)
                if skill.name not in agent.allowed_skills:
                    raise RegistryValidationError(
                        f"Skill {skill.name} 未出现在 Agent {agent_type} allowed_skills"
                    )
                if (
                    "PublishedOntology" in skill.output_artifact_types
                    and not agent.can_publish_ontology
                ):
                    raise RegistryValidationError(f"Agent {agent_type} 没有发布本体权限")
                if "PublishedGraph" in skill.output_artifact_types and not agent.can_publish_graph:
                    raise RegistryValidationError(f"Agent {agent_type} 没有发布图谱权限")

    async def invoke(
        self,
        invocation: SkillInvocation,
    ) -> SkillRunResult:
        """校验权限与输入后调用 Skill 的当前执行器，并验证输出 Artifact Schema。"""

        registration = self.require(invocation.skill_name)
        if invocation.agent_type not in registration.allowed_agents:
            raise SkillPermissionError(
                f"Agent {invocation.agent_type} 无权调用 Skill {invocation.skill_name}"
            )
        if registration.side_effect:
            if self._side_effect_authorizer is None:
                raise SkillPermissionError("有副作用的发布 Skill 尚未开放，必须先接入 PublishGuard")
            await self._side_effect_authorizer.authorize(invocation)
        for artifact in invocation.input_artifacts:
            definition = self._artifact_registry.require(artifact.artifact_type)
            if artifact.status is not ArtifactStatus.AVAILABLE:
                raise SkillExecutionError("Skill 输入 Artifact 必须为 AVAILABLE")
            if artifact.schema_version != definition.schema_version:
                raise SkillExecutionError("Skill 输入 Artifact Schema 版本不兼容")
        actual_types = {item.artifact_type for item in invocation.input_artifacts}
        missing = missing_skill_input_types(registration, actual_types)
        if missing:
            raise SkillExecutionError(f"Skill 缺少必需输入 Artifact：{missing}")
        result = await self._runtimes[registration.name].execute(invocation)
        if (
            result.invocation_id != invocation.invocation_id
            or result.skill_name != registration.name
        ):
            raise SkillExecutionError("Skill 结果身份与当前调用不一致")
        if result.status is AgentRunStatus.SUCCESS and {
            output.artifact_type for output in result.outputs
        } != set(registration.output_artifact_types):
            raise SkillExecutionError("SUCCESS 必须返回注册声明的全部输出类型")
        for output in result.outputs:
            if output.artifact_type not in registration.output_artifact_types:
                raise SkillExecutionError("Skill 返回了注册契约之外的 Artifact 类型")
            self._artifact_registry.serialize_and_validate(
                artifact_type=output.artifact_type,
                schema_version=output.schema_version,
                content=output.content,
            )
        return result
