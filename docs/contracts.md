# V1 核心数据契约

## 1. 文档说明

本文使用接近 Pydantic v2 的形式固化核心领域契约。代码块用于表达 Schema 设计，不是当前阶段的业务代码。实际实现必须保持语义一致；需要修改公共字段时，应先更新本文和相关 ADR。

标记为 `TODO` 的内容尚未完全确认，不得在实现时自行扩展为新的业务规则。

## 2. 基础类型与枚举

```python
from datetime import datetime
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field


class TaskIntent(StrEnum):
    """用户对总任务的顶层操作意图。"""

    START = "START"
    CONTINUE = "CONTINUE"
    MODIFY = "MODIFY"


class SubTaskStatus(StrEnum):
    """AgentSubTask 在协作过程中的状态。"""

    PENDING = "PENDING"
    BLOCKED = "BLOCKED"
    READY = "READY"
    ASSIGNED = "ASSIGNED"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    SUPERSEDED = "SUPERSEDED"


class AgentRunStatus(StrEnum):
    """专业 Agent 一次运行的业务结果。"""

    SUCCESS = "SUCCESS"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    NEEDS_USER_INPUT = "NEEDS_USER_INPUT"
    RETRYABLE_FAILED = "RETRYABLE_FAILED"
    REWORK_REQUIRED = "REWORK_REQUIRED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AssignmentMode(StrEnum):
    """子任务分配方式。V1 默认由总控 Agent 明确指定负责角色。"""

    DIRECT = "DIRECT"


class ReviewTargetType(StrEnum):
    """人工审核目标。"""

    ONTOLOGY = "ONTOLOGY"
    GRAPH = "GRAPH"
```

第二阶段冻结以下状态：

```python
class MasterTaskStatus(StrEnum):
    ACTIVE = "ACTIVE"
    WAITING_USER = "WAITING_USER"
    WAITING_HUMAN = "WAITING_HUMAN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class AssignmentStatus(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"


class ArtifactStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    INVALIDATED = "INVALIDATED"
    SUPERSEDED = "SUPERSEDED"


class HumanReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CHANGES_REQUESTED = "CHANGES_REQUESTED"
    CANCELLED = "CANCELLED"
```

## 3. TaskSpec

```python
class OntologyStrategy(BaseModel):
    """描述本任务如何获得可用于实例抽取的本体。"""

    mode: Literal[
        "REUSE_PUBLISHED",
        "IMPORT_EXTERNAL_JSON",
        "BUILD_FROM_DOCUMENTS",
    ]
    selected_ontology_id: UUID | None = None
    selected_ontology_version: str | None = None
    external_ontology_artifact_id: UUID | None = None


class QualityRequirements(BaseModel):
    """用户明确提出或平台默认采用的质量要求。"""

    requirements: list[str] = Field(default_factory=list)
    thresholds: dict[str, float] = Field(default_factory=dict)


class TaskSpec(BaseModel):
    """总控 Agent 对用户当前目标的结构化、版本化描述。"""

    task_id: UUID
    revision: int = Field(ge=1)
    task_intent: TaskIntent
    business_goal: str
    domain: str = "manufacturing"
    source_artifact_ids: list[UUID] = Field(default_factory=list)
    target_output_types: list[str] = Field(default_factory=list)
    ontology_strategy: OntologyStrategy
    extraction_scope: dict[str, Any] = Field(default_factory=dict)
    quality_requirements: QualityRequirements = Field(default_factory=QualityRequirements)
    reuse_policy: dict[str, Any] = Field(default_factory=dict)
    constraints: list[str] = Field(default_factory=list)
    created_by: Literal["controller_agent"] = "controller_agent"
    created_at: datetime
```

约束：

- TaskSpec 自动生成，不要求独立人工确认。
- `MODIFY` 必须创建新 revision。
- `CONTINUE` 可以逐字段原样沿用当前不可变 revision；目标或字段发生变化时必须创建下一 revision，不得覆盖历史 revision。
- 实例抽取所使用的本体必须最终解析为 PublishedOntology ID 和版本。

## 4. MasterTask

```python
class MasterTask(BaseModel):
    """用户总目标及其生命周期根对象。"""

    task_id: UUID
    conversation_id: UUID
    workspace_id: str = "default-workspace"
    user_id: str = "default-user"
    title: str
    current_task_spec_revision: int
    current_decomposition_revision: int
    status: MasterTaskStatus
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None = None
```

约束：一个 MasterTask 对应一个逻辑 SharedTaskContext。任务修改不能删除历史 TaskSpec、子任务、Assignment 或 Artifact。

### 4.1 Conversation 与会话管理读模型

```python
class Conversation(BaseModel):
    """用户可见的聊天容器；不取代任务级业务状态。"""

    conversation_id: UUID
    title: str
    created_at: datetime
    updated_at: datetime


class ConversationDeletionResult(BaseModel):
    """会话删除后的清理统计及跨存储告警。"""

    conversation_id: UUID
    deleted_task_count: int
    deleted_artifact_count: int
    retained_published_asset_count: int
    deleted_memory_count: int
    cleanup_warnings: list[str]
```

一个 Conversation 可以包含多个 MasterTask；消息按 conversation_id 保存，尚未创建任务的
澄清消息允许 `task_id=None`。已发布资产脱离被删除的来源任务后，物理索引的 task_id 可为空，
但不可变 ArtifactRef 内容中的原始生产任务 ID 与发布内容、checksum 保留，以便追踪版本来源。
删除操作以 V1 固定 `default-user` 所属任务为边界，不能跨用户清理。

## 5. AgentSubTask

```python
class RetryPolicy(BaseModel):
    """子任务发生可重试错误时的重试限制。"""

    max_attempts: int = Field(default=3, ge=1)
    backoff_seconds: int = Field(default=1, ge=0)


class AgentSubTask(BaseModel):
    """由总控 Agent 发布、交给一个专业 Agent 负责的逻辑工作单元。"""

    subtask_id: UUID
    master_task_id: UUID
    decomposition_revision: int = Field(ge=1)
    name: str
    description: str
    goal: str
    subtask_type: str
    assigned_agent_type: str
    assignment_mode: AssignmentMode = AssignmentMode.DIRECT
    dependency_subtask_ids: list[UUID] = Field(default_factory=list)
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    expected_output_types: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    quality_requirements: QualityRequirements = Field(default_factory=QualityRequirements)
    human_review_required: bool = False
    priority: int = 0
    status: SubTaskStatus
    retry_policy: RetryPolicy = Field(default_factory=RetryPolicy)
    created_by: Literal["controller_agent"] = "controller_agent"
    created_at: datetime
```

