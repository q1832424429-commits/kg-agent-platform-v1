# 跨领域自适应知识图谱构建 Agent 平台 V1

## 1. 项目背景

本项目面向企业知识图谱构建场景。用户可以通过多轮自然语言交互和文档上传描述目标，系统根据用户行业、已有本体、外部本体、输入 Artifact 和当前任务状态，自适应地拆解知识图谱构建任务，并把子任务分配给不同专业 Agent。

项目当前以制造业作为首个参考行业，但架构不能把制造业实体类型或流程硬编码到 Agent 核心逻辑中。领域差异应通过本体、Prompt、Agent/Skill 能力声明和配置表达。

项目不再基于 RAG-Anything，也不建设知识查询或 GraphRAG 问答链路。V1 聚焦知识图谱“构建”过程及 Agent 协作准确率。

## 2. V1 目标

V1 的核心目标不是一次性实现所有文档解析和图谱算法，而是建立一个可运行、可测试、可扩展的后端骨架，验证：

1. 总控 Agent 能正确识别用户是在新建、继续还是修改任务。
2. 总控 Agent 能结合 SharedContext 和已有 Artifact 生成或修订 TaskSpec。
3. 总控 Agent 能把 MasterTask 拆成合理的 AgentSubTask，并指定正确的专业 Agent。
4. 专业 Agent 能理解所认领子任务，选择自己允许使用的 Skill，并返回标准 AgentRunResult。
5. 用户继续或修改任务时，系统能够复用仍然有效的既有 Artifact。
6. 本体确认、候选图谱预览修改和最终发布形成完整闭环。
7. Agent 的决策、任务状态、Artifact 血缘、人工审核和用户反馈可追溯、可评测。
8. 新增 Agent、Skill 或 Artifact 类型时，不需要重写总控 Agent 核心流程。

## 3. 用户核心流程

```text
用户输入自然语言并可上传文档、本体或结构化数据
    ↓
加载当前会话、任务状态、长短期记忆和已有 Artifact
    ↓
总控 Agent 判断 START / CONTINUE / MODIFY
    ↓
自动创建或修订 TaskSpec
    ↓
检查已有 Artifact，确定可复用结果
    ↓
总控 Agent 一次性拆解完整 AgentSubTask 计划并指定专业 Agent
    ↓
确定性校验通过后发布到 SharedContext
    ↓
无依赖专业任务并行认领；有依赖任务等待前置 Artifact 后再认领
    ↓
写入 Artifact、AgentRunResult 和执行轨迹
    ↓
确定性服务按依赖解锁任务，并在必要位置进入人工审核
    ↓
用户审核并确认本体
    ↓
基于已发布本体完成实例抽取、融合和质量评估
    ↓
向用户展示可编辑的候选图谱及证据
    ↓
用户确认后由图谱管理更新 Agent 经确定性发布门禁正式发布
    ↓
全部计划终止后由总控 Agent 检查最终目标；完成后沉淀轨迹、反馈和经验
```

TaskSpec 由系统自动生成，不单独要求用户确认。仅在缺少完成任务所必需的信息且无法安全推断时，系统进入 `NEEDS_CLARIFICATION` 控制状态并向用户提问。

## 4. V1 范围

### 4.1 Agent 能力

- 一个真实总控 Agent：意图识别、任务拆解与任务分配。
- 五个真实内置专业 Agent：数据源解析、知识抽取、知识融合、质量评估、图谱管理更新；用户可在受控扩展机制下创建额外的专业 Agent 和 Skill。
- Agent Registry 和 Skill Registry。
- 专业 Agent 的 Skill allowlist 和权限边界。
- 基于 Pydantic 的结构化 LLM 输入输出及失败修复。
- Agent 运行轨迹、Prompt 版本、模型调用指标和错误记录。
- Skill 原始结构化回答受控留档，并可追踪到持久化后的真实 Artifact ID；下游按 Artifact ID
  读取并再次校验内容契约。

### 4.2 任务与上下文

- `START`、`CONTINUE`、`MODIFY` 三类任务意图。
- MasterTask、AgentSubTask、AgentAssignment 分层建模。
- TaskSpec 和任务拆解的版本化修订。
- 一个 MasterTask 一份逻辑 SharedContext。
- 一个用户会话可包含多个 MasterTask；会话保存完整公开对话，并作为左栏导航、实时事件与私有数据删除边界。
- 面向不同 Agent 的过滤 Context View。
- Assignment 认领、租约、心跳、重试、取消和完成状态。
- 不冲突子任务的并行能力，发布和冲突写入的串行保护。

