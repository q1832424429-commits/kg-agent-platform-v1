"""定义候选图谱与已发布图谱的只读可视化投影契约。

本模块不改变 ``CandidateGraph`` 或 ``PublishedGraph`` 的不可变业务内容，也不保存
浏览器布局坐标。它只将既有实体、关系、属性和证据组织成适合前端节点—边渲染的公开视图。
"""

from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class GraphPreviewWarning(BaseModel):
    """描述不应阻断查看、但会影响可视化完整性的图谱数据问题。"""

    code: str
    message: str
    affected_ids: list[str] = Field(default_factory=list)


class GraphPreviewNode(BaseModel):
    """描述一个可绘制实体节点及其公开属性和溯源摘要。"""

    id: str
    label: str
    class_id: str
    aliases: list[str] = Field(default_factory=list)
    properties: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class GraphPreviewEdge(BaseModel):
    """描述一条可绘制关系边及其关系类型和溯源摘要。"""

    id: str
    source: str
    target: str
    label: str
    relation_type_id: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class GraphPreviewStatistics(BaseModel):
    """汇总当前投影包含的节点、边、属性和证据数量。"""

    entity_count: int = Field(ge=0)
    relation_count: int = Field(ge=0)
    property_count: int = Field(ge=0)
    evidence_count: int = Field(ge=0)


class GraphPreview(BaseModel):
    """返回一个 CandidateGraph 或 PublishedGraph 的只读可视化数据。"""

    artifact_id: UUID
    artifact_type: str
    task_id: UUID
    nodes: list[GraphPreviewNode] = Field(default_factory=list)
    edges: list[GraphPreviewEdge] = Field(default_factory=list)
    statistics: GraphPreviewStatistics
    warnings: list[GraphPreviewWarning] = Field(default_factory=list)
