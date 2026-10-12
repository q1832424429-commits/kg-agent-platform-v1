# V1 最终架构

## 1. 文档目的

本文只描述项目最终确认的 V1 架构。它是实现和评审的架构基线，不包含已经放弃的 RAG-Anything、知识查询、Workflow 编排 Agent、执行 Agent、动态 Skill DAG 或文档驱动子 DAG。

## 2. 总体架构

```text
┌───────────────────────────────────────────────────────────────┐
│ 用户                                                          │
│ 自然语言、多轮修改、文档上传、本体选择、本体/图谱人工审核       │
└────────────────────────────┬──────────────────────────────────┘
                             │
                             ▼
┌───────────────────────────────────────────────────────────────┐
│ FastAPI 接口层                                                │
│ 对话、任务、文档/本体分型上传、Artifact、审核、事件、健康检查  │
└────────────────────────────┬──────────────────────────────────┘
                             │
                             ▼
┌───────────────────────────────────────────────────────────────┐
│ 总控 Agent（真实 LLM Agent）                                  │
│                                                               │
│ 读取上下文 → 识别 START/CONTINUE/MODIFY → 创建/修订 TaskSpec  │
│ → 检查可复用 Artifact → 拆解 AgentSubTask → 指定专业 Agent    │
│ → 确定性校验 → 一次发布完整计划 → 最终检查用户目标            │
└────────────────────────────┬──────────────────────────────────┘
                             │ 发布子任务和 Assignment
                             ▼
┌───────────────────────────────────────────────────────────────┐
│ SharedContext（每个 MasterTask 一个逻辑上下文）                │
│ TaskSpec revisions、AgentSubTasks、Assignments、ArtifactRefs、 │
│ AgentRunResults、Review、Memory、ExecutionTraceRefs            │
└──────────────┬──────────────┬──────────────┬───────────────────┘
               │              │              │
               ▼              ▼              ▼
┌───────────────────────────────────────────────────────────────┐
│ 专业 Agent（全部是真实 LLM Agent）                            │
│                                                               │
│ 数据源解析 │ 知识抽取 │ 知识融合 │ 质量评估 │ 图谱管理更新    │
│ 以及用户发布的受控自定义专业 Agent                            │
│                                                               │
│ 认领 Assignment → 读取 Context View → 选择允许的 Skill       │
│ → 调用所选模式 Skill → 校验输出 → 保存 Artifact/RunResult     │
└────────────────────────────┬──────────────────────────────────┘
                             │
                             ▼
┌───────────────────────────────────────────────────────────────┐
│ 确定性领域服务                                               │
│ TaskState、Assignment、Context、Artifact、Reuse、Memory、      │
│ HumanReview、Validator、PublishGuard、Trace、Evaluation        │
└────────────────────────────┬──────────────────────────────────┘
                             │
               ┌─────────────┴─────────────┐
               ▼                           ▼
       PostgreSQL                       MinIO
  状态、元数据、事件、轨迹        文档和大型 Artifact 内容
```

## 3. 总控 Agent

### 3.1 职责

总控 Agent 是用户目标与专业 Agent 协作之间唯一的智能协调入口，负责：

1. 加载当前 ConversationContext、MasterTask、TaskSpec、已有 Artifact、审核状态和相关记忆。
2. 识别顶层任务意图 `START`、`CONTINUE` 或 `MODIFY`。
3. 自动生成或修订 TaskSpec；TaskSpec 不需要单独人工确认。
4. 判断当前输入的 Artifact 类型和已有工作成果。
5. 提出 Artifact 复用计划，并交由 ArtifactReuseService 做确定性校验。

总控模型不直接生成数据库主键、版本、状态、时间戳或 Skill 的机械伴随输出。它只输出内部
`TaskPlanDraft`：用户意图、TaskSpec 语义字段、Agent 级子任务、计划内节点别名和依赖。总控内的
确定性计划编译器随后把草案转换为公开 `TaskDecompositionResult`：生成 UUID，写入 revision、
初始状态和时间，根据 Skill Registry 补齐同一个 Skill 必然同时产生的原子 Artifact 集合，并
应用已选本体与内部续办约束。编译后的结果仍须通过全部 Validator，只有合法结果才能发布到
SharedContext。该编译器是总控内部代码，不是新的 Agent、Workflow 编排器或执行 Agent。
6. 将 MasterTask 拆为 Agent 粒度的 AgentSubTask。
7. 为每个子任务指定 `assigned_agent_type`、依赖、输入、预期输出、完成条件、质量要求和人工审核要求。
8. 调用 SubTaskValidator 校验计划；校验失败时根据结构化错误修复，最多重试次数由配置确定。
9. 通过 TaskStateService 把完整计划原子发布到 SharedContext，并仅为当前依赖已满足的子任务创建 AgentAssignment。
10. 全部计划进入终态后检查用户最终目标；成功则结束，失败、缺失或需返工时发布新的修复 revision。
11. 严格按用户明确终点控制任务范围，不把解析、抽取、候选图谱构建和发布入库相互扩大。
12. START 时一次性动态生成当前最终目标所需的完整 AgentSubTask 计划；它不是固定流水线，只包含当前目标需要的工作。
13. 尚未产生的上游 Artifact 不填写虚构 ID，而以 `dependency_subtask_ids` 表达；依赖成功后由系统按 AgentRunResult 血缘动态绑定真实输入。
14. 正常依赖解锁期间不重复调用总控；正式 Worker 和验证实验室使用相同的确定性依赖推进规则。