### 4.3 Artifact 与知识图谱构建闭环

- 上传原始文档和标准 JSON 外部本体。
- V1 契约覆盖 PDF（含文字、图片、表格、扫描内容）、DOCX、XLSX、PPTX 和常见图片格式。
- 业务处理由 LLM Skill 或已明确注册的正式确定性 Skill 产生经过同一 Schema 校验的 Artifact；系统不提供 Mock Skill 或模拟输入。
- Artifact 版本、状态、内容引用、校验和及血缘。
- 已有 Artifact 查询、兼容性判断和复用。
- 复用已发布本体进行实例抽取。
- 没有本体时构建候选本体。
- 外部本体与平台本体的候选对齐和融合；候选本体直接返回给用户预览、修改和确认。
- 本体人工编辑、确认和正式发布；发布版本可在后续任务中选择复用。
- 实体、关系、属性、证据和冲突等候选结果。
- 候选图谱人工预览、交互式修改、确认和正式发布；工作台支持拖拽、缩放、布局、筛选、
  检索、属性/证据查看以及 ChangeSet 草稿编辑。
- 用户修改以结构化 ChangeSet 表达。

### 4.4 记忆、评测与自进化基础

- 会话短期记忆：最近五轮完整公开对话，即五条用户消息和五条 Agent 回复。
- TaskSpec、任务决策、Artifact 和审核记录等任务记忆。
- 用户授权后的行业偏好、术语映射、本体选择、抽取偏好和成功经验等长期记忆。
- Mem0 Cloud 长期记忆写入和语义召回；召回经过候选扩展、阈值、重排、失效过滤与去重后，
  最多将三条摘要注入总控及专业 Agent 的裁剪 Context View。
- 长期经验只在整个 MasterTask 成功后聚合当前目标、成功 Agent/Skill 路径和最终 Artifact 写入一次；
  单个 Agent 结果不独立写入。相似度不低于 0.92 时跳过，0.80 到 0.92 之间合并，低于 0.80 新增。
- Golden Dataset 和离线评测工具。
- 执行轨迹、用户反馈和成功经验沉淀。
- 仅生成版本化配置、Prompt 或策略优化建议，不自动修改或发布生产配置。

### 4.5 后端和部署

- FastAPI 后端接口。
- 文件上传、对话、任务状态、Artifact、审核和事件接口。
- Linux Docker Compose 部署。
- PostgreSQL 持久化。
- MinIO 文档和大型 Artifact 存储。
- 不使用 Docker 的单机联调模式可显式启用 SQLite 与本地受控 Artifact 目录；正式部署仍使用
  PostgreSQL/MinIO，测试端数据目录与正式端隔离。
- Mem0 Cloud 长期记忆正文存储；PostgreSQL 只保留短期记忆和 Mem0 引用。
- 单用户、单工作空间、无登录。
- 中文项目文档和正常企业项目级中文注释。
- 自定义 Agent / Skill 草稿、编辑、试运行、发布、停用、删除和内部修订；编辑已发布定义时始终
  先创建草稿修订，当前发布版本保持可用，用户确认发布后才原子替换；停用定义也可修订后重新发布，删除仅适用于已停用定义，
  不影响历史任务日志和 Artifact。前端不展示修订号。面向业务用户的
  创建入口只收集能力用途、适用资料、预期结果、职责和授权 Skill，不暴露内部 Artifact 或子任务
  类型；服务端将其编译为已注册契约。自定义 Skill 不要求
  用户选择实现类型：未上传 Python 文件时使用 SKILL.md 调用模型，上传脚本时由后端自动识别
  入口并通过无网络、无业务凭证的隔离 Runner 执行。用户输入名称作为定义的业务显示名称；随机
  内部 key 仅用于权限、路由、调用与血缘，不在业务管理界面展示。脚本发布前会自动扫描依赖，
  再按用户确认的使用方式使用标准样例进行沙箱试运行；只有输出通过 Artifact Pydantic 契约后
  才能发布。已有标准 `run(inputs, parameters)` 脚本直接运行；实体转换类已确认的单参数业务函数
  由平台另存适配入口，不改动用户原脚本，试运行与正式执行使用同一入口。
  V1 只使用固定批准依赖环境，不在任务执行时联网安装依赖。

## 5. 非 V1 范围

