---
name: extract_ontology_candidates
version: 1.0.0
description: 从语义文档归纳候选本体。
owner_agent: knowledge_extraction_agent
supported_subtask_types: [ontology_extract]
input_artifact_types: [SemanticDocument]
output_artifact_types: [CandidateOntology]
parameters_schema: {}
side_effect: false
timeout_seconds: 180
max_retries: 2
lifecycle: ACTIVE
---

# extract_ontology_candidates

## 执行说明

你负责从 SemanticDocument 中归纳待人工确认的 CandidateOntology。

本体只描述文档已支持的概念、关系类型和属性类型。类、关系和属性 ID 要稳定、简洁、可复用；关系必须给出合理的 domain_class_id 与 range_class_id。不要把文档中的每个实例都提升为类，不要凭常识补充文档没有支持的行业概念。source_artifact_ids 必须引用输入语义文档。

返回的 CandidateOntology.content 必须显式包含 classes、relations、properties 和 source_artifact_ids 四个数组，即使某类结果为空也不得省略字段。classes 至少包含一个由文档支持的本体类；不得只在 content_summary 中声称已抽取数量。每个 relation 的定义域和值域、每个 property 的所属类都必须引用 classes 中已声明的 class_id。
