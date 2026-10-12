"""提供可替换的 DOCX 轻量文字解析实现。

本模块只处理 Word OOXML 文档中的正文段落和表格文字，并输出既有
``DocumentManifest``、``ParsedDocument`` 与 ``SemanticDocument`` 契约。图片 OCR、
页眉页脚、批注、文本框、复杂版式还不在当前临时实现范围内；非 DOCX 输入交给注入
的原有 Skill。
"""

from __future__ import annotations

from io import BytesIO
from typing import Any
from uuid import UUID
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile, is_zipfile

from kg_agent.domain.enums import AgentRunStatus
from kg_agent.domain.registry_models import SkillRegistration
from kg_agent.domain.skill_models import SkillArtifactOutput, SkillInvocation, SkillRunResult
from kg_agent.skills.base import Skill
from kg_agent.skills.llm import ArtifactContentReader

_WORD_DOCUMENT_PATH = "word/document.xml"
_WORD_NAMESPACE = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
_WORD_TAG = f"{{{_WORD_NAMESPACE}}}"
_MAX_DOCUMENT_XML_BYTES = 20 * 1024 * 1024
_MAX_TEXT_CHARS = 2_000_000
_MAX_BLOCKS = 20_000


def _filename_from_summary(summary: str) -> str:
    """从上传 Artifact 的受控摘要中恢复展示文件名。"""

    _, separator, value = summary.partition("：")
    return value.strip() if separator and value.strip() else "uploaded-document"


def _is_docx(raw: bytes, filename: str) -> bool:
    """结合扩展名与 OOXML 核心成员判断输入是否为 DOCX。"""

    stream = BytesIO(raw)
    if not is_zipfile(stream):
        return filename.lower().endswith(".docx")
    stream.seek(0)
    try:
        with ZipFile(stream) as archive:
            return _WORD_DOCUMENT_PATH in archive.namelist()
    except BadZipFile:
        return filename.lower().endswith(".docx")


def _node_text(node: ElementTree.Element) -> str:
    """拼接段落或表格单元格内全部 ``w:t`` 文本节点。"""

    return "".join(text.text or "" for text in node.iter(f"{_WORD_TAG}t"))


def _extract_docx_blocks(raw: bytes) -> list[dict[str, Any]]:
    """按正文顺序提取非空段落及表格行，并保留可追溯定位。"""

    with ZipFile(BytesIO(raw)) as archive:
        info = archive.getinfo(_WORD_DOCUMENT_PATH)
        if info.file_size > _MAX_DOCUMENT_XML_BYTES:
            raise ValueError("word/document.xml 超过临时解析器容量限制")
        document_xml = archive.read(info)

    root = ElementTree.fromstring(document_xml)
    body = root.find(f"{_WORD_TAG}body")
    if body is None:
        return []

    blocks: list[dict[str, Any]] = []
    paragraph_index = 0
    table_index = 0
    total_chars = 0

    def append_block(*, block_type: str, locator: str, text: str, metadata: dict[str, Any]) -> None:
        """追加一个受容量限制的标准 DocumentBlock 字典。"""

        nonlocal total_chars
        normalized = " ".join(text.split())
        if not normalized:
            return
        if len(blocks) >= _MAX_BLOCKS:
            raise ValueError("Word 文档块数量超过临时解析器容量限制")
        total_chars += len(normalized)
        if total_chars > _MAX_TEXT_CHARS:
            raise ValueError("Word 文档文字总量超过临时解析器容量限制")
        blocks.append(
            {
                "block_id": f"block-{len(blocks) + 1}",
                "block_type": block_type,
                "locator": locator,
                "text": normalized,
                "metadata": {"parser": "stdlib-ooxml", **metadata},
            }
        )

    for child in body:
        if child.tag == f"{_WORD_TAG}p":
            paragraph_index += 1
            append_block(
                block_type="TEXT",
                locator=f"word/paragraph:{paragraph_index}",
                text=_node_text(child),
                metadata={"paragraph_index": paragraph_index},
            )
            continue
        if child.tag != f"{_WORD_TAG}tbl":
            continue
        table_index += 1
        for row_index, row in enumerate(child.findall(f"{_WORD_TAG}tr"), start=1):
            cells = [_node_text(cell) for cell in row.findall(f"{_WORD_TAG}tc")]
            append_block(
                block_type="TABLE",
                locator=f"word/table:{table_index}/row:{row_index}",
                text=" | ".join(cell for cell in cells if cell),
                metadata={"table_index": table_index, "row_index": row_index},
            )
    return blocks


