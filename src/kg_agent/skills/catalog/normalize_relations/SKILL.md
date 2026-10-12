---
name: normalize_relations
version: 1.0.0
description: 根据已发布本体标准化关系类型。
owner_agent: knowledge_fusion_agent
supported_subtask_types: [relation_normalize]
input_artifact_types: [RelationDataset, PublishedOntology]
output_artifact_types: [RelationDataset]
parameters_schema: {}
side_effect: false
timeout_seconds: 180
max_retries: 2
lifecycle: ACTIVE
---

# normalize_relations

## 执行说明

你负责根据 PublishedOntology 规范化 RelationDataset 的关系类型和方向。

只使用本体定义的 relation_type_id，且关系端点必须保留为输入已有实体。若原关系无法映射到已发布本体，不要猜测替代关系；保留原事实时使用可解释的标准化结果，或让调用失败以便后续处理。不得添加缺少证据的新关系。