### 3.2 边界

总控 Agent 不直接执行文档解析、知识抽取、融合、质量评估或图谱发布。它不调用专业业务 Skill，也不直接修改数据库和 SharedContext。

总控 Agent 只规划 AgentSubTask，不规划专业 Agent 内部 Skill 调用步骤。因此它不是已经否决的 Workflow Planner。

完整计划可以包含尚待前置任务产出 Artifact 的后续 AgentSubTask，但这些任务必须保持
BLOCKED，不能提前认领。减少子任务不等于跨越专业 Agent 边界或人工审核门禁；同一
专业 Agent 只有在职责一致且不隐藏必要依赖时，才可把多个目标合并为一个 AgentSubTask。

### 3.3 固定内部状态机

```text
load_context
    ↓
recognize_intent
    ↓
build_or_revise_task_spec
    ↓
inspect_existing_artifacts
    ↓
decompose_master_task
    ↓
validate_subtasks
    ├── 失败 → repair_decomposition → validate_subtasks
    └── 成功 → publish_subtasks → build_user_response
```

### 3.4 验证实验室的发布前计划审核

8002 Agent 验证实验室为了人工评测意图识别和任务拆解，使用总控的两阶段发布能力：总控先完成
`TaskPlanDraft → TaskDecompositionResult → Validator`，但不写入 SharedContext；候选计划及真实输入
暂存于测试端隔离 MinIO。页面随后弹出审核窗口，用户可以批准、用自然语言要求总控重新规划，或
调整候选子任务的优先顺序。只有批准后，TaskStateService 才原子写入 TaskSpec revision、
AgentSubTask、依赖和 AgentAssignment。依赖约束始终优先于人工调整的展示/执行优先级。

该门禁用于验证实验室的 Agent 准确率评测，不是独立的 TaskSpec 确认，也不替代本体和图谱的
Human Review/PublishGuard。正式端是否启用同一发布前审核，由后续产品范围确认。

该状态机只描述总控 Agent 自身运行步骤，不表达完整知识图谱构建业务流程。

### 3.4 完整计划的依赖推进与最终检查

```text
总控一次发布完整 AgentSubTask 计划
    ↓
SharedContext 保存完整任务和依赖
    ├── 无依赖/依赖已成功 → 创建 Assignment，可由不同专业 Agent 并行认领
    ├── 依赖未完成 → BLOCKED，等待前置 Artifact
    ├── 质量任务带审核标记 → 创建/等待 Human Review
    ├── 审核批准 → 生成 Approval 并解锁对应发布任务
    └── 全部计划进入终态 → TaskCoordinator 调用总控做最终目标检查
                                  ├── 目标完成 → 空 subtasks → MasterTask SUCCEEDED
                                  └── 失败或缺失 → 仅发布必要修复任务的新 revision
```

TaskCoordinator 不是 Agent，也不包含固定知识图谱工作流。它只处理依赖等待、审核门禁
和“全部计划是否终止”的判断；正常执行期间不逐步召回总控重新规划。

总控调用必须区分三种运行目的：真实用户消息使用 `USER_TURN`，由总控结合版本化对话
历史判断 START/CONTINUE/MODIFY；审核修改、失败恢复等使用 `SYSTEM_CONTINUATION`，固定
CONTINUE 并沿用当前 TaskSpec，但仍发布必要续办子任务；全部计划终止后的系统检查使用
`FINAL_VERIFICATION`，同样固定 CONTINUE 并沿用当前 TaskSpec，只判断“目标已完成”或
“需要哪些修复子任务”。两类系统操作都不能被重新解释为新任务。

## 4. SharedContext

### 4.1 定位

SharedContext 是单个 MasterTask 内所有 Agent 共享的逻辑事实来源。它统一承载任务状态和引用，避免 Agent 通过不可审计的自由对话传递正式业务结果。

### 4.2 内容

```text
SharedTaskContext
├── ConversationContext 引用和摘要
├── MasterTask
├── 当前 TaskSpec revision
├── TaskSpec 历史 revision 引用
├── AgentSubTasks
├── AgentAssignments
├── ArtifactRefs
├── AgentRunResults
├── HumanReviewRequests
├── ShortTermMemory
├── LongTermMemoryRefs
└── ExecutionTraceRefs
```

完整文档、数据集和图谱不能直接放入 SharedContext，只能以 ArtifactRef 引用。
长期记忆正文同样不进入 SharedContext：Mem0 Cloud 保存正文并执行语义检索，
SharedContext 只保存本轮召回结果的 `MemoryRef`、摘要和 `mem0://` 来源引用。
角色化 Context View 会再次限制可见记忆数量：最近五轮公开对话为十条短期消息，Mem0
相关长期记忆最多三条。TaskSpec、Artifact、子任务和审核状态属于结构化任务事实，不占十三条
记忆额度。供应商时间戳只用于 MemoryService 内部排序，不直接注入总控或专业 Agent。

### 4.3 读写方式

Agent 不能自由覆盖 SharedContext。写操作通过命令式确定性服务完成，并追加审计事件。关键事件包括任务创建、TaskSpec 修订、子任务发布、Assignment 认领、Artifact 创建、审核请求和任务完成。

### 4.4 Context View

