"""定义 Artifact 元数据、创建命令、持久化记录和复用判定契约。

大型内容不进入 SharedContext；ArtifactRef 只保存受控地址、校验和及血缘元数据。
Artifact 内容默认不可变，任何修改都必须创建新 Artifact 或 ChangeSet。
"""

from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from kg_agent.domain.enums import ArtifactStatus


class ArtifactRef(BaseModel):
    """指向正式 Artifact 元数据和受控内容地址的轻量引用。"""

    artifact_id: UUID
    artifact_type: str
    schema_version: str
    task_id: UUID
    task_spec_revision: int = Field(ge=1)
    producer_agent: str
    producer_skill: str | None = None
    producer_run_id: UUID
    parent_artifact_ids: list[UUID] = Field(default_factory=list)
    ontology_id: UUID | None = None
    ontology_version: str | None = None
    # 只有真正发布的本体或图谱填写该名称；任务内导入副本不重新取得发布名称。
    publication_title: str | None = Field(default=None, max_length=120)
    content_uri: str
    content_summary: str
    checksum: str
    status: ArtifactStatus
    created_at: datetime


class ArtifactRecord(BaseModel):
    """组合 ArtifactRef 与对象存储内部定位信息。"""

    ref: ArtifactRef
    object_key: str
    media_type: str
    size_bytes: int = Field(ge=0)


class ArtifactCreateCommand(BaseModel):
    """描述一次幂等且受控的 Artifact 内容写入。"""

    artifact_id: UUID = Field(default_factory=uuid4)
    artifact_type: str
    schema_version: str = "1.0.0"
    task_id: UUID
    task_spec_revision: int = Field(ge=1)
    producer_agent: str
    producer_skill: str | None = None
    producer_run_id: UUID
    parent_artifact_ids: list[UUID] = Field(default_factory=list)
    ontology_id: UUID | None = None
    ontology_version: str | None = None
    # 发布 Skill 创建资产时把用户审核名称投影到轻量 ArtifactRef，供资产库高效列出。
    publication_title: str | None = Field(default=None, max_length=120)
    content_summary: str
    media_type: str = "application/json"
    content: bytes | dict[str, Any]
    assignment_id: UUID | None = None
    worker_id: str | None = None
    fencing_token: int | None = Field(default=None, ge=0)
    supersedes_artifact_id: UUID | None = None

    @model_validator(mode="after")
    def validate_execution_claim(self) -> "ArtifactCreateCommand":
        """要求 Assignment 写入凭证必须完整提供，避免部分凭证被误用。"""

        claim_values = (self.assignment_id, self.worker_id, self.fencing_token)
        if any(value is not None for value in claim_values) and not all(
            value is not None for value in claim_values
        ):
            raise ValueError("assignment_id、worker_id 和 fencing_token 必须同时提供")
        return self


class ArtifactReuseRequest(BaseModel):
    """描述调用方对一个历史 Artifact 的确定性兼容性要求。"""

    artifact_id: UUID
    expected_artifact_type: str
    expected_schema_version: str
    compatible_task_spec_revisions: set[int]
    expected_parent_checksums: set[str] = Field(default_factory=set)
    expected_ontology_id: UUID | None = None
    expected_ontology_version: str | None = None


class ArtifactReuseDecision(BaseModel):
    """返回 Artifact 是否可复用及全部拒绝原因代码。"""

    artifact_id: UUID
    reusable: bool
    reason_codes: list[str] = Field(default_factory=list)
