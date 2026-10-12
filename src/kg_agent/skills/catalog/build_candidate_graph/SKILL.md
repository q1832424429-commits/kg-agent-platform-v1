---
name: build_candidate_graph
version: 1.0.0
description: 根据已融合的实体、关系与冲突报告构建候选知识图谱。
owner_agent: graph_management_agent
supported_subtask_types: [graph_build]
input_artifact_types: [EntityDataset, RelationDataset, ConflictReport]
output_artifact_types: [CandidateGraph]
parameters_schema: {}
side_effect: false
timeout_seconds: 180
max_retries: 2
lifecycle: ACTIVE
---

# build_candidate_graph

## 执行说明

你负责从已校验的实体、关系和冲突报告组装 CandidateGraph。

当前实现是正式确定性组装器，本提示词用于记录其业务边界：只汇聚已有事实，校验关系端点和属性归属，不补造、不改写、不删除上游知识。CandidateGraph 是待审核结果，不能声明已经发布。