ContextService 根据当前 Agent、Assignment 和权限生成最小 Context View：

- 总控 Agent 查看任务摘要、全部子任务状态、关键 Artifact 摘要和审核状态。
- 数据源解析 Agent 只查看解析目标、原始文件引用、解析参数和验收条件。
- 知识抽取 Agent 只查看语义文档、本体版本、抽取范围和必要术语。
- 知识融合 Agent 只查看待融合数据、本体、冲突策略和相关血缘。
- 质量评估 Agent 只查看候选结果、证据和质量要求。
- 图谱管理更新 Agent 只查看待应用 ChangeSet、Approval 和目标发布版本。

不得把完整 SharedContext 或全部聊天历史直接注入每次模型调用。

## 5. AgentSubTask 与 AgentAssignment

### 5.1 AgentSubTask

AgentSubTask 是总控 Agent 产生的逻辑工作单元，粒度为“一个专业 Agent 可以独立负责的一项目标”。它描述做什么、由谁负责、依赖什么、使用哪些输入、预期产生什么以及如何判断完成。

AgentSubTask 可以在 START 时作为完整计划一次发布，也可以因 MODIFY、失败返工或最终检查缺失而新增 revision；它不是 DAG Runtime 中的 Skill 节点。依赖关系作为任务数据持久化，由确定性服务判断何时转为 READY。

### 5.2 AgentAssignment

AgentAssignment 是一个 AgentSubTask 的一次具体执行尝试。`assigned_agent_type` 指定逻辑 Agent 角色，`claimed_by` 记录实际 Worker 实例。

AssignmentService 负责：

- 只允许匹配角色的 Agent 实例认领。
- 原子认领和重复认领保护。
- lease、heartbeat 和 fencing token。
- 超时后安全释放或重试。
- 校验完成者仍持有当前 fencing token。

### 5.3 并发原则

同一个 MasterTask 可以并行执行输入输出互不冲突的 Assignment。以下操作必须串行：

- 写入同一个 Artifact 逻辑版本。
- 应用同一候选本体或候选图谱的 ChangeSet。
- 本体发布。
- 图谱发布。

## 6. 专业 Agent

### 6.1 通用执行模型

```text
认领 Assignment
    ↓
加载过滤后的 Context View
    ↓
理解子任务、输入和完成标准
    ↓
从本 Agent Skill allowlist 中选择 Skill
    ↓
确定性校验 Skill 权限和输入兼容性
    ↓
执行 LLM 或正式确定性 Skill
    ↓
检查是否得到预期输出
    ↓
保存 Artifact 和 AgentRunResult
    ↓
完成、部分成功、返工、重试、等待用户或失败
```

### 6.2 数据源解析 Agent

负责识别并解析真实 PDF、DOCX、XLSX、PPTX 和图片输入，产出 DocumentManifest、ParsedDocument 和 SemanticDocument。当前 DOCX 具备正式轻量文本解析器；其他复杂多模态内容由 LLM 联通实现处理，生产级解析算法仍待逐步替换。

### 6.3 知识抽取 Agent

支持本体候选抽取和实例知识抽取两类子任务。实例抽取前必须获得 PublishedOntology，并统一由一个 `knowledge_extract` AgentSubTask 调用 `extract_knowledge` Skill。模型使用专用扁平 Schema 一次返回 entities、relations、properties、evidence 四个必填数组，不生成 Artifact 外壳；代码检查关系端点、属性主体和证据覆盖后，确定性产生 EntityDataset、RelationDataset、PropertyDataset 和 EvidenceDataset。实体、关系和属性不再拆为三个串行 Skill。

### 6.4 知识融合 Agent

负责外部本体/候选本体对齐融合，以及实体对齐、消歧、关系标准化和冲突分析。可产出 FusedCandidateOntology、对齐后的 EntityDataset、标准化 RelationDataset 和 ConflictReport；不负责构建 CandidateGraph。

### 6.5 质量评估 Agent

负责对候选图谱的完整性、一致性、证据覆盖和冲突情况作结构化评估，产出 GraphQualityReport，并可以提出返工建议。候选本体不经该 Agent 评估，而是直接交给用户审核。

### 6.6 图谱管理更新 Agent

负责把已融合的实体、关系、证据和冲突报告构建为 CandidateGraph；也可将本轮新增知识与已有
CandidateGraph 基线比对，生成 GraphChangeSet 草案。该草案不改变原图谱，只有
下游 `apply_graph_changeset` 才会把草案确定性应用为新候选版本。用户在新 CandidateGraph 预览中
查看、修改和批准图谱；该 Agent 继续负责应用用户编辑产生的 OntologyChangeSet 或 GraphChangeSet，
并在 PublishGuard 校验成功后调用发布 Skill。该 Agent
是唯一可以构建候选图谱及调用本体/图谱发布类业务 Skill 的专业 Agent。`publish_graph`
确定性保留已批准候选图谱内容；持久化服务随后在同一 PostgreSQL 事务中写入图查询投影。

增量图谱更新固定为两个 AgentSubTask：`graph_changeset_generate` 只产出 `GraphChangeSet` 草案；
`graph_changeset_apply` 依赖该草案并以同一旧 `CandidateGraph` 为基线，只产出新的
`CandidateGraph`。两者不可合并，因此用户始终能看到“旧图谱 + 建议变更 + 应用后的候选图谱”三层
可追溯内容；新候选图谱才进入质量评估、图谱预览/编辑、批准与发布。