约束：

- `assigned_agent_type` 是 Agent Registry 中的逻辑角色，不是 Worker 实例 ID。
- `subtask_type` 保持可扩展字符串，但发布前必须通过 Registry 能力校验。
- 专业 Agent 不能直接创建该对象，只能提出 ProposedSubTask。
- 被新 decomposition revision 替代且未开始的子任务可以标为 `SUPERSEDED`，已完成子任务不得删除。
- START 的同一 decomposition revision 可以一次包含当前最终目标所需的全部 AgentSubTask。
- `input_artifact_ids` 只填写计划生成时已经存在的 Artifact；未来输入用 `dependency_subtask_ids` 声明来源，依赖成功后由 Worker 根据祖先 AgentRunResult 动态绑定真实 ID。

## 6. AgentAssignment

```python
class AgentAssignment(BaseModel):
    """一个 AgentSubTask 的具体执行尝试和租约记录。"""

    assignment_id: UUID
    subtask_id: UUID
    assigned_agent_type: str
    claimed_by: str | None = None
    status: AssignmentStatus
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    fencing_token: int = Field(default=0, ge=0)
    attempt: int = Field(default=1, ge=1)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    created_at: datetime
```

约束：

- 同一 Assignment 同一时刻只能有一个有效认领者。
- 只有 `assigned_agent_type` 匹配的 Agent 实例可以认领。
- 所有完成和 Artifact 回写必须校验当前 fencing token。
- 同一 MasterTask 可以存在多个并行、互不冲突的有效 Assignment。

## 7. ArtifactRef

```python
class ArtifactRef(BaseModel):
    """SharedContext 中对正式 Artifact 的轻量、可追溯引用。"""

    artifact_id: UUID
    artifact_type: str
    schema_version: str
    task_id: UUID
    task_spec_revision: int
    producer_agent: str
    producer_skill: str | None = None
    producer_run_id: UUID
    parent_artifact_ids: list[UUID] = Field(default_factory=list)
    ontology_id: UUID | None = None
    ontology_version: str | None = None
    content_uri: str
    content_summary: str
    checksum: str
    status: ArtifactStatus
    created_at: datetime
```

约束：

- `content_uri` 指向 MinIO 或受控对象存储位置，不允许任意本地路径。
- Artifact 内容默认不可变。
- Artifact 的生产者、输入血缘、TaskSpec revision 和本体版本必须可追溯。
- Artifact 复用必须经过 ArtifactReuseService。

## 8. SharedContext

```python
class ConversationContextRef(BaseModel):
    """当前任务关联的会话引用和受控摘要。"""

    conversation_id: UUID
    latest_message_id: UUID | None = None
    summary: str = ""


class MemoryRef(BaseModel):
    """SharedContext 中的结构化记忆引用。"""

    memory_id: UUID
    memory_type: Literal["SHORT_TERM", "LONG_TERM", "EXPERIENCE"]
    summary: str
    source_refs: list[str] = Field(default_factory=list)


class LongTermMemoryRecord(BaseModel):
    """Mem0 Cloud 返回的一条长期记忆；不直接作为 SharedContext 正文保存。"""

    provider_memory_id: str
    memory: str
    score: float | None = None
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)
    categories: list[str] = Field(default_factory=list)


class MemoryWriteReceipt(BaseModel):
    """长期记忆供应商接受写入请求后的公开回执。"""

    event_id: str | None = None
    status: str
    message: str = ""


class MemorySearchRequest(BaseModel):
    """长期记忆语义检索参数。"""

    query: str
    top_k: int = Field(default=8, ge=1, le=100)
    threshold: float | None = Field(default=None, ge=0, le=1)
    rerank: bool = False


class MemoryCreateRequest(BaseModel):
    """用户显式授权的长期记忆写入请求。"""

    text: str
    infer: bool = True
    authorized: bool = False
    source_refs: list[str] = Field(default_factory=list)


class CompletedTaskExperience(BaseModel):
    """MasterTask 成功后由确定性服务聚合的一条完整公开经验。"""

    experience_key: str
    task_id: UUID
    task_spec_revision: int = Field(ge=1)
    decomposition_revision: int = Field(ge=1)
    summary: str
    source_refs: list[str] = Field(default_factory=list)


class MemoryConsolidationResult(BaseModel):
    """完整经验经过语义去重后的处理结果。"""

    action: Literal["CREATED", "MERGED", "SKIPPED_DUPLICATE", "DISABLED"]
    receipt: MemoryWriteReceipt
    similarity: float | None = Field(default=None, ge=0, le=1)
    matched_provider_memory_id: str | None = None


class SharedTaskContext(BaseModel):
    """一个 MasterTask 内由全部 Agent 共享的逻辑上下文快照。"""

    task_id: UUID
    version: int = Field(ge=1)
    conversation: ConversationContextRef
    master_task: MasterTask
    current_task_spec: TaskSpec
    task_spec_revision_refs: list[int] = Field(default_factory=list)
    subtask_ids: list[UUID] = Field(default_factory=list)
    assignment_ids: list[UUID] = Field(default_factory=list)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    agent_run_result_ids: list[UUID] = Field(default_factory=list)
    human_review_request_ids: list[UUID] = Field(default_factory=list)
    short_term_memories: list[MemoryRef] = Field(default_factory=list)
    long_term_memory_refs: list[MemoryRef] = Field(default_factory=list)
    execution_trace_ids: list[UUID] = Field(default_factory=list)
    updated_at: datetime
```

约束：

- SharedTaskContext 不能由 Agent 整体覆盖。
- `version` 用于乐观并发控制。
- 完整消息、文档和大型 Artifact 内容在各自存储中维护。
- `short_term_memories` 中以 `conversation-role:user/assistant` 标记的引用组成公开对话投影；
  当前 Context View 最多暴露最近五轮十条，对话正文以 PostgreSQL 会话消息表为准。
- 长期记忆正文和向量检索由 Mem0 Cloud 维护；这里只保存 MemoryRef。
- 当前 Context View 只暴露本轮相关的最多三条 `LONG_TERM` 引用；`EXPERIENCE` 不直接占用
  这三条额度，须经 Mem0 写入和后续语义召回后才能作为长期记忆进入 Agent。
