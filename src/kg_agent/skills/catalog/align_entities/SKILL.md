---
name: align_entities
version: 1.0.0
description: 执行实体消歧、去重与对齐。
owner_agent: knowledge_fusion_agent
supported_subtask_types: [entity_align]
input_artifact_types: [EntityDataset]
output_artifact_types: [EntityDataset]
parameters_schema: {}
side_effect: false
timeout_seconds: 180
max_retries: 2
lifecycle: ACTIVE
---

# align_entities

## 执行说明

你负责对 EntityDataset 做实体消歧、别名归并和去重。

只有名称、标识、上下文和证据足够支持同一性的实体才能合并；不确定时保留为不同实体。合并后保留可追溯别名，不得创建没有来源的新实体，也不得改变 class_id 的业务语义。输出中 entity_id 必须唯一。

输出 Artifact 的 content 必须显式包含 entities 数组，实际实体必须写在数组中；不能只在
content_summary 或 quality_signals 中报告数量。输入非空时输出不能为空。若没有实体合并，
逐一保留所有输入 entity_id、class_id 和原有证据；entity_count、original_entity_count
和 merged_count 必须与实际输入输出数量一致。
