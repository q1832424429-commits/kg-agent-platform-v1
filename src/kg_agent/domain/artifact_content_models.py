"""定义 V1 Artifact 内容体的版本化 Pydantic 契约。

本模块只描述业务产物的数据形状，不执行文档解析、知识抽取、融合或图谱发布算法。
V1 的 LLM Skill 与未来真实算法必须共同遵守这些契约。
"""

from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class ArtifactContent(BaseModel):
    """所有 JSON Artifact 内容体的公共基类。"""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["1.0.0"] = "1.0.0"


class EvidencePointer(BaseModel):
    """定位知识项在来源 Artifact 中的可追溯位置。"""

    source_artifact_id: UUID
    locator: str
    quote: str | None = None


class RawDocumentMetadata(ArtifactContent):
    """描述以二进制方式保存的原始上传文件。"""

    filename: str
    media_type: str
    size_bytes: int = Field(ge=0)


class ExternalOntologyContent(ArtifactContent):
    """描述用户上传的标准 JSON 外部本体。"""

    name: str
    version: str
    classes: list[dict[str, Any]] = Field(default_factory=list)
    relations: list[dict[str, Any]] = Field(default_factory=list)
    properties: list[dict[str, Any]] = Field(default_factory=list)


class StructuredDatasetContent(ArtifactContent):
    """描述标准化表格数据；V1 仅保存列定义和行数据。"""

    columns: list[str]
    rows: list[dict[str, Any]] = Field(default_factory=list)


class ReportSection(BaseModel):
    """描述终端文字报告中的一个可独立展示章节。"""

    heading: str
    content: str


class TextReportContent(ArtifactContent):
    """描述不需要继续进入图谱主链路的可追溯文字报告。"""

    title: str
    summary: str
    sections: list[ReportSection] = Field(default_factory=list)
    evidence: list[EvidencePointer] = Field(default_factory=list)


class DocumentEntry(BaseModel):
    """描述文档清单中的一个结构元素。"""

    element_id: str
    element_type: Literal["TEXT", "TABLE", "IMAGE", "SCAN", "SHEET", "SLIDE"]
    locator: str
    summary: str | None = None


class DocumentManifestContent(ArtifactContent):
    """记录解析前识别到的文档结构及多模态元素。"""

    source_artifact_id: UUID
    filename: str
    media_type: str
    elements: list[DocumentEntry] = Field(default_factory=list)


class DocumentBlock(BaseModel):
    """表示解析后的文本、表格、图片或扫描块。"""

    block_id: str
    block_type: Literal["TEXT", "TABLE", "IMAGE", "SCAN", "SHEET", "SLIDE"]
    locator: str
    text: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class ParsedDocumentContent(ArtifactContent):
    """保存文档解析产生的结构化块。"""

    source_artifact_id: UUID
    blocks: list[DocumentBlock] = Field(default_factory=list)


class SemanticSegment(BaseModel):
    """表示保留证据定位的语义文档片段。"""

    segment_id: str
    text: str
    evidence: list[EvidencePointer] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class SemanticDocumentContent(ArtifactContent):
    """保存供本体和实例知识抽取使用的语义片段。"""

    source_artifact_ids: list[UUID]
    segments: list[SemanticSegment] = Field(default_factory=list)


class OntologyClass(BaseModel):
    """表示候选或发布本体中的一个类。"""

    class_id: str
    name: str
    description: str = ""
    parent_class_ids: list[str] = Field(default_factory=list)


class OntologyRelation(BaseModel):
    """表示本体关系类型及其定义域和值域。"""

    relation_id: str
    name: str
    domain_class_id: str
    range_class_id: str
    description: str = ""


class OntologyProperty(BaseModel):
    """表示本体类的属性约束。"""

    property_id: str
    name: str
    owner_class_id: str
    value_type: str
    required: bool = False


class CandidateOntologyContent(ArtifactContent):
    """描述尚待审核的候选本体。"""

    name: str
    classes: list[OntologyClass] = Field(default_factory=list)
    relations: list[OntologyRelation] = Field(default_factory=list)
    properties: list[OntologyProperty] = Field(default_factory=list)
    source_artifact_ids: list[UUID] = Field(default_factory=list)


class FusedCandidateOntologyContent(CandidateOntologyContent):
    """描述完成对齐和融合、仍待审核的候选本体。"""

    alignment_notes: list[str] = Field(default_factory=list)


class PublishedOntologyContent(CandidateOntologyContent):
    """描述已经审核发布、可供实例抽取使用的固定本体版本。"""

    # 发布名称是用户在人工审核批准时填写的业务名称；它属于不可变发布版本，不能在
    # 后续复用任务中被改写。默认值仅用于兼容早期没有名称的历史 Artifact。
    publication_title: str = Field(default="未命名本体", min_length=1, max_length=120)
    ontology_id: UUID
    ontology_version: str
    approval_artifact_id: UUID


