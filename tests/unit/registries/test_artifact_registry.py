"""验证 V1 Artifact Registry 的类型、版本和内容 Schema。"""

import pytest

from kg_agent.exceptions import ArtifactValidationError
from kg_agent.registries.artifact_registry import ArtifactRegistry


def test_v1_registry_covers_complete_artifact_family() -> None:
    """V1 主流程类型与自定义终端文字报告类型必须全部注册。"""

    registry = ArtifactRegistry.v1()

    assert len(registry.list_all()) == 23
    assert registry.require("RawDocument").content_kind == "BINARY"
    assert registry.require("PublishedGraph").schema_version == "1.0.0"
    assert registry.require("TextReport").content_kind == "JSON"


def test_json_content_is_validated_and_canonicalized() -> None:
    """相同 JSON 语义应序列化为相同字节，以稳定计算 checksum。"""

    registry = ArtifactRegistry.v1()
    first = registry.serialize_and_validate(
        artifact_type="EntityDataset",
        schema_version="1.0.0",
        content={"schema_version": "1.0.0", "entities": []},
    )
    second = registry.serialize_and_validate(
        artifact_type="EntityDataset",
        schema_version="1.0.0",
        content={"entities": [], "schema_version": "1.0.0"},
    )

    assert first == second


def test_registry_rejects_unknown_version_and_invalid_content() -> None:
    """未知 Schema 版本和缺少必填字段的内容不能进入对象存储。"""

    registry = ArtifactRegistry.v1()

    with pytest.raises(ArtifactValidationError):
        registry.serialize_and_validate(
            artifact_type="CandidateGraph",
            schema_version="2.0.0",
            content={"schema_version": "1.0.0"},
        )
    with pytest.raises(ArtifactValidationError):
        registry.serialize_and_validate(
            artifact_type="DocumentManifest",
            schema_version="1.0.0",
            content={"schema_version": "1.0.0"},
        )
    with pytest.raises(ArtifactValidationError):
        registry.serialize_and_validate(
            artifact_type="RawDocument",
            schema_version="1.0.0",
            content={"not": "bytes"},
        )