- 用户知识查询、GraphRAG 检索和问答。
- RAG-Anything 集成。
- 真实生产级 PDF/OCR/表格/图片理解算法。
- 真实实体抽取、关系抽取、本体归纳、融合和质量算法。
- Neo4j、Apache AGE 或其他图原生数据库发布；V1 使用现有 PostgreSQL 节点—边关系表
  持久化审核后的 PublishedGraph。
- 向量数据库和 Embedding/Reranker 检索链路。
- DWG 解析。
- 完整业务运营前端、多人协同图编辑和生产级图数据库可视化；V1 仅提供候选图谱审核所需的
  轻量交互工作台，业务修改仍以 ChangeSet 和后端审核流程为准。
- 登录、用户中心、多租户、细粒度权限系统。
- 数百用户并发和生产级水平扩展。
- 计费、运营后台、商业化包装和统计大屏。
- iDME、OP、造物平台、MCP 等外部系统集成。
- Agent 自动修改自身生产 Prompt、代码、Skill 或路由策略。

## 6. 核心业务能力

### 6.1 意图识别与任务修订

总控 Agent 根据当前消息、会话历史和 SharedContext 判断：

- `START`：创建新的 MasterTask。
- `CONTINUE`：在当前任务目标下继续后续工作，优先复用已有 Artifact。
- `MODIFY`：修改已有任务目标、输入、本体选择或约束，生成新 revision，并判断旧 Artifact 是否仍可复用。

### 6.2 自适应任务拆解与分配

总控 Agent 不生成 Skill DAG，而是生成 Agent 粒度的 AgentSubTask。每个子任务包含负责的 Agent 类型、依赖、输入 Artifact、预期输出、完成条件、质量要求和人工审核要求。

START 时总控一次性生成当前最终目标所需的完整 AgentSubTask 计划。计划是动态的，不是固定流水线；没有依赖的任务可以并行，有依赖的任务在 SharedContext 中保持阻塞，直到确定性服务确认全部前置任务成功。全部计划进入终态后，总控再检查最终目标是否已经完成；只有失败返工、目标缺失或用户 MODIFY 时才产生新的拆解 revision。

### 6.3 本体建设与复用

平台支持三种本体来源：

1. 用户指定平台中已发布的本体及版本。
2. 用户上传标准 JSON 外部本体，经必要的对齐、融合和用户确认后发布。
3. 没有可用本体时，根据行业、文档和任务目标生成候选本体，供用户预览、修改并确认后发布。

实例知识抽取必须基于已确认发布的本体版本。实体、关系、属性和统一证据由同一个
`extract_knowledge` Skill 在一次调用中联合生成，避免拆分调用造成上下文重复、空实体级联和
跨产物引用不一致。模型只返回实体、关系、属性和证据四个必填数组，不生成 Artifact 外壳、
摘要或计数；结构化结果在保存前必须通过跨产物语义完整性校验，再由代码拆分为四类 Artifact。

### 6.4 候选图谱审核与发布

知识抽取、融合和质量评估完成后产生 CandidateGraph。用户能够查看证据，并通过 ChangeSet
表达实体、关系和属性的增删改以及实体合并、拆分。只有当前版本的 GraphApproval 存在时
才能发布。发布后的完整 JSON 作为不可变 PublishedGraph Artifact 保存，同时把图版本、节点、
边、属性和证据原子写入 PostgreSQL 关系表，供后续 SQL 查询。

用户批准候选本体或候选图谱时可以填写发布名称。名称随 Approval 与发布 Artifact 不可变保存，
并在“已发布资产”页面用于浏览、复用和管理；任务中为复用而创建的受控导入副本不属于新发布资产。

### 6.5 Artifact 复用

任务继续或修改时，系统根据输入校验和、TaskSpec 影响、本体版本、父级血缘、状态和 Schema 兼容性判断已有 Artifact 是否可复用，避免重复执行已经完成且仍然有效的工作。

### 6.6 质量、溯源和反馈

所有候选本体、实体、关系和图谱结果均保留来源 Artifact 和证据引用。质量评估结果可以触发返工；人工审核意见、执行结果和用户反馈进入可评测的经验数据，但 V1 不自动改变生产策略。

## 7. 主要术语

