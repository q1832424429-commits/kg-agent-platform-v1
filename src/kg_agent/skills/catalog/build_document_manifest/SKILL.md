---
name: build_document_manifest
version: 1.0.0
description: 优先为 DOCX 本地生成正文和表格结构清单；其他格式使用当前执行模式的联通实现。
owner_agent: document_parsing_agent
supported_subtask_types: [document_parse]
input_artifact_types: [RawDocument]
output_artifact_types: [DocumentManifest]
parameters_schema: {}
side_effect: false
timeout_seconds: 60
max_retries: 2
lifecycle: ACTIVE
---

# build_document_manifest

## 执行说明

你负责识别原始文档的可见结构。根据输入 RawDocument 的文件名、媒体类型和可读取内容，输出 DocumentManifest。

只登记确实能够从输入确认的文本、表格、图片、扫描、工作表或幻灯片元素；每个元素必须有稳定 locator。不要猜测未读取到的页面、表格或图片内容。非 DOCX 文件由当前 LLM 联通实现处理，不能宣称完成 OCR 或复杂版式解析。