## 7. Skill Registry

Skill Registry 保存 Skill 的机器可读元数据：

```text
name、version、description、owner_agent、allowed_agents、
input_artifact_types、output_artifact_types、parameters_schema、
requires_dedicated_subtask、side_effect、timeout、retry_policy、lifecycle
```

`configs/skills/v1.yaml` 只保存全局的 Skill—Agent 调用权限映射；Registry 启动时把该映射
注入每个 Skill 注册项，并拒绝未授权或不存在的 Skill。

V1 业务 Skill 共享同一注册信息和 Artifact 契约，不划分 LLM 或脚本模式。无 Python 资源的 Skill 由专业 Agent 加载正文作为提示词；含 Python 资源的自定义 Skill 进入隔离 Runner；已有内置专用代码的 Skill 直接加载该实现。所有路径都经过相同权限、Schema、审核和发布门禁。

每个 Skill 的业务定义独立存放于 `src/kg_agent/skills/catalog/<skill_name>/SKILL.md`：Markdown
前置元数据保存运行契约，正文保存执行说明。Registry 仅扫描这些 Markdown 定义；若目录中存在
`implementation.py` 则加载该 Skill 的正式算法，否则用通用 LLM 执行器读取正文、投影输入
Artifact 并生成结构化输出。全局 YAML 只做 Agent 调用权限校验，不参与执行策略或提示词加载。

新增 Skill 通过注册发现；新增专业 Agent 通过 Agent Registry 暴露能力目录。总控 Agent 从能力目录获得当前可分配 Agent，而不是依赖硬编码名称分支。

## 8. Artifact

Artifact 是正式业务产物和 Agent 间数据交换协议。SharedContext 只保存 ArtifactRef，内容存储由 ArtifactService 管理。

V1 Artifact 家族包括：

```text
RawDocument、ExternalOntology、StructuredDataset
DocumentManifest、ParsedDocument、SemanticDocument
CandidateOntology、FusedCandidateOntology
OntologyChangeSet、OntologyApproval、PublishedOntology
EntityDataset、RelationDataset、PropertyDataset、EvidenceDataset
ConflictReport、CandidateGraph、GraphQualityReport
GraphChangeSet、GraphApproval、PublishedGraph
```

Artifact 默认不可变。修订内容产生新 Artifact 或 ChangeSet，并使用 parent_artifact_ids 保留血缘。ArtifactReuseService 对复用进行确定性校验。

跨 TaskSpec revision 的复用按 Artifact 的真实影响字段判断：原始输入、领域、本体版本、
父血缘和内容相关的抽取参数不变时，允许把已经完成的文档、实体或关系结果用于下一轮
目标扩展；流程开关、最终输出或业务目标文字变化本身不使旧 Artifact 失效。

输入窗口负责明确类型而不做模型猜测：普通资料进入 `RawDocument`，专用 JSON 本体
窗口进入 `ExternalOntology`。外部本体必须先通过版本化内容契约，上传成功不改变其
候选状态，也不绕过后续融合和 Human Review。候选本体不执行
自动质量评估，而是直接返回给用户预览、修改和确认。

第三阶段的物理存储边界为：

~~~text
SkillArtifactOutput
        ↓ ArtifactRegistry 校验与规范化
ArtifactService
        ├── SHA-256 checksum
        ├── Assignment fencing 校验
        ├── 容量、状态和父级血缘校验
        ├── MinIO：不可变内容
        └── PostgreSQL：ArtifactRef + object_key + lineage
                └── PublishedGraph：graph + nodes + edges + properties + evidence
                         ↓
                  SharedContext 新快照
~~~

ArtifactReuseService 是无 LLM 的兼容性门禁，同时检查状态、类型、Schema、
TaskSpec revision、本体版本和父输入 checksum。

生产应用通过 PostgresArtifactService 统一管理数据库提交和对象补偿。MinIO 网络上传前只进行无锁预检；上传后再以统一锁顺序二次校验 Assignment lease、fencing、父级状态和容量，避免上传过程阻塞 Worker 心跳。内容 checksum 进入对象键，冲突幂等请求不会互相覆盖。正式发布 Skill 默认关闭；只有绑定第六阶段 PublishGuard，并通过当前候选版本、Approval 和审核终态校验后才允许执行。


## 9. Human Review

### 9.1 本体审核

```text
CandidateOntology / FusedCandidateOntology + 差异与证据
                         ↓
                HumanReviewRequest
             ┌───────────┼───────────┐
             ▼           ▼           ▼
           修改         拒绝         批准
             │           │           │
OntologyChangeSet      返工     OntologyApproval
             │                       │
图谱管理更新 Agent                   │
应用修改生成新候选                   │
             │                       │
             └────再次人工审核───────┤
                                     ▼
                            图谱管理更新 Agent
                                     ↓
                            PublishedOntology
```

本体发布选择当前已审核的一个 `CandidateOntology` 或 `FusedCandidateOntology`，
并携带与其 ID、checksum 绑定的 `OntologyApproval`；两个候选同时出现时不自动猜选。
`publish_ontology` 只复制已批准的本体事实，融合说明保留在原候选 Artifact 中。

### 9.2 候选图谱审核

