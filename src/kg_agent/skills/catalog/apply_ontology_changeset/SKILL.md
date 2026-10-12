---
name: apply_ontology_changeset
version: 1.0.0
description: 把用户修改应用为新的候选本体 Artifact。
owner_agent: graph_management_agent
supported_subtask_types: [ontology_changeset_apply]
input_artifact_types: [CandidateOntology, OntologyChangeSet]
output_artifact_types: [CandidateOntology]
parameters_schema: {}
side_effect: false
timeout_seconds: 90
max_retries: 2
lifecycle: ACTIVE
---

# apply_ontology_changeset

## 执行说明

你负责将用户确认的 OntologyChangeSet 应用到 CandidateOntology，并输出新的 CandidateOntology 版本。

必须严格执行 ChangeSet 中的 ADD、UPDATE、DELETE、MERGE、SPLIT 操作，不应加入用户未要求的改动。输出是新的待审核候选本体，保留必要血缘语义；不得复用旧 Approval 或把结果直接标记为 PublishedOntology。
