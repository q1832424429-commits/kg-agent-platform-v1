"""定义用户自定义 Skill 可选择的受控输入输出契约模板。

模板只复用 Artifact Registry 中已经注册的业务类型。大模型可以从模板中推荐最符合
用户描述的一项，但不能创造 Artifact 名称、修改权限或绕过代码兼容性校验。
"""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, Field

from kg_agent.exceptions import CustomExtensionValidationError
from kg_agent.extensions.contracts import (
    IntegrationMode,
    ResultSemantics,
    SkillContractOption,
)
from kg_agent.registries.artifact_registry import ArtifactRegistry


@dataclass(frozen=True, slots=True)
class SkillContractProfile:
    """保存一个可由业务用户选择、由运行时严格执行的 Skill 契约模板。"""

    profile_id: str
    business_name: str
    integration_mode: IntegrationMode
    input_artifact_types: tuple[str, ...]
    output_artifact_types: tuple[str, ...]
    result_semantics: ResultSemantics
    input_description: str
    output_description: str
    suggested_position: str

    def public_option(self) -> SkillContractOption:
        """转换为不暴露内部 Artifact 名称的前端业务选项。"""

        return SkillContractOption(
            profile_id=self.profile_id,
            business_name=self.business_name,
            integration_mode=self.integration_mode,
            input_description=self.input_description,
            output_description=self.output_description,
            suggested_position=self.suggested_position,
        )


class SkillProfileSelection(BaseModel):
    """约束模型只能返回已有 Profile 标识、置信度和公开理由。"""

    profile_id: str
    confidence: float = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=2_000)


class ContractProfileRegistry:
    """管理全部受控自定义 Skill 契约，并在启动时校验 Artifact 注册事实。"""

    LEGACY_DEFAULT_PROFILE = "document_table_analysis"

    def __init__(
        self, artifact_registry: ArtifactRegistry, profiles: tuple[SkillContractProfile, ...]
    ) -> None:
        """注册模板并拒绝重复 ID 或未注册 Artifact 类型。"""

        self._profiles: dict[str, SkillContractProfile] = {}
        for profile in profiles:
            if profile.profile_id in self._profiles:
                raise CustomExtensionValidationError(
                    f"自定义 Skill 契约模板重复：{profile.profile_id}"
                )
            for artifact_type in (
                *profile.input_artifact_types,
                *profile.output_artifact_types,
            ):
                artifact_registry.require(artifact_type)
            self._profiles[profile.profile_id] = profile

    @classmethod
    def v1(cls, artifact_registry: ArtifactRegistry) -> ContractProfileRegistry:
        """创建第一版受控模板；业务名称面向非开发者，类型只在后端使用。"""

        profiles = (
            SkillContractProfile(
                "document_table_analysis",
                "文档表格分析",
                "SIDE_OUTPUT",
                ("SemanticDocument",),
                ("StructuredDataset",),
                "REPORT",
                "解析后的业务文档",
                "可筛选和导出的结构化表格",
                "文档解析完成后，直接作为用户结果",
            ),
            SkillContractProfile(
                "document_text_report",
                "文档文字报告",
                "SIDE_OUTPUT",
                ("SemanticDocument",),
                ("TextReport",),
                "REPORT",
                "解析后的业务文档",
                "包含摘要、章节和证据的文字报告",
                "文档解析完成后，直接作为用户结果",
            ),
            SkillContractProfile(
                "standalone_text_report",
                "纯问题分析",
                "STANDALONE",
                (),
                ("TextReport",),
                "REPORT",
                "用户目标、当前问题和授权记忆摘要",
                "可直接阅读的文字报告",
                "独立任务，不依赖知识图谱主流程",
            ),
            SkillContractProfile(
                "document_preprocess",
                "文档内容预处理",
                "PIPELINE",
                ("SemanticDocument",),
                ("SemanticDocument",),
                "TRANSFORM",
                "解析后的业务文档",
                "处理后仍可进入知识抽取的语义文档",
                "文档解析后、知识抽取前",
            ),
            SkillContractProfile(
                "entity_transform",
                "实体补充与规范化",
                "PIPELINE",
                ("EntityDataset",),
                ("EntityDataset",),
                "TRANSFORM",
                "已经抽取的实体",
                "补充或规范化后的实体",
                "知识抽取后、知识融合前",
            ),
            SkillContractProfile(
                "relation_transform",
                "关系补充与修正",
                "PIPELINE",
                ("RelationDataset", "EvidenceDataset"),
                ("RelationDataset",),
                "TRANSFORM",
                "已经抽取的关系及其证据",
                "补充或修正后的关系",
                "知识抽取后、知识融合前",
            ),
            SkillContractProfile(
                "property_transform",
                "属性补充与标准化",
                "PIPELINE",
                ("PropertyDataset",),
                ("PropertyDataset",),
                "TRANSFORM",
                "已经抽取的属性",
                "补充或标准化后的属性",
                "知识抽取后、知识融合前",
            ),
            SkillContractProfile(
                "knowledge_rule_check",
                "知识规则检查",
                "PIPELINE",
                ("EntityDataset", "RelationDataset", "PropertyDataset", "EvidenceDataset"),
                ("ConflictReport",),
                "DERIVE",
                "实体、关系、属性及证据",
                "可供后续处理或用户查看的冲突报告",
                "知识融合或候选图谱构建前",
            ),
            SkillContractProfile(
                "graph_change_analysis",
                "图谱增量分析",
                "PIPELINE",
                ("PublishedGraph", "SemanticDocument"),
                ("GraphChangeSet",),
                "DERIVE",
                "已发布图谱和新解析文档",
                "待审核的图谱变更集合",
                "已发布图谱的增量更新阶段",
            ),
            SkillContractProfile(
                "structured_data_compare",
                "通用数据对比",
                "STANDALONE",
                ("StructuredDataset",),
                ("StructuredDataset",),
                "REPORT",
                "已有结构化业务结果",
                "对比、筛选或统计后的结构化结果",
                "独立分析或旁路结果",
            ),
        )
        return cls(artifact_registry, profiles)

    def require(self, profile_id: str | None) -> SkillContractProfile:
        """返回指定模板；历史请求为空时保持旧版文档表格分析行为。"""

        resolved = profile_id or self.LEGACY_DEFAULT_PROFILE
        profile = self._profiles.get(resolved)
        if profile is None:
            raise CustomExtensionValidationError(f"未知自定义 Skill 能力模板：{resolved}")
        return profile

    def list_all(self) -> list[SkillContractProfile]:
        """按稳定 ID 返回全部模板，供模型选择和前端确认。"""

        return [self._profiles[key] for key in sorted(self._profiles)]
