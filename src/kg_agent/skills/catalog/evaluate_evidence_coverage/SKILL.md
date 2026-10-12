---
name: evaluate_evidence_coverage
version: 1.0.0
description: 评估图谱证据覆盖率。
owner_agent: quality_evaluation_agent
supported_subtask_types: [evidence_quality_evaluate]
input_artifact_types: [CandidateGraph, EvidenceDataset]
output_artifact_types: [GraphQualityReport]
parameters_schema: {}
side_effect: false
timeout_seconds: 90
max_retries: 2
lifecycle: ACTIVE
---

# evaluate_evidence_coverage

## 执行说明

你负责评估 CandidateGraph 的证据覆盖情况，并输出 GraphQualityReport。

检查实体、关系和属性是否能够关联到 EvidenceDataset 中的证据；区分无证据、证据定位缺失和置信度不足。issues 必须列出受影响知识 ID，recommendations 给出可执行改进方向。不要补造证据或修改图谱事实。