```text
CandidateGraph + GraphQualityReport + ConflictReport + EvidenceRefs
                              ↓
                     HumanReviewRequest
                  ┌───────────┼───────────┐
                  ▼           ▼           ▼
                修改         拒绝         批准
                  │           │           │
           GraphChangeSet    返工     GraphApproval
                  │                       │
                  └────重新评估───────────┤
                                          ▼
                                 图谱管理更新 Agent
                                          ↓
                                  PublishedGraph
```

Human Review 是任务门禁，不是第七个 Agent。

候选图谱本身是不可变 Artifact，不保存浏览器布局。正式工作台和验证实验室在读取
`CandidateGraph` 或 `PublishedGraph` 后，由无副作用的 `GraphPreviewService` 生成只读的
节点—边投影：节点包含实体、属性和证据，边包含关系类型和证据。投影发现悬空边、孤儿
属性或重复 ID 时仅返回告警，不修改原始 Artifact，也不替代质量评估。

两个页面使用同一份本地 G6 图谱工作台展示该投影。它支持临时拖拽、缩放、布局、筛选和
检索；这些视图状态不会进入 SharedContext 或 Artifact。候选图谱审核人员可查看溯源，并
以 `ADD/UPDATE/DELETE/MERGE/SPLIT` 生成 `GraphChangeSet` 草稿。提交后，图谱管理更新 Agent
通过既有 `apply_graph_changeset` Skill 校验目标版本并原子生成新的 CandidateGraph；新版本仍须
经过图谱质量评估和人工确认后才可发布。

发布时 `publish_graph` 只把已批准 CandidateGraph 提升为 PublishedGraph，不重新调用模型生成
知识。PublishedGraph 的不可变 JSON 正文保留在 MinIO；PostgreSQL 同步保存图版本、节点、
有向边、属性和证据查询表，并用外键绑定 PublishedGraph Artifact、来源 CandidateGraph 和
GraphApproval。正式端与验证实验室使用各自隔离数据库，任一关系表写入失败都会回滚本次
发布元数据，不能出现 Artifact 已发布而 PostgreSQL 图投影不完整的状态。

本体与图谱在用户点击“批准”时同时填写业务发布名称。该名称经 Approval 绑定目标 Artifact ID 和
checksum，并由确定性发布 Skill 原样带入 PublishedOntology / PublishedGraph 及轻量 ArtifactRef。
正式端和验证实验室各自提供“已发布资产”目录：仅显示真实 `publish_ontology` / `publish_graph`
产物，支持查看本体 JSON 与交互预览已发布图谱。跨任务选择资产时仍会创建保留父血缘的任务内
导入副本；导入副本的生产者为 `published_asset_import`，因此不会再次出现在资产目录。
资产目录允许修改显示名称或将目录项标记为 `REMOVED` 并移出新任务可选列表；
已审核 JSON、checksum 和历史任务引用不可在目录中覆盖。内容修订应创建新候选版本，
通过人工审核门禁后另行发布。

## 10. 完整构图任务的 Agent 级协作示例

下列关系是总控 Agent 在完整构图目标下可能一次性安排的任务生命周期，不是固定 Workflow 或 Skill DAG。其中 T4 和 T10 是 Human Review 门禁，不是 AgentSubTask，也不会分配给专业 Agent：

```text
T1  数据源解析             → 数据源解析 Agent
T2  候选本体抽取           → 知识抽取 Agent
T3  本体对齐与融合（如需） → 知识融合 Agent
T4  用户审核候选本体       → Human Review
T5  发布已确认本体         → 图谱管理更新 Agent
T6  基于已发布本体联合抽取实体、关系、属性和证据 → 知识抽取 Agent
T7  实例知识融合           → 知识融合 Agent
T8  构建候选图谱           → 图谱管理更新 Agent
T9  候选图谱质量评估       → 质量评估 Agent
T10 用户审核候选图谱       → Human Review
T11 发布已确认图谱         → 图谱管理更新 Agent
```

若用户直接提供 SemanticDocument、EntityDataset、CandidateGraph 或 Approval，前置 AgentSubTask 可以省略。总控 Agent 应从对应 Artifact 位置继续，而不是重新执行无关工作。

## 11. 状态流转

AgentSubTask 已确认的状态集合：

```text
PENDING → BLOCKED → READY → ASSIGNED → CLAIMED → RUNNING
                                             ├→ WAITING_HUMAN
                                             ├→ SUCCEEDED
                                             ├→ FAILED
                                             ├→ CANCELLED
                                             └→ SUPERSEDED
```

第二阶段已经冻结合法迁移表；普通迁移由 TaskStateService 拒绝非法跳转，Assignment 租约过期恢复只允许将 CLAIMED/RUNNING 子任务重新置为 ASSIGNED 并创建递增 attempt。

专业 Agent 的 AgentRunResult 状态：

```text
SUCCESS
PARTIAL_SUCCESS
NEEDS_USER_INPUT
RETRYABLE_FAILED
REWORK_REQUIRED
FAILED
CANCELLED
```

## 12. 运行与存储边界

V1 Docker Compose 包含：

```text
api       FastAPI 接口
worker    总控 Agent 与五类专业 Agent 的运行进程
postgres  状态、元数据、事件、短期记忆、长期记忆引用、审核、轨迹
minio     原始文件与大型 Artifact 内容
mem0      用户授权的长期记忆正文、成功经验与语义召回
```

worker 是确定性的后台运行进程，不是执行 Agent。V1 使用 PostgreSQL 原子认领 Assignment，不引入 Redis、Celery 或 Temporal。

