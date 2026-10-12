---
name: fuse_ontology
version: 1.0.0
description: 融合候选本体与平台已发布本体。
owner_agent: knowledge_fusion_agent
supported_subtask_types: [ontology_fuse]
input_artifact_types: [CandidateOntology, PublishedOntology]
output_artifact_types: [FusedCandidateOntology]
parameters_schema: {}
side_effect: false
timeout_seconds: 180
max_retries: 2
lifecycle: ACTIVE
---

# fuse_ontology

## 执行说明

你负责将 CandidateOntology 与已有 PublishedOntology 融合为 FusedCandidateOntology。

已发布本体是稳定基线，不能擅自改写其既有语义。只在候选本体存在明确增量时添加或对齐；发现冲突应记录在 alignment_notes，留给用户审核。输出必须仍是待审核候选版本，不能复用 PublishedOntology 的发布身份或 Approval。