class EntityRecord(BaseModel):
    """表示一个带类型和证据的候选实体。"""

    entity_id: str
    name: str
    class_id: str
    aliases: list[str] = Field(default_factory=list)
    evidence: list[EvidencePointer] = Field(default_factory=list)


class RelationRecord(BaseModel):
    """表示两个实体间的一条候选关系。"""

    relation_id: str
    source_entity_id: str
    relation_type_id: str
    target_entity_id: str
    evidence: list[EvidencePointer] = Field(default_factory=list)


class PropertyRecord(BaseModel):
    """表示一个实体属性值。"""

    property_id: str
    entity_id: str
    property_type_id: str
    value: Any
    evidence: list[EvidencePointer] = Field(default_factory=list)


class EvidenceRecord(BaseModel):
    """表示可独立查询的证据记录。"""

    evidence_id: str
    pointer: EvidencePointer
    confidence: float = Field(ge=0, le=1)


class EntityDatasetContent(ArtifactContent):
    """保存实体抽取或实体对齐结果。"""

    entities: list[EntityRecord] = Field(default_factory=list)


class RelationDatasetContent(ArtifactContent):
    """保存关系抽取或标准化结果。"""

    relations: list[RelationRecord] = Field(default_factory=list)


class PropertyDatasetContent(ArtifactContent):
    """保存属性抽取结果。"""

    properties: list[PropertyRecord] = Field(default_factory=list)


class EvidenceDatasetContent(ArtifactContent):
    """保存知识项的统一证据集合。"""

    evidence: list[EvidenceRecord] = Field(default_factory=list)


class ConflictItem(BaseModel):
    """表示融合阶段发现的一项冲突。"""

    conflict_id: str
    category: str
    description: str
    affected_ids: list[str] = Field(default_factory=list)


class ConflictReportContent(ArtifactContent):
    """保存需要后续处理或人工查看的冲突。"""

    conflicts: list[ConflictItem] = Field(default_factory=list)


class CandidateGraphContent(ArtifactContent):
    """描述待审核候选图谱的实体、关系、属性和证据。"""

    entities: list[EntityRecord] = Field(default_factory=list)
    relations: list[RelationRecord] = Field(default_factory=list)
    properties: list[PropertyRecord] = Field(default_factory=list)
    evidence: list[EvidenceRecord] = Field(default_factory=list)


class QualityIssue(BaseModel):
    """表示质量评估发现的一个可定位问题。"""

    code: str
    severity: Literal["INFO", "WARNING", "ERROR"]
    message: str
    affected_ids: list[str] = Field(default_factory=list)


class QualityReportContent(ArtifactContent):
    """候选本体和候选图谱共用的质量报告结构。"""

    target_artifact_id: UUID
    scores: dict[str, float] = Field(default_factory=dict)
    issues: list[QualityIssue] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    passed: bool


class ChangeOperation(BaseModel):
    """表达候选本体或图谱的一次结构化修改。"""

    operation_id: str
    action: Literal["ADD", "UPDATE", "DELETE", "MERGE", "SPLIT"]
    target_kind: Literal[
        "CLASS", "RELATION_TYPE", "PROPERTY_TYPE", "ENTITY", "RELATION", "PROPERTY"
    ]
    target_id: str | None = None
    value: dict[str, Any] = Field(default_factory=dict)


class ChangeSetContent(ArtifactContent):
    """保存针对一个候选 Artifact 的不可变修改集合。"""

    target_artifact_id: UUID
    target_checksum: str
    operations: list[ChangeOperation] = Field(default_factory=list)


class ApprovalContent(ArtifactContent):
    """保存用户对特定 Artifact 校验和作出的审核决定。"""

    target_artifact_id: UUID
    target_checksum: str
    decision: Literal["APPROVED", "REJECTED", "CHANGES_REQUESTED"]
    # APPROVED 时记录用户为待发布本体或图谱指定的名称；其他审核动作保持为空。
    # 默认值保证此前已经持久化的 Approval 内容仍可按新契约读取。
    publication_title: str = Field(default="", max_length=120)
    comment: str = ""


class PublishedGraphContent(CandidateGraphContent):
    """描述经用户审核并正式发布的固定图谱版本。"""

    # 与 PublishedOntology 相同，发布名称用于用户资产库管理，而非内部图谱 ID。
    publication_title: str = Field(default="未命名图谱", min_length=1, max_length=120)
    graph_id: UUID
    graph_version: str
    approval_artifact_id: UUID