- 自动经验只在 MasterTask 成功后按当前 TaskSpec/decomposition revision 聚合一条；单个
  AgentRunResult 和候选计划发布不得直接写入长期经验。`experience_key` 同时作为 Mem0 元数据
  和 PostgreSQL 写入幂等依据。
- 完整经验相似度 `>=0.92` 时跳过，`>=0.80 且 <0.92` 时合并后写入，`<0.80` 时新增；
  阈值判断由 MemoryService 确定执行，不交给模型。
- Agent 实际获得的是 ContextService 生成的角色化 Context View，不是整个对象。

## 8.1 TaskEvent

```python
class TaskEvent(BaseModel):
    """记录已经提交的确定性任务状态变化。"""

    event_id: UUID
    sequence: int = Field(ge=1)
    task_id: UUID
    subtask_id: UUID | None = None
    assignment_id: UUID | None = None
    event_type: TaskEventType
    actor_type: Literal["USER", "AGENT", "SERVICE", "WORKER"]
    actor_id: str
    reason_code: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at: datetime
```

约束：TaskEvent 只能追加；不保存 API Key、完整文档或模型隐藏思维过程。业务状态、事件和对应 SharedContext 新快照必须在同一事务提交。

## 8.2 Context View

第二阶段提供 `ControllerContextView` 和 `ProfessionalAgentContextView`。总控视图包含任务摘要、全部子任务状态、Assignment 摘要、Artifact 摘要和审核引用；专业 Agent 视图只包含当前 Assignment、当前 AgentSubTask、计划时显式输入及依赖成功后动态绑定的输入 Artifact、目标与验收条件。任何 Context View 均不得包含 Artifact `content_uri`、无关任务数据或隐藏思维过程。

## 9. ProposedSubTask 与 AgentRunResult

```python
class ProposedSubTask(BaseModel):
    """专业 Agent 对后续工作的建议；必须由总控 Agent 审核后才能发布。"""

    name: str
    goal: str
    reason: str
    proposed_agent_type: str | None = None
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    expected_output_types: list[str] = Field(default_factory=list)


class QualitySignal(BaseModel):
    """Agent 对本次结果给出的结构化质量信号。"""

    name: str
    value: float | str | bool
    evidence_refs: list[str] = Field(default_factory=list)


class SkillArtifactBinding(BaseModel):
    """一次 Skill 调用与持久化 Artifact 之间的确定性映射。"""

    invocation_id: UUID
    skill_name: str
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_types: list[str] = Field(default_factory=list)


class AgentRunResult(BaseModel):
    """专业 Agent 完成一次 Assignment 后写入的标准结果。"""

    task_id: UUID
    subtask_id: UUID
    assignment_id: UUID
    agent_name: str
    run_id: UUID
    status: AgentRunStatus
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    output_artifact_ids: list[UUID] = Field(default_factory=list)
    skill_artifact_bindings: list[SkillArtifactBinding] = Field(default_factory=list)
    skills_used: list[str] = Field(default_factory=list)
    quality_signals: list[QualitySignal] = Field(default_factory=list)
    blocking_issues: list[str] = Field(default_factory=list)
    recommended_next_agent: str | None = None
    proposed_subtasks: list[ProposedSubTask] = Field(default_factory=list)
    requires_user_input: bool = False
    user_question: str | None = None
    retryable: bool = False
    metrics: dict[str, int | float | str] = Field(default_factory=dict)
    execution_trace_id: UUID
    memory_entries: list[MemoryRef] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime
```

约束：

- AgentRunResult 不能直接解锁下游或创建正式 AgentSubTask。
- 状态与字段必须一致，例如 `NEEDS_USER_INPUT` 必须提供 `user_question`。
- `SUCCESS` 必须包含满足 expected_output_types 的有效 Artifact。
- `skill_artifact_bindings` 必须完整覆盖 `output_artifact_ids`，编号和类型一一对应；编号由
  ArtifactService 持久化时生成，不能由模型输出或提前编造。
- 下游子任务沿依赖祖先的 AgentRunResult 解析真实 Artifact ID，再从对象存储读取内容；
  读取后必须校验 checksum，并按 Artifact Registry 的内容模型再次执行 Pydantic 校验。
- Skill 原始模型回答只用于受控排障，不是 Artifact，也不能作为下游正式输入。

## 10. HumanReviewRequest

```python
class ReviewOption(StrEnum):
    """Human Review API 支持的用户操作。"""

    APPROVE = "APPROVE"
    REJECT = "REJECT"
    SUBMIT_CHANGES = "SUBMIT_CHANGES"


class HumanReviewRequest(BaseModel):
    """要求用户审核候选本体或候选图谱的持久化门禁。"""

    review_id: UUID
    task_id: UUID
    subtask_id: UUID | None = None
    target_type: ReviewTargetType
    target_artifact_id: UUID
    target_artifact_checksum: str
    quality_report_artifact_ids: list[UUID] = Field(default_factory=list)
    conflict_report_artifact_ids: list[UUID] = Field(default_factory=list)
    evidence_artifact_ids: list[UUID] = Field(default_factory=list)
    allowed_actions: list[ReviewOption]
    status: HumanReviewStatus
    requested_at: datetime
    resolved_at: datetime | None = None
    decision_artifact_id: UUID | None = None
    reviewer_feedback: str | None = None
```

约束：

- Approval 必须绑定被审核 Artifact 的 ID 和 checksum。
- 本体审核可以不附带质量报告；候选本体直接由用户预览、修改或批准。
- 候选图谱审核必须至少附带一份指向当前候选版本的 GraphQualityReport。
- 审核后目标 Artifact 发生变化时，旧 Approval 自动失效。
- 本体审核通过产生 OntologyApproval；图谱审核通过产生 GraphApproval。
- 用户批准本体或图谱时可填写发布名称；名称写入对应 ApprovalContent，发布 Skill 原样写入
  PublishedOntologyContent 或 PublishedGraphContent，并投影到 ArtifactRef，供资产库管理和展示。
- 修改产生 OntologyChangeSet 或 GraphChangeSet；本体修改形成新候选后再次人工确认，图谱修改根据影响重新评估。

## 11. ExecutionTrace