class DocxAwareBuildDocumentManifestSkill:
    """为真实 DOCX 本地生成结构清单，其他格式委托当前模式的回退 Skill。"""

    def __init__(
        self,
        registration: SkillRegistration,
        *,
        artifact_reader: ArtifactContentReader,
        fallback: Skill,
    ) -> None:
        """绑定 ``build_document_manifest`` 契约、内容读取器和回退实现。"""

        if registration.name != "build_document_manifest":
            raise ValueError("DOCX 清单包装器只能绑定 build_document_manifest")
        if registration.output_artifact_types != ["DocumentManifest"]:
            raise ValueError("build_document_manifest 输出契约必须是 DocumentManifest")
        self._registration = registration
        self._reader = artifact_reader
        self._fallback = fallback

    @property
    def name(self) -> str:
        """返回稳定 Skill 名称。"""

        return self._registration.name

    async def execute(self, invocation: SkillInvocation) -> SkillRunResult:
        """读取真实 DOCX 并用本地解析结果构建可追溯文档清单。"""

        source = next(
            (item for item in invocation.input_artifacts if item.artifact_type == "RawDocument"),
            None,
        )
        if source is None:
            return self._failure(invocation, "DOCX_INPUT_MISSING", "缺少 RawDocument 输入")
        try:
            raw = await self._reader.read_content(source.artifact_id)
        except Exception as exc:
            return self._failure(
                invocation,
                "DOCX_INPUT_READ_FAILED",
                f"读取 Word 文档失败：{type(exc).__name__}",
                retryable=True,
            )
        filename = _filename_from_summary(source.content_summary)
        if not _is_docx(raw, filename):
            return await self._fallback.execute(invocation)
        try:
            blocks = _extract_docx_blocks(raw)
        except (BadZipFile, KeyError, ElementTree.ParseError, ValueError) as exc:
            return self._failure(
                invocation,
                "DOCX_MANIFEST_FAILED",
                f"Word 结构清单生成失败：{type(exc).__name__}",
            )
        if not blocks:
            return self._failure(
                invocation,
                "DOCX_NO_TEXT",
                "Word 文档没有可登记的正文或表格文字；图片型文档当前需要 OCR",
            )
        elements = [
            {
                "element_id": f"element-{index}",
                "element_type": block["block_type"],
                "locator": block["locator"],
                "summary": block["text"][:200],
            }
            for index, block in enumerate(blocks, start=1)
        ]
        parent_ids = [item.artifact_id for item in invocation.input_artifacts]
        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.SUCCESS,
            outputs=[
                SkillArtifactOutput(
                    artifact_type="DocumentManifest",
                    content={
                        "schema_version": "1.0.0",
                        "source_artifact_id": str(source.artifact_id),
                        "filename": filename,
                        "media_type": (
                            "application/vnd.openxmlformats-officedocument."
                            "wordprocessingml.document"
                        ),
                        "elements": elements,
                    },
                    content_summary=f"{filename} 的 DOCX 结构清单，共 {len(elements)} 个元素",
                    parent_artifact_ids=parent_ids,
                )
            ],
            quality_signals={
                "docx_manifest_built": 1.0,
                "document_element_count": float(len(elements)),
            },
        )

    def _failure(
        self,
        invocation: SkillInvocation,
        code: str,
        message: str,
        *,
        retryable: bool = False,
    ) -> SkillRunResult:
        """把预期内的清单生成问题转换成标准失败结果。"""

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=(AgentRunStatus.RETRYABLE_FAILED if retryable else AgentRunStatus.FAILED),
            error_code=code,
            error_message=message,
            retryable=retryable,
        )


