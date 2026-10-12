---
name: publish_graph
version: 1.0.0
description: 发布已经通过审核的固定版本图谱。
owner_agent: graph_management_agent
supported_subtask_types: [graph_publish]
input_artifact_types: [CandidateGraph, GraphApproval]
output_artifact_types: [PublishedGraph]
parameters_schema: {}
side_effect: true
timeout_seconds: 90
max_retries: 2
lifecycle: ACTIVE
---

# publish_graph

## 执行说明

你负责把已批准的 CandidateGraph 发布为 PublishedGraph。

当前实现是正式确定性发布器，本提示词用于记录其边界：只能原样保留绑定 GraphApproval 的候选图谱事实，不能重写、补造或删除实体、关系、属性和证据。发布前必须由 PublishGuard 校验，发布后图谱仍不可变。
