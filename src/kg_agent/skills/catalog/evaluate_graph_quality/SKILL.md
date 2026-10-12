---
name: evaluate_graph_quality
version: 1.0.0
description: 评估候选图谱及冲突处理质量。
owner_agent: quality_evaluation_agent
supported_subtask_types: [graph_quality_evaluate]
input_artifact_types: [CandidateGraph, ConflictReport]
output_artifact_types: [GraphQualityReport]
parameters_schema: {}
side_effect: false
timeout_seconds: 90
max_retries: 2
lifecycle: ACTIVE
---

# evaluate_graph_quality

## 执行说明

你负责评估 CandidateGraph 与 ConflictReport，并输出 GraphQualityReport。

检查实体和关系完整性、关系端点有效性、类型一致性、重复知识和未处理冲突。scores 必须在 0 到 1 范围内；issues 必须可定位到 affected_ids。passed 只表示当前质量评估结论，不表示用户已审核，也不能触发自动发布。
