"""验证临时 DOCX parse_document Skill 的真实文字提取和契约兼容性。"""

from io import BytesIO
from uuid import UUID, uuid4
from zipfile import ZIP_DEFLATED, ZipFile

import pytest

from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.skill_models import SkillInvocation
from kg_agent.model_gateway.fake import FakeModelAdapter
from kg_agent.registries.artifact_registry import ArtifactRegistry
from kg_agent.registries.skill_registry import SkillRegistry
from tests.factories import make_artifact


class _BytesArtifactReader:
    """按 Artifact ID 返回测试构造的原始文档字节。"""

    def __init__(self, contents: dict[UUID, bytes]) -> None:
        self._contents = contents

    async def read_content(self, artifact_id: UUID) -> bytes:
        """读取指定测试 Artifact 的内容。"""

        return self._contents[artifact_id]


def _docx_bytes() -> bytes:
    """构造包含一个正文段落和两行表格的最小 DOCX OOXML。"""

    document_xml = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>设备 A 属于装配线。</w:t></w:r></w:p>
    <w:tbl>
      <w:tr><w:tc><w:p><w:r><w:t>设备</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>状态</w:t></w:r></w:p></w:tc></w:tr>
      <w:tr><w:tc><w:p><w:r><w:t>机器人 A</w:t></w:r></w:p></w:tc>
        <w:tc><w:p><w:r><w:t>运行</w:t></w:r></w:p></w:tc></w:tr>
    </w:tbl>
    <w:sectPr/>
  </w:body>
</w:document>"""
    stream = BytesIO()
    with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", document_xml)
    return stream.getvalue()


@pytest.mark.asyncio
async def test_registry_parse_document_extracts_docx_paragraphs_and_tables(
    project_root,
) -> None:
    """真实 DOCX 应由本地解析器生成可追溯段落与表格块。"""

    task_id = uuid4()
    source = make_artifact(task_id, artifact_type="RawDocument").model_copy(
        update={"content_summary": "用户上传文件：汽车制造.docx"}
    )
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=FakeModelAdapter([]),
        artifact_reader=_BytesArtifactReader({source.artifact_id: _docx_bytes()}),
    )

    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="document_parsing_agent",
            skill_name="parse_document",
            input_artifacts=[source],
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    outputs = {item.artifact_type: item for item in result.outputs}
    parsed = outputs["ParsedDocument"].content
    semantic = outputs["SemanticDocument"].content
    assert [item["block_type"] for item in parsed["blocks"]] == [
        "TEXT",
        "TABLE",
        "TABLE",
    ]
    assert [item["text"] for item in parsed["blocks"]] == [
        "设备 A 属于装配线。",
        "设备 | 状态",
        "机器人 A | 运行",
    ]
    assert semantic["segments"][0]["evidence"][0]["locator"] == "word/paragraph:1"
    assert result.quality_signals["docx_text_extracted"] == 1.0


@pytest.mark.asyncio
async def test_registry_build_document_manifest_uses_same_local_docx_structure(
    project_root,
) -> None:
    """DOCX 清单不应调用慢速回退实现，并应准确列出正文和表格元素。"""

    task_id = uuid4()
    source = make_artifact(task_id, artifact_type="RawDocument").model_copy(
        update={"content_summary": "用户上传文件：汽车制造.docx"}
    )
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        model_gateway=FakeModelAdapter([]),
        artifact_reader=_BytesArtifactReader({source.artifact_id: _docx_bytes()}),
    )

    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="document_parsing_agent",
            skill_name="build_document_manifest",
            input_artifacts=[source],
        )
    )

    assert result.status is AgentRunStatus.SUCCESS
    manifest = result.outputs[0]
    assert manifest.artifact_type == "DocumentManifest"
    assert manifest.content["filename"] == "汽车制造.docx"
    assert [item["element_type"] for item in manifest.content["elements"]] == [
        "TEXT",
        "TABLE",
        "TABLE",
    ]
    assert manifest.content["elements"][0]["summary"] == "设备 A 属于装配线。"
    assert result.quality_signals["docx_manifest_built"] == 1.0


@pytest.mark.asyncio
async def test_non_docx_requires_configured_llm_runtime(project_root) -> None:
    """PDF 等格式没有模型运行依赖时必须明确失败，不能伪造解析结果。"""

    task_id = uuid4()
    source = make_artifact(task_id, artifact_type="RawDocument").model_copy(
        update={"content_summary": "用户上传文件：设备说明.pdf"}
    )
    registry = SkillRegistry.from_directory(
        project_root / "src" / "kg_agent" / "skills" / "catalog",
        artifact_registry=ArtifactRegistry.v1(),
        artifact_reader=_BytesArtifactReader({source.artifact_id: b"%PDF-1.7"}),
    )

    result = await registry.invoke(
        SkillInvocation(
            invocation_id=uuid4(),
            agent_type="document_parsing_agent",
            skill_name="parse_document",
            input_artifacts=[source],
        )
    )

    assert result.status is AgentRunStatus.FAILED
    assert result.error_code == "LLM_SKILL_RUNTIME_NOT_CONFIGURED"
    assert result.outputs == []