```python
class TraceEvent(BaseModel):
    """执行轨迹中的单个结构化事件。"""

    event_id: UUID
    event_type: str
    occurred_at: datetime
    actor_type: Literal["USER", "AGENT", "SKILL", "SERVICE", "MODEL"]
    actor_name: str
    summary: str
    input_refs: list[str] = Field(default_factory=list)
    output_refs: list[str] = Field(default_factory=list)
    metrics: dict[str, int | float | str] = Field(default_factory=dict)
    error_code: str | None = None


class ExecutionTrace(BaseModel):
    """关联一次端到端任务或 Agent Run 的可审计轨迹。"""

    trace_id: UUID
    task_id: UUID
    subtask_id: UUID | None = None
    assignment_id: UUID | None = None
    agent_run_id: UUID | None = None
    parent_trace_id: UUID | None = None
    prompt_version: str | None = None
    model_name: str | None = None
    events: list[TraceEvent] = Field(default_factory=list)
    started_at: datetime
    finished_at: datetime | None = None
    status: str
```

约束：ExecutionTrace 保存可观察事件和指标，不保存模型隐藏思维过程，不记录 API Key 或完整敏感文档。

## 12. 总控 Agent 结构化输出

总控模型首先返回内部轻量草案；它不是 SharedContext 的公开持久化契约：

```python
class AgentSubTaskDraft(BaseModel):
    """模型负责的 Agent 级任务语义。"""

    node_id: str
    name: str
    description: str = ""
    goal: str
    subtask_type: str
    assigned_agent_type: str
    dependency_node_ids: list[str] = Field(default_factory=list)
    input_artifact_ids: list[UUID] = Field(default_factory=list)
    expected_output_types: list[str] = Field(default_factory=list)
    completion_criteria: list[str] = Field(default_factory=list)
    human_review_required: bool = False


class TaskPlanDraft(BaseModel):
    """模型输出的意图、TaskSpec 语义字段和计划内依赖。"""

    task_intent: TaskIntent
    task_spec: TaskSpecDraft
    subtasks: list[AgentSubTaskDraft]
    reused_artifact_ids: list[UUID] = Field(default_factory=list)
    requires_clarification: bool = False
    clarification_question: str | None = None
```

`TaskPlanDraft` 不包含持久化 UUID、revision、运行状态和时间。确定性计划编译器将节点别名
解析为依赖，补齐运行时事实和 Skill 原子输出后，生成下列既有公开契约：

```python
class TaskDecompositionResult(BaseModel):
    """总控 Agent 完成意图、任务规格和子任务拆解后的候选输出。"""

    task_intent: TaskIntent
    task_spec: TaskSpec
    subtasks: list[AgentSubTask]
    reused_artifact_ids: list[UUID] = Field(default_factory=list)
    requires_clarification: bool = False
    clarification_question: str | None = None
```

编译后的公开输出必须依次经过：

1. Pydantic Schema 校验。
2. Agent Registry 能力校验。
3. Artifact 输入输出兼容性校验。
4. 子任务依赖和环路校验。
5. 本体/图谱强制人工审核校验。
6. Artifact 复用确定性校验。
7. 权限和预算校验。

只有全部通过后才能发布到 SharedContext。

## 13. Agent 与 Skill 注册契约

```python
class AgentRegistration(BaseModel):
    """专业 Agent 向总控 Agent 暴露的能力目录。"""

    agent_type: str
    name: str
    version: str
    description: str
    accepted_subtask_types: list[str]
    accepted_input_types: list[str]
    produced_output_types: list[str]
    allowed_skills: list[str]
    can_publish_subtasks: bool = False
    can_publish_ontology: bool = False
    can_publish_graph: bool = False
    lifecycle: str


class SkillRegistration(BaseModel):
    """Skill Registry 保存的能力和权限声明。"""

    name: str
    version: str
    description: str
    owner_agent: str | None
    supported_subtask_types: list[str]
    allowed_agents: list[str]
    input_artifact_types: list[str]
    output_artifact_types: list[str]
    parameters_schema: dict[str, Any]
    side_effect: bool
    timeout_seconds: int
    max_retries: int
    lifecycle: str
```

Registry 启动校验必须拒绝重复名称/版本、未知 Owner、非法权限和无法匹配的 Artifact 类型。
`supported_subtask_types` 必须属于 Owner Agent 的 `accepted_subtask_types`；运行时可据此查询
某个 Agent 级子任务可能调用的 Skill，但 Registry 不替专业 Agent 选择实际 Skill。

每个注册项来自 `src/kg_agent/skills/catalog/<skill_name>/SKILL.md` 的 YAML front matter，其目录名
必须与 `name` 一致；正文是当前 Skill 的执行说明。`implementation.py` 仅对已经实现专用算法的
Skill 可选存在；缺失时使用通用 LLM 执行器。`allowed_agents` 只来自 `configs/skills/v1.yaml` 的
全局权限清单，不在单 Skill 目录重复配置。目录约定与权限映射均由服务端 Registry 校验。

Skill 不暴露执行模式，也不允许任务请求选择执行器。Registry 每个 Skill 只绑定一个当前执行器：
目录含内置 `implementation.py` 时加载专用代码；自定义 Skill 含 Python 资源时进入隔离 Runner；
否则由专业 Agent 加载 `SKILL.md` 正文作为提示词并调用模型。三条路径都必须遵守同一 Artifact Schema、
权限、审核和发布门禁，且不进入 TaskSpec、AgentSubTask 或 SharedContext。

## 14. Golden Evaluation Record

```python
class GoldenEvaluationRecord(BaseModel):
    """用于评测总控 Agent 路由和拆解准确率的标注案例。"""

    case_id: str
    conversation_history: list[dict[str, Any]]
    shared_context: dict[str, Any]
    current_task: dict[str, Any] | None
    existing_artifacts: list[ArtifactRef]
    user_message: str
    expected_task_intent: TaskIntent
    expected_task_spec: dict[str, Any]
    expected_subtasks: list[dict[str, Any]]
    expected_assignments: list[dict[str, Any]]
    expected_reused_artifacts: list[UUID]
    expected_clarification: bool
    forbidden_actions: list[str]
```

## 15. 第三阶段 Artifact 写入与复用契约

~~~python
class ArtifactRecord(BaseModel):
    """组合 SharedContext 引用和对象存储内部元数据。"""

    ref: ArtifactRef
    object_key: str
    media_type: str
    size_bytes: int = Field(ge=0)


