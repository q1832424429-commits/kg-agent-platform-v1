---
name: apply_graph_changeset
version: 1.0.0
description: 把用户修改应用为新的候选图谱 Artifact。
owner_agent: graph_management_agent
supported_subtask_types: [graph_changeset_apply]
input_artifact_types: [CandidateGraph, GraphChangeSet]
output_artifact_types: [CandidateGraph]
parameters_schema: {}
side_effect: false
timeout_seconds: 90
max_retries: 2
lifecycle: ACTIVE
---

# apply_graph_changeset

## 执行说明

你负责将输入的 GraphChangeSet 原子应用到 CandidateGraph，形成可供用户预览、编辑和批准的新版本。

当前实现是正式确定性修改器，本提示词用于记录其边界：操作必须完全遵从 ChangeSet，失败时不得产生半成品；修改结果必须成为新的 CandidateGraph 并进入质量评估和人工审核，不能直接发布。