用户明确选择纯本机 uv 联调时，可按 ADR-074 使用两个隔离 SQLite 文件和两个受控本地 Artifact
目录分别运行 8000/Worker 与 8002。该适配层只替代开发环境物理存储，不改变 Task、SharedContext、
Artifact、审核或 Registry 契约；未启用本机模式时仍严格使用 PostgreSQL 与 MinIO。

ModelGateway 隔离 DeepSeek API；测试使用 FakeModelAdapter。具体生产模型标识为 TODO，通过环境变量配置。

## 13. 记忆、追踪与自进化

- 短期记忆保存当前会话和任务必要上下文。
- 每个已形成 MasterTask 的用户轮次同时持久化公开 `USER` 消息和基于已校验总控结果生成的公开
  `ASSISTANT` 摘要；每次 Agent 调用只取最近五轮十条。完整正文位于 PostgreSQL 会话表，
  SharedContext 当前快照只保留这十条轻量引用。
- 长期记忆正文由 Mem0 Cloud 保存，内容包括用户授权的稳定偏好、术语映射、本体选择和完整任务成功经验。
- 总控 Agent 每轮拆解前用当前用户目标执行 Mem0 搜索；召回摘要进入 ControllerRequest，发布后成为 SharedContext 的轻量引用，专业 Agent 再从角色化 Context View 使用。
- 召回采用“较大候选集 → 相关度阈值/供应商重排 → 失效状态过滤 → 文本近似去重 → 最终 Top-K”；
  最终最多三条摘要交给 Agent，完整候选和时间戳不进入 Prompt。
- 自动成功经验只在 MasterTask 成功收口后，由确定性服务聚合当前目标、成功 AgentRunResult、
  Skill 路径和最终 Artifact 写入一次；单个专业 Agent 完成或计划刚发布时不写 Mem0。
- 聚合经验写入前执行三档语义策略：相似度不低于 0.92 时跳过，0.80（含）到 0.92（不含）
  时合并公开事实并原位更新命中记忆，低于 0.80 时新增。稳定 `experience_key` 与 PostgreSQL
  幂等记录阻止重试重复。
- Mem0 搜索或后台经验写入失败时记录告警并按无长期记忆继续，不回滚 PostgreSQL 中已经提交的任务事实。
- 用户显式写入使用授权字段；Mem0 API Key 始终只在服务端。
- 专业 Agent 的结果先提交 PostgreSQL 终态并停止 Assignment 心跳，不访问 Mem0；全部目标完成后
  协调器才触发聚合经验写入，任何云端延迟都不能使已完成 Assignment 被再次续租或回滚。
- WSL 原生 Docker 需要出站代理时，只向应用容器注入 `HTTP_PROXY/HTTPS_PROXY`，内部数据库与对象存储地址必须进入 `NO_PROXY`。
- 8002 验证实验室复用同一 MemoryService 边界，但使用独立的测试 `app_id + user_id`。每轮在总控拆解前召回，任务完整成功后写入一条聚合经验；实验室页面显示召回、创建、合并或跳过事件，供人工观察记忆是否参与路由。
- 正式端和测试端的 Mem0 查询必须同时过滤 `app_id` 与 `user_id`，不能只依赖 MasterTask、conversation_id 或单一用户标识隔离环境。
- 8002 验证实验室的 Artifact 内容使用独立 MinIO Bucket；测试与正式环境可以复用同一 MinIO 服务和凭据，但 Bucket 名必须不同。这样 8002 进程重启后，隔离 PostgreSQL 中的 Artifact 元数据仍能解析到可读取的不可变内容，多轮复用不会形成悬空引用。
- 记忆必须结构化并保留来源，不保存隐藏思维过程。
- ExecutionTrace 关联 task_id、subtask_id、assignment_id、agent_run_id、model_call 和 skill_call。
- 每次 Skill 模型调用的完整原始结构化回答保存到任务日志目录的独立受限文件；实时 JSONL
  只记录文件引用、字符数和 SHA-256，不嵌入大段正文。该文件是诊断证据，不是业务 Artifact。
- Skill 输出通过校验并持久化后，AgentRunResult 记录 `invocation_id + skill_name + 输入 Artifact
  IDs + 输出 Artifact IDs/类型`。下游只沿依赖和该结果取得真实编号，通过 ArtifactService
  读取、校验 checksum 并再次按注册 Schema 校验，绝不直接消费模型原始回答。
- 用户反馈和执行轨迹进入 Experience Registry，用于离线评测和生成版本化优化建议。
- V1 不允许 Evolution Engine 自动更新或发布生产 Prompt、Skill、Agent 配置和 Planner 策略。

## 14. 自定义 Agent 与 Skill 扩展

在不替代五个内置专业 Agent 的前提下，用户可发布受控自定义专业 Agent 与 Skill。定义包
完全保存在 PostgreSQL，而非写入仓库目录：Skill 包含 `SKILL.md`、可选资源；Agent 包含
`AGENT.md` 和唯一的 `permissions.yaml`。稳定身份表持有 `current_revision_id`，每次编辑创建
不可变新 revision；只有已发布指针参与新的任务发现，前端不显示内部 revision。