class ArtifactCreateCommand(BaseModel):
    """创建一次不可变 Artifact。"""

    artifact_id: UUID
    artifact_type: str
    schema_version: str
    task_id: UUID
    task_spec_revision: int
    producer_agent: str
    producer_skill: str | None
    producer_run_id: UUID
    parent_artifact_ids: list[UUID]
    ontology_id: UUID | None
    ontology_version: str | None
    publication_title: str | None
    content_summary: str
    media_type: str
    content: bytes | dict[str, Any]
    assignment_id: UUID | None
    worker_id: str | None
    fencing_token: int | None
    supersedes_artifact_id: UUID | None


class ArtifactReuseRequest(BaseModel):
    """声明一次复用必须满足的全部兼容条件。"""

    artifact_id: UUID
    expected_artifact_type: str
    expected_schema_version: str
    compatible_task_spec_revisions: set[int]
    expected_parent_checksums: set[str]
    expected_ontology_id: UUID | None
    expected_ontology_version: str | None


class ArtifactReuseDecision(BaseModel):
    artifact_id: UUID
    reusable: bool
    reason_codes: list[str]
~~~

约束：

- ArtifactCreateCommand 的 Assignment 凭证必须全部为空或全部提供。
- 专业 Agent 写入在上传前无锁预检，上传后按 Assignment → SubTask → MasterTask 顺序短暂加锁并二次检查 lease/fencing；MinIO 上传期间不持有数据库锁。
- 专业 Agent 生产结果时凭证不能为空，且必须匹配当前持有者、角色、状态和 fencing token。
- JSON 内容先由 ArtifactRegistry 校验并规范化序列化，之后计算 sha256:<hex>；读取时重新计算并比对。
- artifact_id 是幂等键；相同 ID 只能重放完全相同的内容和稳定元数据。
- `publication_title` 仅由本体/图谱发布路径写入，表示用户在批准审核时确认的资产名称；
  PublishedOntology、PublishedGraph 的任务内导入副本必须不填写该字段，不能作为新的资产目录项。
- 父 Artifact 必须存在且状态为 AVAILABLE。通常父子 Artifact 必须属于同一 MasterTask；唯一例外是“复用已发布资产”入口：系统可以把其他任务中的 `PublishedOntology` 完整复制为当前任务的 `PublishedOntology` 输入，或把 `PublishedGraph` 去除发布元数据后复制为当前任务的 `CandidateGraph` 基线。复制必须保留源 Artifact 为父血缘、校验源内容完全一致，并且不能原地修改已发布资产。
- supersedes_artifact_id 自动进入父血缘；新旧类型必须相同，旧 Artifact 仅改变状态，内容仍保留。
- 复用的任一条件不兼容都返回明确 reason code，不允许部分通过。

### 15.1 Artifact 内容 Schema

所有 JSON 内容体当前版本均为 1.0.0，代码事实来源是
src/kg_agent/domain/artifact_content_models.py，类型映射事实来源是
src/kg_agent/registries/artifact_registry.py。

| 内容契约 | Artifact 类型 |
|---|---|
| 二进制内容 | RawDocument |
| ExternalOntologyContent | ExternalOntology |
| StructuredDatasetContent | StructuredDataset |
| TextReportContent | TextReport |
| DocumentManifestContent | DocumentManifest |
| ParsedDocumentContent | ParsedDocument |
| SemanticDocumentContent | SemanticDocument |
| CandidateOntologyContent | CandidateOntology |
| FusedCandidateOntologyContent | FusedCandidateOntology |
| QualityReportContent | OntologyQualityReport、GraphQualityReport |
| ChangeSetContent | OntologyChangeSet、GraphChangeSet |
| ApprovalContent | OntologyApproval、GraphApproval |
| PublishedOntologyContent | PublishedOntology |
| EntityDatasetContent | EntityDataset |
| RelationDatasetContent | RelationDataset |
| PropertyDatasetContent | PropertyDataset |
| EvidenceDatasetContent | EvidenceDataset |
| ConflictReportContent | ConflictReport |
| CandidateGraphContent | CandidateGraph |
| PublishedGraphContent | PublishedGraph |

### 15.2 图谱预览投影契约

`GraphPreview` 是 `CandidateGraph` / `PublishedGraph` 的 API 读取投影，不是新的
Artifact 类型，不进入 SharedContext，不影响审核和发布判定。代码事实来源是
`src/kg_agent/domain/graph_preview_models.py`。

~~~python
class GraphPreviewWarning(BaseModel):
    """描述不阻断查看、但会影响绘图完整性的内容问题。"""

    code: str
    message: str
    affected_ids: list[str] = Field(default_factory=list)


class GraphPreviewNode(BaseModel):
    """一个实体节点及其公开属性和证据。"""

    id: str
    label: str
    class_id: str
    aliases: list[str] = Field(default_factory=list)
    properties: list[dict[str, Any]] = Field(default_factory=list)
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class GraphPreviewEdge(BaseModel):
    """一条关系边及其公开证据。"""

    id: str
    source: str
    target: str
    label: str
    relation_type_id: str
    evidence: list[dict[str, Any]] = Field(default_factory=list)


class GraphPreview(BaseModel):
    """供图谱工作台绘制的只读图谱投影。"""

    artifact_id: UUID
    artifact_type: Literal["CandidateGraph", "PublishedGraph"]
    task_id: UUID
    nodes: list[GraphPreviewNode]
    edges: list[GraphPreviewEdge]
    statistics: GraphPreviewStatistics
    warnings: list[GraphPreviewWarning] = Field(default_factory=list)
~~~

约束：预览服务只能读取图谱 Artifact；悬空关系不得绘制为边；告警不得修改或掩盖原始
内容。图谱工作台的节点坐标、缩放、筛选与布局仅存在浏览器视图。用户业务编辑仍必须转换
成 `GraphChangeSet` 的既有 ChangeOperation，不能直接写入 CandidateGraph 或 PublishedGraph。

对 `GraphChangeSet` 的图谱操作补充以下冻结语义：

- `ADD/UPDATE/DELETE` 的 `target_kind` 可为 `ENTITY`、`RELATION` 或 `PROPERTY`；新增记录的
  `target_id` 必须与对应 `entity_id`、`relation_id` 或 `property_id` 一致。
