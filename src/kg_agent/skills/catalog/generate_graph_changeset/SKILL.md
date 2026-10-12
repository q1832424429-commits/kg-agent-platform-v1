---
name: generate_graph_changeset
version: 1.0.0
description: 比对新增知识与候选图谱基线，生成用于构建增量候选图谱的图谱修改草案。
owner_agent: graph_management_agent
supported_subtask_types: [graph_changeset_generate]
input_artifact_types: [CandidateGraph, PublishedOntology, EntityDataset, RelationDataset, PropertyDataset, EvidenceDataset]
output_artifact_types: [GraphChangeSet]
parameters_schema: {}
requires_dedicated_subtask: true
side_effect: false
timeout_seconds: 180
max_retries: 2
lifecycle: ACTIVE
---

# generate_graph_changeset

## 执行目标

你负责把本轮新文档产生的实体、关系、属性和证据，与指定的既有 `CandidateGraph` 进行
增量比对，生成供下游 `graph_changeset_apply` 构建新候选版本的 `GraphChangeSet`。你不应用
修改、不覆盖候选图谱、不发布图谱，也不把建议视为已发布事实。

## 输入语义

- `CandidateGraph`：唯一的图谱基线；`GraphChangeSet.target_artifact_id` 必须严格复制它的
  `artifact_id`，`target_checksum` 必须严格复制它的 `checksum`。
- `PublishedOntology`：本轮唯一可用的实体类别、关系类型和属性语义约束；不得发明本体外的
  `class_id`、`relation_type_id` 或属性语义。
- `EntityDataset`、`RelationDataset`、`PropertyDataset`、`EvidenceDataset`：仅表示本轮新文档
  提供的新增候选知识及其证据，不能默认覆盖旧图谱事实。

## 变更生成规则

1. 只有证据、标识、名称及上下文足以支持时，才把新实体映射到基线中的既有实体。
2. 基线不存在且证据充分的新事实使用 `ADD`；已有事实的可验证属性差异使用 `UPDATE`。
3. `MERGE`、`SPLIT`、`DELETE` 是高风险操作。证据不足、存在冲突或无法明确映射时，不得
   生成这些操作；保守地不输出该操作，等待用户在审核界面手动处理。
4. 新关系的 `source_entity_id` 和 `target_entity_id` 必须指向应用修改后存在的实体：新增实体
   先生成 `ADD ENTITY`，再生成引用它的 `ADD RELATION`。
5. 每个操作必须使用唯一且稳定的 `operation_id`；`target_kind` 只能是 `ENTITY`、`RELATION`
   或 `PROPERTY`。
6. `value` 必须符合现有 `apply_graph_changeset` 的应用语义：
   - `ADD`：提供完整目标记录，且记录 ID 与 `target_id` 一致；
   - `UPDATE`：只提供要覆盖的业务字段，不能修改记录 ID；
   - `MERGE ENTITY`：提供 `source_entity_ids`；
   - `SPLIT ENTITY`：提供不少于两个完整替代实体的 `entities`；
   - `DELETE`：不需要补造 value。
7. 在不影响上述应用字段的前提下，可在 `value` 内记录 `source_evidence_ids`、
   `source_artifact_ids`、`confidence`、`reason`，便于审核页面解释建议来源。

## 停止边界

- 只输出一个 `GraphChangeSet`，即使没有安全可执行的建议也必须输出空 `operations`。草案必须
  作为下游新 CandidateGraph 的可追溯来源保留。
- 不创建 `CandidateGraph`、`GraphApproval`、`PublishedGraph` 或任何审核结论。
- 不因为用户请求“暂不发布”而省略增量草案；用户的可视化编辑、批准和发布发生在下游新
  CandidateGraph 形成之后。
