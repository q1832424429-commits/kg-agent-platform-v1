---
name: analyze_conflicts
version: 1.0.0
description: 汇总实体、关系和证据并执行冲突分析，不负责构建候选图谱。
owner_agent: knowledge_fusion_agent
supported_subtask_types: [conflict_analyze]
input_artifact_types: [EntityDataset, RelationDataset, EvidenceDataset]
output_artifact_types: [ConflictReport]
parameters_schema: {}
side_effect: false
timeout_seconds: 180
max_retries: 2
lifecycle: ACTIVE
---

# analyze_conflicts

## 执行说明

你负责分析实体、关系和证据数据中的可验证冲突并输出 ConflictReport。

只报告输入数据中可以明确定位的重复标识、互斥值、类型不一致、悬空引用或证据矛盾。每个 conflict_id 稳定且唯一，description 应说明冲突原因和受影响 ID。不要创建 CandidateGraph，不要擅自修复或删除知识事实。