- `MERGE ENTITY` 的 `target_id` 是保留的主实体，`value.source_entity_ids` 是待合并实体；
  合并会确定性重定向关系和属性，并收集别名。
- `SPLIT ENTITY` 的 `value.entities` 至少包含两个完整实体；V1 默认把原关系和属性迁移给
  列表第一个实体，用户可以用后续关系操作精确调整。
- 服务端必须同时校验 `target_artifact_id` 和 `target_checksum`，并在内存副本上原子应用全量
  操作；任一操作非法时不得创建新的候选图谱。

### 15.3 PublishedGraph PostgreSQL 查询投影

`PublishedGraphContent` 仍是发布知识的不可变业务契约和审计事实。发布成功时，系统在保存
Artifact 元数据的同一 PostgreSQL 事务中维护以下查询投影：

| 表 | 主键 | 主要内容 |
|---|---|---|
| `published_graphs` | `graph_id` | Artifact、Task、版本、来源候选图和 Approval |
| `published_graph_nodes` | `graph_id + entity_id` | 实体名称、类型、别名和证据 |
| `published_graph_edges` | `graph_id + relation_id` | 起点、关系类型、终点和证据 |
| `published_graph_properties` | `graph_id + property_id` | 实体属性类型、JSON 值和证据 |
| `published_graph_evidence` | `graph_id + evidence_id` | 来源 Artifact、位置、引文和置信度 |

约束：边的起点和终点、属性所属实体必须以复合外键引用同一 `graph_id` 的节点；投影只能由
`publish_graph` 产生的 PublishedGraph 创建；投影失败必须回滚 Artifact 元数据。MinIO 中的
PublishedGraph JSON 不因查询投影而删除或改变。

## 16. 第三阶段 Skill 调用契约

~~~python
class SkillInvocation(BaseModel):
    invocation_id: UUID
    agent_type: str
    skill_name: str
    input_artifacts: list[ArtifactRef]
    parameters: dict[str, Any]


class SkillArtifactOutput(BaseModel):
    artifact_type: str
    schema_version: str
    content: dict[str, Any]
    content_summary: str
    media_type: str
    parent_artifact_ids: list[UUID]
    ontology_id: UUID | None
    ontology_version: str | None


class SkillRunResult(BaseModel):
    invocation_id: UUID
    skill_name: str
    status: AgentRunStatus
    outputs: list[SkillArtifactOutput]
    quality_signals: dict[str, float]
    error_code: str | None
    error_message: str | None
    retryable: bool
~~~

约束：

- SkillRegistry 必须先校验调用 Agent 位于 allowlist，并具备全部声明输入类型。
- Skill 只能返回注册声明中的输出类型；每个输出内容必须再次通过 ArtifactRegistry。
- Skill 不直接保存 Artifact；后续专业 Agent 运行时把 SkillArtifactOutput 交给 ArtifactService。
- LLM 输出必须经过 Artifact Schema 校验；正式确定性 Skill 只能执行其明示的、无模型事实变换。
- `extract_knowledge` 的模型内部响应不是 `SkillArtifactOutput`：它只包含必填的 entities、
  relations、properties、evidence 数组；校验通过后由代码转换为四个 SkillArtifactOutput。
- LLM Skill 最多允许初次调用加一次结构化修复；修复后仍无法通过 Pydantic 或业务完整性
  校验时必须返回不可重试 FAILED，且 outputs 为空，不能创建新的 Assignment 重复相同输入。
- publish_ontology 和 publish_graph 是有副作用的发布 Skill；SkillRegistry 未绑定 PublishGuard 时必须拒绝调用，绑定后也必须先通过 Agent 权限、候选版本、Approval 内容及 HumanReviewRequest 终态校验。
- `publish_ontology` 的候选输入为 `CandidateOntology` 或 `FusedCandidateOntology` 二选一，另须有绑定该候选 ID/checksum 的 `OntologyApproval`；两类候选同时输入或缺少候选均不合法。发布内容只固化已批准本体的公共事实字段，融合说明留在原候选 Artifact 中。

## 17. 自定义定义包契约

以下契约描述用户维护的扩展定义，不是运行时 TaskSpec 或 AgentSubTask：

~~~python
class BusinessSkillDraftInput(BaseModel):
    # 面向业务用户的创建接口；不包含任何内部 Artifact 或子任务字段。
    name: str
    solves_problem: str
    applicable_materials: str
    expected_result: str
    example_request: str
    additional_requirements: str = ""
    contract_profile_id: str | None = None  # 由业务化确认页回传，不要求用户手填

class BusinessAgentDraftInput(BaseModel):
    # 面向业务用户的创建接口；只选择已发布 Skill，不提交技术能力契约。
    name: str
    responsibility: str
    applicable_scenarios: str
    example_request: str
    allowed_skill_names: list[str]

class CustomSkillDefinitionInput(BaseModel):
    skill_key: str                 # ^[a-z][a-z0-9_]{2,127}$，创建后不可修改
    name: str
    description: str
    supported_subtask_types: list[str]
    input_artifact_types: list[str]     # 必须为已有 Artifact 类型
    output_artifact_types: list[str]    # 必须为已有 Artifact 类型
    parameters_schema: dict[str, Any]
    instruction_markdown: str           # 渲染为 SKILL.md 正文
    timeout_seconds: int
    max_retries: int
    business_input: dict[str, str] | None
    script_input_bindings: list[ScriptInputFieldBinding]  # 可选；由用户确认后随修订保存
    entity_record_adapter: EntityRecordAdapter | None  # 已确认的实体逐项函数映射
    function_adapter: ScriptFunctionAdapter | None  # 已确认的普通函数及逐参数来源
    generated_script_entrypoint: str | None  # 仅服务端生成，用户不能填写或上传覆盖
    contract_profile_id: str | None
    integration_mode: Literal["PIPELINE", "SIDE_OUTPUT", "STANDALONE"] | None
    result_semantics: Literal["DERIVE", "TRANSFORM", "REPORT"] | None
    # 用户不提交运行类型；发布服务根据本修订是否上传 .py 文件自动推导。

class CustomAgentDefinitionInput(BaseModel):
    agent_key: str                 # 创建后不可修改，不能覆盖内置 Agent
    name: str
    description: str
    accepted_subtask_types: list[str]
    accepted_input_types: list[str]
    produced_output_types: list[str]
    instruction_markdown: str      # 渲染为 AGENT.md 正文
    allowed_skill_names: list[str] # 唯一生成 permissions.yaml 的权限来源
    routing_examples: list[dict[str, str]]