面向业务用户的创建表单只收集“要解决的问题、适用资料、预期结果、典型提问”和 Agent 职责及
已发布 Skill 授权。自定义 Skill 创建时，模型只能从代码注册的 Contract Profile 中推荐业务用途，
代码重新解析并验证 Profile，前端以业务语言显示输入内容、输出内容和建议位置，用户确认后才创建
和发布草稿。Profile 覆盖流程型、旁路型和独立型能力；自定义 Agent 的子任务、输入和输出继续从
所选 Skill 确定性推导。用户不会看到 Artifact、子任务类型、内部 key、YAML 或执行模式。

自定义定义同时持有两个不可混用的名称：用户填写的业务显示名称（`display_name`）写入
`SKILL.md` / `AGENT.md`、能力目录、提示词和管理界面；随机内部 key 仅用于 Registry、
`permissions.yaml`、SkillInvocation、Assignment 和 Artifact 血缘。用户改名不得隐式改写内部
key；对旧 revision，运行期投影从稳定身份记录补齐显示名称，避免篡改不可变历史定义。

管理页面只允许对已停用的自定义定义执行物理删除。删除会移除该 Agent/Skill 的稳定身份、内部
revision 和 Skill 资源，使其不可恢复；但历史 MasterTask、Assignment、ExecutionTrace、日志和
Artifact 仅保存运行时快照/引用，不会被级联删除，仍可审计。活动或草稿定义必须先停用；删除 Skill
前还会再次检查没有活动自定义 Agent 的 `permissions.yaml` 引用它。

```text
前端定义表单
    ↓
PostgreSQL 草稿修订（Agent / Skill / 资源）
    ↓ 静态契约校验 + 用户发布
当前发布指针
    ↓
内置 Registry + Custom Provider → 本轮 Composite Registry
    ↓                         ↓
总控发现并分配 Agent       专业 Agent 按 permissions.yaml 调用 Skill
```

权限只以 Agent revision 中的 `permissions.yaml` 为事实来源。运行期为 Skill 派生的
`allowed_agents` 只存在于内存快照，绝不回写成第二份数据库权限。自定义 Agent 使用现有
通用专业 Agent Runtime、同样的 Context View、Artifact 和 Human Review 边界；不能创建
正式子任务、修改 TaskSpec 或调用本体/图谱发布 Skill。用户不选择 Skill 类型：未上传 `.py`
时使用 SKILL.md 调用模型；一个 `.py` 自动作为入口；多个 `.py` 必须包含 `main.py`。脚本只在
无网络、无业务凭证且具备资源限制的独立 Sandbox Runner 中运行，不能在 API 或 Worker 进程
导入或执行；其 JSON 输出仍须通过 Skill Registry 与 Artifact Registry 校验。

脚本 Skill 的发布增加确定性门禁：服务先静态扫描 import 与可选 `requirements.txt`，只接受
固定 Runner 运行环境已经预装的批准依赖；随后按用户确认的 Contract Profile 生成非空标准
Artifact 输入，在同一无网络 Runner 中执行一次契约试运行。Runner 响应必须同时通过脚本响应
协议、精确 Artifact 类型集合和 Artifact Pydantic Schema。PostgreSQL 检查记录绑定不可变
revision、资源包 checksum、Profile、运行环境和协议版本；绑定事实变化即须重新检查。任务运行时
仍使用同一 Runner 和相同协议。新脚本可只实现 `run(inputs, parameters)`，由 Runner 内的平台
固定入口负责标准输入输出；未定义该函数的历史脚本继续兼容原 JSON 标准输入输出协议。不新增
Agent、编排层或宿主机脚本回退。

## 15. 会话导航、实时刷新与清理边界

`Conversation` 是用户可见的聊天容器，包含多轮公开消息和多个 MasterTask 的引用；每个
MasterTask 仍独立维护 TaskSpec、SharedContext、AgentSubTask、审核和 Artifact。总控读取当前
会话最近五轮公开用户—Agent 对话，按当前任务边界判断 START / CONTINUE / MODIFY。即使一轮
尚未生成 MasterTask 而请求澄清，公开消息也归属该会话，刷新后仍可恢复。

正式工作台左栏按会话而不是 MasterTask 展示；右键或 `⋯` 提供重命名和删除。会话内任务
选择器允许查看历史任务或在当前聊天中开启新任务。会话 SSE 根据数据库全局 TaskEvent 序号
订阅该会话所属全部任务，消息正文仍从 PostgreSQL 读取，不经 SSE 携带模型原始输出。

删除会话清理私有执行事实与普通文件、日志。正式发布的 PublishedOntology / PublishedGraph
和图谱 PostgreSQL 投影解除来源任务外键后继续位于资产目录；公开发布内容不因来源聊天删除
而变化。已合并或被其他任务复用的长期经验保留，只有拥有精确 Mem0 memory ID、且未被
合并/复用的独立经验可删除。跨系统清理失败会显式返回告警，不伪装为完整清理成功。

## 16. 自定义脚本的字段映射与上下游版本绑定

自定义脚本继续接收正式 Artifact 列表；可选的已确认字段映射将脚本别名指向
`Artifact 类型 + content JSON Pointer`。映射保存在不可变 Skill 修订中，发布前
沙箱试运行和真实运行使用同一确定性解析器，解析值出现在
`run(inputs, parameters)` 的 `parameters["input_fields"]`。代码只能验证字段存在与
路径匹配，不能从不同名称自行判断业务语义等价；缺失或多义时显式失败。

