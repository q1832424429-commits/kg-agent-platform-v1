"""实现 V1 Artifact 类型、Schema 版本和内容格式注册中心。

Registry 只做确定性契约查找与校验，不访问数据库或对象存储，也不执行业务算法。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ValidationError

from kg_agent.domain.artifact_content_models import (
    ApprovalContent,
    CandidateGraphContent,
    CandidateOntologyContent,
    ChangeSetContent,
    ConflictReportContent,
    DocumentManifestContent,
    EntityDatasetContent,
    EvidenceDatasetContent,
    ExternalOntologyContent,
    FusedCandidateOntologyContent,
    ParsedDocumentContent,
    PropertyDatasetContent,
    PublishedGraphContent,
    PublishedOntologyContent,
    QualityReportContent,
    RelationDatasetContent,
    SemanticDocumentContent,
    StructuredDatasetContent,
    TextReportContent,
)
from kg_agent.exceptions import ArtifactValidationError, RegistryValidationError


@dataclass(frozen=True, slots=True)
class ArtifactRegistration:
    """声明一个 Artifact 类型的当前 Schema 版本和内容介质。"""

    artifact_type: str
    schema_version: str
    content_kind: Literal["JSON", "BINARY"]
    content_model: type[BaseModel] | None


class ArtifactRegistry:
    """管理 Artifact 内容契约，并产生稳定的规范化 JSON 字节。"""

    def __init__(self, registrations: list[ArtifactRegistration] | None = None) -> None:
        """创建空 Registry，并按顺序注册可选定义。"""

        self._registrations: dict[str, ArtifactRegistration] = {}
        for registration in registrations or []:
            self.register(registration)

    @classmethod
    def v1(cls) -> ArtifactRegistry:
        """创建覆盖 V1 全部 Artifact 家族的内置 Registry。"""

        json_models: dict[str, type[BaseModel]] = {
            "ExternalOntology": ExternalOntologyContent,
            "StructuredDataset": StructuredDatasetContent,
            "TextReport": TextReportContent,
            "DocumentManifest": DocumentManifestContent,
            "ParsedDocument": ParsedDocumentContent,
            "SemanticDocument": SemanticDocumentContent,
            "CandidateOntology": CandidateOntologyContent,
            "FusedCandidateOntology": FusedCandidateOntologyContent,
            "OntologyQualityReport": QualityReportContent,
            "OntologyChangeSet": ChangeSetContent,
            "OntologyApproval": ApprovalContent,
            "PublishedOntology": PublishedOntologyContent,
            "EntityDataset": EntityDatasetContent,
            "RelationDataset": RelationDatasetContent,
            "PropertyDataset": PropertyDatasetContent,
            "EvidenceDataset": EvidenceDatasetContent,
            "ConflictReport": ConflictReportContent,
            "CandidateGraph": CandidateGraphContent,
            "GraphQualityReport": QualityReportContent,
            "GraphChangeSet": ChangeSetContent,
            "GraphApproval": ApprovalContent,
            "PublishedGraph": PublishedGraphContent,
        }
        registrations = [
            ArtifactRegistration("RawDocument", "1.0.0", "BINARY", None),
            *[
                ArtifactRegistration(name, "1.0.0", "JSON", model)
                for name, model in json_models.items()
            ],
        ]
        return cls(registrations)

    def register(self, registration: ArtifactRegistration) -> None:
        """注册一个 Artifact 类型；重复类型会被拒绝。"""

        if registration.artifact_type in self._registrations:
            raise RegistryValidationError(f"Artifact 类型重复注册：{registration.artifact_type}")
        if registration.content_kind == "JSON" and registration.content_model is None:
            raise RegistryValidationError("JSON Artifact 必须声明 content_model")
        if registration.content_kind == "BINARY" and registration.content_model is not None:
            raise RegistryValidationError("BINARY Artifact 不应声明 JSON content_model")
        self._registrations[registration.artifact_type] = registration

    def require(self, artifact_type: str) -> ArtifactRegistration:
        """返回 Artifact 定义；未知类型会被拒绝。"""

        registration = self._registrations.get(artifact_type)
        if registration is None:
            raise ArtifactValidationError(f"未知 Artifact 类型：{artifact_type}")
        return registration

    def list_all(self) -> list[ArtifactRegistration]:
        """按类型名稳定返回全部 Artifact 定义。"""

        return [self._registrations[key] for key in sorted(self._registrations)]

    def serialize_and_validate(
        self,
        *,
        artifact_type: str,
        schema_version: str,
        content: bytes | dict[str, object],
    ) -> bytes:
        """校验内容并返回用于 checksum 和存储的规范化字节。

        Raises:
            ArtifactValidationError: 类型、版本或内容体与 Registry 声明不一致。
        """

        registration = self.require(artifact_type)
        if schema_version != registration.schema_version:
            raise ArtifactValidationError(f"{artifact_type} 不支持 Schema 版本 {schema_version}")
        if registration.content_kind == "BINARY":
            if not isinstance(content, bytes):
                raise ArtifactValidationError(f"{artifact_type} 内容必须是 bytes")
            return content
        if not isinstance(content, dict):
            raise ArtifactValidationError(f"{artifact_type} 内容必须是 JSON 对象")
        assert registration.content_model is not None
        try:
            validated = registration.content_model.model_validate(content)
        except ValidationError as exc:
            raise ArtifactValidationError(
                f"{artifact_type} 内容不符合 Schema {schema_version}"
            ) from exc
        payload = validated.model_dump(mode="json")
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