class CustomSkillContractCheck(BaseModel):
    # 脚本发布门禁记录；不保存测试输入正文或脚本输出原文。
    check_id: UUID
    skill_revision_id: UUID
    contract_profile_id: str
    bundle_checksum: str
    runtime_profile_id: str
    protocol_version: str
    status: Literal["PASSED", "FAILED"]
    dependency_findings: list[ScriptDependencyFinding]
    diagnostics: list[str]
    duration_ms: float
    checked_at: datetime
~~~

`BusinessSkillDraftInput` 先由模型从代码注册的 Contract Profile 中推荐业务用途，再由用户确认并
由服务端生成不可见的 `CustomSkillDefinitionInput`。Profile 决定 `PIPELINE | SIDE_OUTPUT |
STANDALONE`、输入输出 Artifact 和结果语义；缺少 Profile 的历史调用只兼容映射到
`SemanticDocument → StructuredDataset`。`BusinessAgentDraftInput` 由服务端根据权威 Skill 目录
推导任务类型、输入和输出；
浏览器不能提交或覆盖这些值。`Custom*DefinitionInput` 仅供受控服务和历史内部 API 使用，
不得作为面向业务用户的表单字段。

每个稳定定义有 `DRAFT | ACTIVE | DISABLED` 状态；修订有
`DRAFT | PUBLISHED | SUPERSEDED` 状态。发布后稳定定义通过 `current_revision_id` 指向当前
修订。Skill 资源记录 `relative_path`、介质类型、二进制内容、大小和 checksum；发布 Skill 的
revision checksum 必须覆盖元数据、Markdown 和所有资源 checksum。资源内容不在 Registry
发现阶段加载。稳定定义只有进入 `DISABLED` 后才可物理删除：删除 Agent/Skill 的身份、修订及
Skill 资源，但不删除任何历史 MasterTask、Assignment、AgentRunResult、ExecutionTrace、日志或
Artifact；删除 Skill 前必须确认没有活动自定义 Agent 的 `permissions.yaml` 引用它。

自定义 Skill / Agent 的内部标识分别为 `skill_key` / `agent_key`，只用于运行时引用；用户填写的
名称为业务显示名称。Skill 的 `SKILL.md` 需同时记录 `name=skill_key` 和
`display_name=用户名称`，Agent 的 `AGENT.md` 记录 `agent_type=agent_key`、`name` 与
`display_name=用户名称`。运行期 `SkillRegistration.name` 继续使用 key，新增可选
`display_name` 仅供模型理解和用户展示，不能参与权限与调用身份校验。
发现阶段或模型 Prompt 中默认加载。没有 `.py` 时以 SKILL.md 调用模型；单个 `.py` 自动成为
入口；多个 `.py` 必须包含 `main.py`。该判断由发布服务完成，用户不选择 Skill 类型。

含 `.py` 的草稿在发布前必须取得与当前 revision、Contract Profile、资源包 checksum、
`runtime_profile_id` 和 `protocol_version` 完全一致的 `PASSED` 检查记录。检查使用 Profile 的
非空标准样例输入在隔离 Runner 中运行，响应先通过脚本协议模型，再核对精确输出类型集合，最后
通过目标 Artifact 的 Pydantic 内容模型。`requirements.txt` 只参与批准依赖检测；运行时不安装
任何依赖。不满足任一条件时发布失败，旧检查记录不能跨草稿或资源变更复用。
脚本推荐入口为 `run(inputs, parameters) -> dict`。标准响应包含 `outputs` 和可选
`quality_signals`。脚本可返回标准 `outputs`、走实体逐项适配、返回经 Schema
校验的完整单产物内容，或使用已确认的输出字段绑定；任意普通 JSON 不能
单独作为正式产物。异类型单输出可在已确认的 `script_output_bindings` 覆盖必填字段后
转换为目标标准内容，多个输出仍须返回标准 `outputs`。
平台不根据原始数据猜测必填业务字段；新草稿只有精确的
`run(inputs, parameters)` 才能直连，其他签名须先确认适配。

### 实体逐项脚本适配（ADR-090）

新建主流程实体转换 Skill 可保存以下不可变修订配置：

```python
class EntityRecordAdapter(BaseModel):
    function_name: str       # 用户脚本中只接收一个参数的函数
    parameter_name: str      # 平台逐项传入 EntityRecord.name
    source_field: Literal["name"] = "name"
    result_field: str        # 字典字段；__value__ 表示函数直接返回名称字符串
    target_field: Literal["name"] = "name"
```

只允许现有 `entity_transform` Profile，即 `EntityDataset → EntityDataset`。
发布前沙箱试运行与正式执行都使用同一配置；平台逐项调用脚本，
保留 entity_id、class_id、evidence 与旧别名，名称变化时把原名称加入 aliases。
结果缺失、为空或不满足 EntityDataset Pydantic 契约时失败，不生成下游 Artifact。
旧脚本的标准 `run(inputs, parameters)` 与既有标准字段映射仍可读取，
但通用输出映射不再作为新建 Skill 的默认交互。

按 ADR-091，新确认的实体业务函数不再通过运行请求的特殊参数执行。服务端把
`generated_script_entrypoint=_kg_skill_entry.py` 写入不可变修订，并将该生成文件与
用户原脚本共同纳入资源 checksum；用户原有 `main.py` 保持不变。试运行、发布门禁与
正式运行都选择该显式入口。没有生成入口标记的历史修订继续使用旧逐项适配路径。
业务用户只确认函数、参数含义及返回值含义，不提交入口路径或生成代码。

### 普通脚本函数参数适配（ADR-093）

```python
class ScriptFunctionArgument(BaseModel):
    parameter_name: str
    source: Literal["TASK_PAYLOAD", "INPUTS", "ARTIFACT_CONTENT", "ARTIFACT_FIELD"]
    artifact_type: str | None = None
    source_pointer: str | None = None

class ScriptFunctionAdapter(BaseModel):
    function_name: str
    arguments: list[ScriptFunctionArgument]  # 与函数参数顺序和名称完全一致
```