class DocxAwareParseDocumentSkill:
    """优先解析真实 DOCX，其他格式委托原执行模式的回退 Skill。

    该包装器保持 ``parse_document`` 的名称、权限和 Artifact 输入输出完全不变。
    因此后续换成专业文档解析服务时，只需替换本实现，不必修改 Agent 调用链。
    """

    def __init__(
        self,
        registration: SkillRegistration,
        *,
        artifact_reader: ArtifactContentReader,
        fallback: Skill,
    ) -> None:
        """绑定注册契约、Artifact 内容读取器和非 DOCX 回退实现。

        Args:
            registration: ``parse_document`` 的版本化注册信息。
            artifact_reader: 读取 RawDocument 原始字节的受控服务。
            fallback: 当前 LLM 解析实现。

        Raises:
            ValueError: 注册的 Skill 名称或输出契约不是 ``parse_document`` 时抛出。
        """

        if registration.name != "parse_document":
            raise ValueError("DOCX 解析包装器只能绑定 parse_document")
        if set(registration.output_artifact_types) != {
            "ParsedDocument",
            "SemanticDocument",
        }:
            raise ValueError("parse_document 输出契约必须包含两类文档 Artifact")
        self._registration = registration
        self._reader = artifact_reader
        self._fallback = fallback

    @property
    def name(self) -> str:
        """返回稳定 Skill 名称。"""

        return self._registration.name

    async def execute(self, invocation: SkillInvocation) -> SkillRunResult:
        """识别并解析 DOCX；非 Word OOXML 文件保持原模式的处理方式。

        Args:
            invocation: 包含一个 RawDocument 引用的标准 Skill 调用。

        Returns:
            DOCX 返回真实文字块和语义片段；其他格式返回回退 Skill 的结果。
        """

        source = next(
            (item for item in invocation.input_artifacts if item.artifact_type == "RawDocument"),
            None,
        )
        if source is None:
            return self._failure(
                invocation,
                code="DOCX_INPUT_MISSING",
                message="parse_document 缺少 RawDocument 输入",
            )

        try:
            raw = await self._reader.read_content(source.artifact_id)
        except Exception as exc:
            return self._failure(
                invocation,
                code="DOCX_INPUT_READ_FAILED",
                message=f"读取 Word 文档失败：{type(exc).__name__}",
                retryable=True,
            )

        filename = _filename_from_summary(source.content_summary)
        if not _is_docx(raw, filename):
            return await self._fallback.execute(invocation)

        try:
            blocks = _extract_docx_blocks(raw)
        except (BadZipFile, KeyError, ElementTree.ParseError, ValueError) as exc:
            return self._failure(
                invocation,
                code="DOCX_PARSE_FAILED",
                message=f"Word OOXML 解析失败：{type(exc).__name__}",
            )
        if not blocks:
            return self._failure(
                invocation,
                code="DOCX_NO_TEXT",
                message="Word 文档没有可提取的正文或表格文字；图片型文档当前需要 OCR",
            )
        return self._success(invocation, source.artifact_id, filename, blocks)

    def _success(
        self,
        invocation: SkillInvocation,
        source_artifact_id: UUID,
        filename: str,
        blocks: list[dict[str, Any]],
    ) -> SkillRunResult:
        """把解析块转换成现有 ParsedDocument 与 SemanticDocument 输出。"""

        segments = [
            {
                "segment_id": f"segment-{index}",
                "text": block["text"],
                "evidence": [
                    {
                        "source_artifact_id": str(source_artifact_id),
                        "locator": block["locator"],
                        "quote": block["text"][:500],
                    }
                ],
                "metadata": {
                    "source_block_id": block["block_id"],
                    "block_type": block["block_type"],
                    "parser": "stdlib-ooxml",
                },
            }
            for index, block in enumerate(blocks, start=1)
        ]
        parent_ids = [item.artifact_id for item in invocation.input_artifacts]
        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=AgentRunStatus.SUCCESS,
            outputs=[
                SkillArtifactOutput(
                    artifact_type="ParsedDocument",
                    content={
                        "schema_version": "1.0.0",
                        "source_artifact_id": str(source_artifact_id),
                        "blocks": blocks,
                    },
                    content_summary=f"{filename} 的轻量 DOCX 解析结果，共 {len(blocks)} 个文字块",
                    parent_artifact_ids=parent_ids,
                ),
                SkillArtifactOutput(
                    artifact_type="SemanticDocument",
                    content={
                        "schema_version": "1.0.0",
                        "source_artifact_ids": [str(source_artifact_id)],
                        "segments": segments,
                    },
                    content_summary=f"{filename} 的可追溯语义文本，共 {len(segments)} 个片段",
                    parent_artifact_ids=parent_ids,
                ),
            ],
            quality_signals={
                "docx_text_extracted": 1.0,
                "document_block_count": float(len(blocks)),
            },
        )

    def _failure(
        self,
        invocation: SkillInvocation,
        *,
        code: str,
        message: str,
        retryable: bool = False,
    ) -> SkillRunResult:
        """把预期内解析问题转换为标准 SkillRunResult。"""

        return SkillRunResult(
            invocation_id=invocation.invocation_id,
            skill_name=self.name,
            status=(AgentRunStatus.RETRYABLE_FAILED if retryable else AgentRunStatus.FAILED),
            error_code=code,
            error_message=message,
            retryable=retryable,
        )
