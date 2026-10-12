"""连接自定义 Skill 的隔离脚本 Runner 与既有 Artifact 输入输出契约。

专业 Agent 仍只调用 Skill Registry；本模块不在主进程导入或执行用户脚本。
"""

from __future__ import annotations

import base64
import json

from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillArtifactOutput, SkillInvocation, SkillRunResult
from kg_agent.extensions.script_contract_validation import (
    FilesystemScriptRunnerClient,
    ScriptRunnerClient,
    normalize_script_result,
    validate_entity_transform_result,
    validate_property_transform_result,
)
from kg_agent.extensions.script_field_mapping import project_script_input_fields
from kg_agent.extensions.service import CustomExtensionService
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.skills.llm import ArtifactContentReader


class ScriptSkill:
    """把固定修订的脚本包送往无网络 Runner，并校验其候选输出。"""

    def __init__(
        self,
        registration: SkillRegistration,
        *,
        extensions: CustomExtensionService,
        artifact_reader: ArtifactContentReader,
        artifact_registry: ArtifactRegistry,
        runner: ScriptRunnerClient | None = None,
    ) -> None:
        """绑定不可变版本、资源读取服务和已有 Artifact 内容注册表。"""

        self._registration = registration
        self._extensions = extensions
        self._reader = artifact_reader
        self._artifacts = artifact_registry
        self._runner = runner or FilesystemScriptRunnerClient()

    @property
    def name(self) -> str:
        """返回注册的 Skill 名称。"""

        return self._registration.name

    async def execute(self, invocation: SkillInvocation) -> SkillRunResult:
        """发送真实输入至隔离 Runner；失败时不产生任何正式 Artifact。"""

        try:
            revision = int(self._registration.version.removeprefix("custom-"))
            bundle = await self._extensions.load_script_bundle(self.name, revision)
            inputs: list[dict[str, object]] = []
            for ref in invocation.input_artifacts:
                content = await self._reader.read_content(ref.artifact_id)
                if ref.artifact_type == "RawDocument":
                    value: object = {"content_base64": base64.b64encode(content).decode("ascii")}
                else:
                    value = self._artifacts.serialize_and_validate(
                        artifact_type=ref.artifact_type,
                        schema_version=ref.schema_version,
                        content=json.loads(content),
                    )
                    value = json.loads(value)
                inputs.append(
                    {
                        "artifact_id": str(ref.artifact_id),
                        "artifact_type": ref.artifact_type,
                        "content": value,
                    }
                )
            response = await self._runner.execute(
                entrypoint=bundle.entrypoint,
                resources=bundle.resources,
                input_payload={
                    "inputs": inputs,
                    "parameters": {
                        **invocation.parameters,
                        "input_fields": project_script_input_fields(
                            inputs, list(bundle.input_bindings)
                        ),
                        **(
                            {
                                "entity_record_adapter": bundle.entity_record_adapter.model_dump(
                                    mode="json"
                                )
                            }
                            if bundle.entity_record_adapter
                            else {}
                        ),
                    },
                },
                timeout_seconds=min(self._registration.timeout_seconds, 300),
            )
            if "error_code" in response:
                return self._failed(
                    invocation,
                    str(response["error_code"]),
                    str(response.get("error_message", "执行失败")),
                )
            parsed = normalize_script_result(
                response.get("result"),
                tuple(self._registration.output_artifact_types),
                inputs,
                bundle.output_bindings,
                self._artifacts,
            )
            if self._registration.input_artifact_types == [
                "EntityDataset"
            ] and self._registration.output_artifact_types == ["EntityDataset"]:
                validate_entity_transform_result(inputs, parsed)
            if self._registration.input_artifact_types == [
                "PropertyDataset"
            ] and self._registration.output_artifact_types == ["PropertyDataset"]:
                validate_property_transform_result(inputs, parsed)
            actual_types = [item.artifact_type for item in parsed.outputs]
            if len(actual_types) != len(set(actual_types)):
                raise ValueError("脚本重复返回同一 Artifact 类型")
            if set(actual_types) != set(self._registration.output_artifact_types):
                raise ValueError("脚本输出 Artifact 类型与 Skill 声明不一致")
            parent_ids = [item.artifact_id for item in invocation.input_artifacts]
            outputs = []
            for item in parsed.outputs:
                definition = self._artifacts.require(item.artifact_type)
                content = self._artifacts.serialize_and_validate(
                    artifact_type=item.artifact_type,
                    schema_version=definition.schema_version,
                    content=item.content,
                )
                outputs.append(
                    SkillArtifactOutput(
                        artifact_type=item.artifact_type,
                        schema_version=definition.schema_version,
                        content=json.loads(content),
                        content_summary=item.content_summary,
                        parent_artifact_ids=parent_ids,
                    )
                )
            return SkillRunResult(
                invocation_id=invocation.invocation_id,
                skill_name=self.name,
                status=AgentRunStatus.SUCCESS,
                outputs=outputs,
                quality_signals=parsed.quality_signals,
            )
        except Exception as exc:
            return self._failed(invocation, "SCRIPT_SKILL_FAILED", str(exc))

    def _failed(self, invocation: SkillInvocation, code: str, message: str) -> SkillRunResult:
        """把隔离执行或结构化校验失败归一化为不可重试结果。"""

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.FAILED,
            error_code=code,
            error_message=message[:2000],
            retryable=False,
        )
