---
name: parse_document
version: 1.0.0
description: 优先提取 DOCX 正文与表格文字，并生成结构块和可追溯语义片段；其他格式使用当前执行模式的联通实现。
owner_agent: document_parsing_agent
supported_subtask_types: [document_parse]
input_artifact_types: [RawDocument]
output_artifact_types: [ParsedDocument, SemanticDocument]
parameters_schema: {}
side_effect: false
timeout_seconds: 90
max_retries: 2
lifecycle: ACTIVE
---

# parse_document

## 执行说明

你负责把原始文档转换为可追溯的 ParsedDocument 与 SemanticDocument。

保留每一段内容对应的 source_artifact_id 和 locator；文本、表格、图片和扫描内容必须以真实可见信息为依据。无法可靠读取的内容应保守省略，不能编造文字、数值、实体或关系。输出语义片段时保持原意，不加入业务推断。