| 术语 | 定义 |
|---|---|
| MasterTask | 用户当前要完成的总目标，是任务生命周期和 SharedContext 的边界。 |
| Conversation | 用户可见的聊天会话，容纳多轮消息和多个 MasterTask，不替代任务级 SharedContext。 |
| TaskSpec | 系统对用户目标、领域、输入、目标输出、本体策略、约束和复用策略的结构化描述。 |
| Task Intent | 顶层任务行为，只能是 `START`、`CONTINUE`、`MODIFY`。 |
| AgentSubTask | 总控 Agent 从 MasterTask 中拆出的 Agent 粒度工作单元，不是 Skill DAG 节点。 |
| AgentAssignment | AgentSubTask 的一次具体执行分配和认领记录。 |
| 总控 Agent | 负责意图识别、TaskSpec、子任务拆解、Agent 分配和后续协调的真实 LLM Agent。 |
| 专业 Agent | 负责特定专业环节、能够自主选择其允许 Skill 的真实 LLM Agent。 |
| Skill | Agent 可调用的统一能力包；无 Python 资源时正文作为提示词，有 Python 资源时由隔离 Runner 执行，内置专用代码按包加载。 |
| Skill Registry | 保存 Skill 版本、Owner、权限、输入输出契约和运行配置的注册中心。 |
| SharedContext | 一个 MasterTask 内共享的逻辑上下文和事实来源，由确定性服务管理。 |
| Context View | SharedContext 针对某个 Agent 和子任务裁剪后的最小上下文。 |
| Artifact | Agent/Skill 产生并通过标准契约持久化的业务产物。 |
| ArtifactRef | SharedContext 中指向 Artifact 元数据和内容地址的轻量引用。 |
| Artifact Lineage | Artifact 与输入、父 Artifact、生产者、TaskSpec 和本体版本之间的血缘。 |
| CandidateOntology | 尚未经过用户确认和发布的候选本体。 |
| PublishedOntology | 已经用户确认、具有固定版本、可用于实例抽取的本体。 |
| CandidateGraph | 完成抽取和融合但尚未最终确认发布的候选实例图谱。 |
| ChangeSet | 用户或系统对候选本体/图谱提出的结构化增删改、合并和拆分操作集合。 |
| Human Review | 用户对候选本体或候选图谱进行查看、修改、批准或拒绝的确定性门禁。 |
| AgentRunResult | 专业 Agent 一次运行的结构化结果、输出 Artifact、质量信号和后续建议。 |
| ExecutionTrace | 一次任务、Agent、模型或 Skill 运行的可审计事件和指标记录。 |
| Experience | 从成功或失败执行轨迹、用户反馈和审核结果中形成的结构化经验。 |
| NEEDS_CLARIFICATION | 缺少必要信息时的控制结果，不是新的 Task Intent。 |

## 8. V1 初始验收指标

默认验收目标如下：

| 指标 | 目标 |
|---|---:|
| `START/CONTINUE/MODIFY` Macro-F1 | ≥ 95% |
| 专业 Agent 分配准确率 | ≥ 95% |
| 必要子任务召回率 | ≥ 95% |
| 不必要子任务率 | ≤ 5% |
| Artifact 复用判断准确率 | ≥ 95% |
| 本体审核节点遗漏率 | 0 |
| 图谱审核节点遗漏率 | 0 |
| 未经审核直接发布次数 | 0 |
| 端到端任务路由成功率 | ≥ 90% |

评测必须同时包含正常、新建、继续、修改、歧义、Artifact 复用、等待审核、质量返工、失败重试、取消和越权发布等案例。

## 9. 当前仍需在编码过程中显式保留的 TODO

- TODO：确定生产环境实际使用的 DeepSeek 模型标识；代码必须允许环境变量配置，不能写死。
- Artifact 开发默认限制已冻结为单个 50 MiB、每任务 20 个 RawDocument、任务总量 500 MiB，部署时可通过环境变量调整。
- TODO：在获得真实业务对话后补充和冻结 Golden Dataset；初始仓库案例仅作为开发基线。
- 长期记忆写入授权已通过 `authorized=true` 或任务消息的 `remember_long_term=true` 表达。
- TODO：确定长期记忆撤销、删除和数据导出的 API 契约。
- MasterTask、AgentAssignment、Artifact 和 HumanReviewRequest 状态枚举已在第二阶段冻结，后续新增状态必须先更新契约和 ADR。
- 五个专业 Agent 的 V1 能力矩阵和 16 个 Skill 已在版本化 Registry 配置中冻结；新增能力必须同步契约、配置与测试。
- 22 类 V1 Artifact 内容体已冻结为 Schema 1.0.0；后续不兼容修改必须新增 Schema 版本。
- 正式工作台和 8002 验证实验室均使用 SSE 实时展示任务进度；SSE 只是公开状态的
  传输方式，不改变 PostgreSQL 任务事实、Agent 协作或 Artifact 契约。
