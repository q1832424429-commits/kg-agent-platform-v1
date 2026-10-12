---
name: publish_ontology
version: 1.0.0
description: 发布经 PublishGuard 验证的候选或融合本体。
owner_agent: graph_management_agent
supported_subtask_types: [ontology_publish]
input_artifact_types: [CandidateOntology, OntologyApproval]
output_artifact_types: [PublishedOntology]
parameters_schema: {}
side_effect: true
timeout_seconds: 90
max_retries: 2
lifecycle: ACTIVE
---

# publish_ontology

## 执行说明

你负责生成 PublishedOntology，但仅在调用前的 PublishGuard 已确认当前 CandidateOntology 或 FusedCandidateOntology 与 OntologyApproval 绑定时执行。两类候选本体只能选择一个，不能同时发布。

PublishedOntology 的 classes、relations、properties 和 source_artifact_ids 必须原样继承已批准候选本体；ontology_id、ontology_version 与 approval_artifact_id 必须完整且可追溯。不得用模型重写、补充或删除用户已经批准的本体事实。