跨 Agent 解析输入时，同类新 Artifact 若沿父级血缘衍生自旧 Artifact，下游默认
使用新版本；用户显式指定的旧 ID 不被静默替换。无血缘关系的同类输入保留，
不根据时间或数组顺序擅自选择。正式输出仍经已有响应协议、Pydantic Schema、
Artifact ID/checksum 和 AgentRunResult 血缘传递。

创建弹窗和 Skill 管理页已提供模型建议与人工确认；模型只从代码列举的字段候选中建议，
不直接保存。脚本输出必须成为已注册的标准 Artifact 内容；未映射的普通 JSON 不生成
正式产物，也不另存独立附加对象。下游仅根据 Artifact ID、类型和血缘读取标准内容。
异类型单输出若需要普通 JSON，可通过受控接口人工确认原始字段到目标 Artifact
顶层字段的映射；试运行与实际执行复用相同规则，目标 Schema 仍严格校验。
按 ADR-090，新建实体转换脚本优先使用用户确认的逐项函数适配：平台逐项传入实体名称、保留 ID/类型/证据、生成新 EntityDataset。通用输出字段映射接口仍可将原始字段写入标准产物，但其业务弹窗不再出现在默认创建流程。
TODO：复杂嵌套、多产物输出映射及上游未抽取字段的定向补抽取尚未完成。

## 17. 已有 Python 脚本创建 Skill 的入口边界

用户原有脚本与 Skill 定义包一起保存。若它提供标准 `run(inputs, parameters)`，
可直接作为入口；实体转换 Profile 下的已确认单参数业务函数，由服务端确定性生成
`_kg_skill_entry.py` 作为独立入口。原 `main.py`、`main()` 和辅助文件均不覆盖；
平台只允许用户确认函数及业务含义，不允许上传文件占用保留入口名。
修订中显式记录生成入口，并把它及原脚本一起纳入 checksum；发布前隔离试运行和
正式运行共用同一个入口与标准 `outputs` 协议。生成的新 EntityDataset 仍由现有
ArtifactService 按父 ID 持久化，并由下游 Agent 沿 AgentSubTask 依赖读取。
按 ADR-093，其他 Profile 的顶层普通函数可先静态列出名称和参数，再由用户确认
每个参数取自整份输入、标准 Artifact 列表、唯一上游 Artifact 的完整 content
或已注册的内容字段。平台据此生成同一
标准入口供发布前和正式运行使用；不在运行时猜测脚本语义或放宽输出契约。
CLI 型零参数 `main()` 不被自动当作业务函数。

按 ADR-094，用户确认的上下游 Contract Profile 先确定目标 Artifact；脚本若返回
完整单产物内容且通过注册 Schema，平台可补标准响应外壳。普通 JSON 部分结果
只展示有界字段名并引导人工确认输出映射，生成新 Skill 修订后重新试运行；
无法确认或校验失败时不发布。实体同型自动封装必须保留上游 ID、类型和证据。
试运行与正式执行共用同一确定性归一化逻辑，不新增自由格式 Artifact。
实体同型转换在标准输出、自动封装和人工字段映射三条路径均核对实体 ID、
类型与证据，避免错误输入导致空实体列表被误判为成功。


按 ADR-095，函数参数来源与用户脚本在该参数上实际读取的列表字段路径由同一
创建／编辑界面呈现并一次确认。静态分析只读取字面量键，来源候选只列举注册
Artifact Schema；唯一同层别名可以预填建议，歧义需人工选择。确认的字段绑定
与生成入口同属不可变 Skill 修订，隔离试运行与正式执行均在输入副本上应用；
这不改变输出协议、实体身份和证据校验，也不代表支持任意脚本自动兼容。
按 ADR-099，静态分析覆盖普通循环和列表／字典推导式，并区分方括号必需读取
与带默认值的可选读取；通用字段角色别名只作候选，保存前由后端复核每个必需
读取路径。未找到来源时明确报告缺失，不把空映射送入沙箱。已有函数若直接返回
记录列表，生成入口只补可观察外壳，输出仍要完成字段映射和标准契约校验。

按 ADR-096，脚本 Skill 试运行只暴露有界的输出字段形状；输出映射先由代码
精确匹配，剩余字段由受控模型提出候选，再由代码白名单核对、正式端用户确认。
沙箱与正式 Worker 共用确定性转换器，将结果校验为下游既有 Artifact。实体
同型转换保留 ID、类别和证据，未定义关系重定向的多实体合并不得进入下游。

按 ADR-097，实体对齐 Skill 的模型生成契约比历史持久化读取契约更严格：输出必须
显式包含 entities。运行时在 SkillRunResult 进入 ArtifactService 前，对照权威
EntityDataset 输入检查实体数量、身份、类型、证据及统计。失败只触发既有的一次
模型结构化修复，不能将空的 EntityDataset 作为成功产物传给下游 Agent。

按 ADR-098，属性转换脚本的发布前样本必须含真实形状的非空 PropertyDataset；正式执行和试运行均核对属性 ID、所属实体、类型和证据。用户确认后可在唯一完整输入的副本上应用单层列表及记录字段别名，例如 properties 到 attributes；来源只取注册 Schema，目标只取脚本静态读取路径，不借映射推断缺失单位。普通 JSON 属性输出需按已确认 property_id 回填上游身份与证据，再经标准契约校验。完整用户脚本不得在未获单独授权时外发模型分析。

