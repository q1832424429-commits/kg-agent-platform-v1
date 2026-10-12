---
name: align_ontology
version: 1.0.0
description: 对齐候选本体与外部本体。
owner_agent: knowledge_fusion_agent
supported_subtask_types: [ontology_align]
input_artifact_types: [CandidateOntology, ExternalOntology]
output_artifact_types: [FusedCandidateOntology]
parameters_schema: {}
side_effect: false
timeout_seconds: 180
max_retries: 2
lifecycle: ACTIVE
---

# align_ontology

## 执行说明

你负责把 CandidateOntology 与用户上传的 ExternalOntology 对齐并输出 FusedCandidateOntology。

识别同义类、关系和属性时应保留可解释的对齐说明。外部本体与候选本体存在冲突时，不得静默删除任一方信息；保守保留并在 alignment_notes 中说明。输出仍是候选本体，绝不声称已经审核或发布。
