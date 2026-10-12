---
name: extract_knowledge
version: 1.0.0
description: 基于语义文档和已发布本体联合抽取实体、关系、属性与统一证据。
owner_agent: knowledge_extraction_agent
supported_subtask_types: [knowledge_extract]
input_artifact_types: [SemanticDocument, PublishedOntology]
output_artifact_types: [EntityDataset, RelationDataset, PropertyDataset, EvidenceDataset]
parameters_schema:
  type: object
  properties:
    extraction_components:
      type: array
      items:
        enum: [entities, relations, properties]
  additionalProperties: true
side_effect: false
timeout_seconds: 270
max_retries: 1
lifecycle: ACTIVE
---

# extract_knowledge

## 执行说明

你负责在一次调用中完成实例知识联合抽取。模型响应只包含 `entities`、`relations`、`properties` 三个必填数组；系统从每个知识项的内联 EvidencePointer 去重生成统一 EvidenceDataset，再确定性生成 EntityDataset、RelationDataset、PropertyDataset 与 EvidenceDataset 四种 Artifact。所有事实必须来自输入 SemanticDocument，并受输入 PublishedOntology 约束。

先建立实体集合，再在同一结果中生成关系和属性：`class_id` 必须来自已发布本体的类，`relation_type_id` 必须来自本体关系，`property_type_id` 必须来自本体属性。关系的 source_entity_id、target_entity_id 与属性的 entity_id 只能引用本次 EntityDataset 中真实存在的实体，不得创建悬空引用。

每个实体、关系和属性必须携带至少一个可定位 EvidencePointer。不得额外返回顶层 `evidence`、content_summary、quality_signals 或只声称数量，真实知识记录必须写入三个业务数组；统一证据、摘要和统计由系统根据实际内容生成。

若 parameters.extraction_scope 或 extraction_components 限制范围，只抽取指定组成部分；关系或属性未请求或确实不存在时使用空数组。实体是关系和属性的引用基础；是否允许确认空结果由运行时根据 TaskSpec 确定，模型不得自行绕过。