静态扫描只展示顶层 1–6 个普通参数的函数；不导入或执行用户代码。
`ARTIFACT_CONTENT` 传入唯一指定上游 Artifact 的完整标准内容，不含平台
envelope；`ARTIFACT_FIELD` 必须选当前 Profile 的已注册内容字段，并生成同名
`script_input_bindings`。确认后服务端生成独立标准入口；原脚本、确认映射和
生成入口共同绑定当前修订。函数原始返回值不因适配而自动变成标准 Artifact，
仍按标准 `outputs`／已确认输出映射及 Pydantic Schema 检查。
平台根据脚本实际读取的业务键选择参数来源；列表推导式与普通循环均可
静态识别。已注册来源字段的唯一同名或受限通用别名只作为界面建议，
须经用户确认；无法静态确认时不自动保存映射。方括号读取属于必需
路径，带默认值的 `get` 仅作为可选候选；后端在保存函数适配修订前
再次核对全部可静态识别的必需路径，缺失时返回具体路径，不进入试运行。

ADR-094 补充：当唯一输出类型有注册的 JSON 内容模型且脚本结果包含该模型
全部业务字段时，可先校验内容再自动封装为单项 `outputs`。实体同型转换还须
核对上游实体 ID、类型和证据。部分结果仍需用户确认输出字段映射或使用实体
逐项适配；多输出不得自动封装。检查响应只公开顶层字段名和诊断，不保存原始
脚本输出正文；`observed_output_shapes: dict[str, str]` 额外给出有界的字段类型、
列表长度及最多 12 个单项字段名，不包含业务值。受控生成入口对已有
函数直接返回的记录列表仅包装为 `{"result": [...]}` 供后续输出字段映射观察；
该列表不能直接视为标准 Artifact，仍须确认映射并通过 Pydantic 校验。

### 脚本输入字段映射（ADR-088）

受控定义接口可保存 `script_input_bindings: list[ScriptInputFieldBinding]`：

```python
class ScriptInputFieldBinding(BaseModel):
    script_field: str                 # 脚本读取的别名，修订内唯一
    artifact_type: str               # 必须属于 Skill 的正式输入类型
    source_pointer: str              # 指向标准 Artifact 内容的 JSON Pointer
    required: bool = True
```

映射随 Skill 不可变修订参与 checksum。沙箱契约试运行与正式执行都把解析结果传入
`parameters["input_fields"]`；未配置映射的历史脚本收到空字典并继续使用原协议。
必填路径缺失、同类型输入不唯一或输出不满足现有 Artifact Pydantic Schema 时停止执行，
不得把无法确认的数据送给下游。创建弹窗和管理页会展示经过白名单校验的模型建议，
用户确认后创建新草稿修订。脚本只能从标准 Artifact 内容中读取字段；输出必须写入
已注册的标准 Artifact 内容并通过 Pydantic 校验，不提供独立附加对象。
脚本输出字段映射按 Skill 修订保存：
```python
class ScriptOutputFieldBinding(BaseModel):
    artifact_type: str  # 已注册的唯一输出类型
    source_pointer: str  # 指向脚本普通 JSON 结果
    target_field: str    # 已注册输出内容的顶层字段
    record_field_bindings: dict[str, str]  # 可选：标准记录字段 -> 脚本字段
```
受控接口 `PUT /api/v1/extensions/skills/{skill_id}/output-bindings` 只保存人工确认配置，
创建新草稿修订并使旧契约检查失效。发布前和运行时共用映射，缺失字段或目标 Schema
校验失败即中止。创建弹窗和 Skill 管理页可根据试运行观测字段展示标准输出用途候选，用户确认后生成新草稿修订并重新试运行。


### 函数入口的嵌套字段映射（ADR-095）

`ScriptFunctionAdapter` 新增可选 `field_bindings: list[ScriptNestedFieldBinding]`，
与 `arguments` 一起由用户确认并保存于同一 Skill 修订：

```python
class ScriptNestedFieldBinding(BaseModel):
    parameter_name: str
    artifact_type: str
    source_pointer: str  # 例如 /entities/*/entity_id
    target_pointer: str  # 例如 /entities/*/id
```

来源必须是所选 Profile 中已注册 Artifact 内容模型的字段；目标必须是当前上传脚本
所选函数静态读取的字面量路径。绑定参数必须选择唯一 Artifact 的
`ARTIFACT_CONTENT`；来源和目标只能位于同一列表记录内。用户不确认时不应用推荐。
执行时仅在隔离入口对输入副本增加别名，原 Artifact 不变；试运行和正式运行共用
同一配置。输出仍须通过既有标准 Artifact 契约和实体身份、类型、证据校验。

### 脚本输出列表字段推荐（ADR-096）

平台从沙箱试运行观测顶层字段及列表单项字段，结合所选 Contract Profile 的
已注册输出 Artifact Schema，预填输出字段对应关系。推荐不等于确认；用户确认后
创建新 Skill 修订并重新试运行。逐项映射不补造缺失值，同型 EntityDataset 只能
依据相同实体 ID 继承证据，继续校验 ID、类型和证据完整性。真正的实体合并需要
关系与证据重定向，本版不通过普通字段映射放行。

### 实体对齐的模型生成门禁（ADR-097）

历史 EntityDataset 内容模型仍允许默认空 entities 以便读取既有产物；新 LLM 输出使用
EntityDatasetGenerationContent，要求显式提交 entities 数组。align_entities 对照
真实上游 Artifact 检查非空、ID 唯一、无合并时保留 ID／class_id／原有证据，并要求
entity_count、original_entity_count、merged_count 与实际内容一致。失败走现有一次
结构化修复，仍失败不得创建新 Artifact；摘要及指标不能代替内容。

### 属性脚本非空保全与列表别名（ADR-098）

`ScriptNestedFieldBinding` 允许已确认的单层列表别名（例如 `/properties → /attributes`）及该列表内的逐项字段别名；跨列表记录字段必须同时确认列表别名。来源必须属于当前 Profile 注册 Schema，目标必须是脚本静态读取的路径。生成入口只改输入副本。

`property_transform` 的契约试运行使用非空 PropertyDataset；输出 PropertyDataset 必须保留输入 property_id 集合、entity_id、property_type_id 与 evidence。普通 JSON 逐项输出若只包含局部字段，仅可按已确认 property_id 匹配上游记录后继承其余标准字段；匹配失败或非空输入变空输出均拒绝。缺失的单位不能通过映射补造。

