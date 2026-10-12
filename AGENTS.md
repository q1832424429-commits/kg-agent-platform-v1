# AGENTS.md

## 1. 文件作用与适用范围

本文件定义 `kg-agent-platform-v1` 仓库的项目级开发规则，适用于仓库根目录及全部子目录。任何开发者或编码 Agent 在读取、修改、测试本项目代码前，都必须先阅读本文件以及 `PROJECT.md`、`docs/architecture.md`、`docs/contracts.md`、`docs/decisions.md` 和 `docs/current_task.md`。

发生约束冲突时，按以下优先级处理：

1. 用户当前明确指令。
2. 本文件中的项目级规则。
3. `docs/decisions.md` 中状态为“已接受”的 ADR。
4. `docs/architecture.md` 和 `docs/contracts.md`。
5. 其他说明文档和代码注释。

不得仅依赖聊天历史推断项目架构。需要改变已接受决策时，必须先更新 ADR 并获得用户确认。

## 2. 项目目标约束

本项目的业务方向是知识图谱构建。V1 的首要工程目标是验证以下 Agent 能力：

- 总控 Agent 对 `START`、`CONTINUE`、`MODIFY` 的意图识别准确率。
- 总控 Agent 将 MasterTask 拆解成 AgentSubTask 的准确性。
- 总控 Agent 为 AgentSubTask 分配正确专业 Agent 的准确性。
- 专业 Agent 对子任务、输入 Artifact 和可用 Skill 的理解与选择准确性。
- 多轮任务中对已有 Artifact 的正确复用。
- 本体审核和候选图谱审核不可绕过。
- 总控 Agent 必须严格遵守用户当前目标边界：抽取不等于构图，构图不等于发布或入库。
- 总控 Agent 必须根据当前最终目标一次性动态生成完整的 Agent 级子任务计划；未来产物通过子任务依赖声明，不能伪造 Artifact ID。
- 无依赖任务允许并行认领，有依赖任务必须等待全部前置任务成功；全部计划进入终态后由总控 Agent 检查最终目标是否完成。

V1 的 Agent 必须是真实 LLM Agent。Skill 是统一能力包，不划分 LLM/脚本模式；没有 Python 资源的 Skill 由专业 Agent 加载正文作为提示词，有 Python 资源的 Skill 进入隔离 Runner，内置专用实现按目录代码执行。

## 3. 不可破坏的架构约束

### 3.1 Agent 架构

- 系统只有一个总控 Agent：意图识别、任务拆解与任务分配 Agent。
- 系统内置五个专业 Agent：数据源解析 Agent、知识抽取 Agent、知识融合 Agent、质量评估 Agent、图谱管理更新 Agent；在 ADR-072 约束下，用户可发布受控的自定义专业 Agent。自定义 Agent 不改变总控 Agent 的唯一性，也不新增 Workflow、Planner 或执行 Agent。
- 总控 Agent 和所有专业 Agent 都是真实 LLM Agent，不得用规则函数、固定返回值或伪代码代替 Agent 决策。
- 总控 Agent 负责理解用户目标、创建或修订 TaskSpec、检查已有 Artifact、一次性拆解完整 AgentSubTask 计划、指定负责 Agent、发布子任务，并在全部计划终止后检查最终目标。
- 专业 Agent 负责认领已经分配给自己类型的 AgentAssignment，读取过滤后的 Context View，选择自己有权限使用的 Skill，执行子任务并提交 AgentRunResult。
- 只有总控 Agent 可以创建并发布正式 AgentSubTask。专业 Agent 只能在 AgentRunResult 中提出 `proposed_subtasks`，不能直接发布新子任务。
- 不得新增 Workflow 编排 Agent、Planner Agent 或执行 Agent。
- 不得把专业 Agent 之间的任务关系重新实现成动态 Skill DAG、子 DAG 或由文档清单生成的子 DAG。
- LangGraph 只用于 Agent 自身的小型、固定运行状态机，以及暂停、恢复、持久化和人工中断；不得把它变成已经否决的全局业务流程编排器。

### 3.2 Task 与 Assignment

- 顶层任务意图只有 `START`、`CONTINUE`、`MODIFY`。
- `NEEDS_CLARIFICATION` 只能表示控制状态或 Agent 结果，不能作为第四种业务任务意图。
- TaskSpec 自动生成和修订，不设置独立的 TaskSpec 人工确认节点。
- 用户修改任务时创建新的 TaskSpec revision 和 decomposition revision，不覆盖历史版本。
- MasterTask、AgentSubTask 和 AgentAssignment 必须分离建模。
- 一个 MasterTask 可以包含多个 AgentSubTask；一个 AgentSubTask 可以有多个执行尝试，但同一时刻只能有一个有效认领。
- 锁定粒度是 AgentAssignment，不是整个 MasterTask。输入输出互不冲突的子任务允许并行。
- 完整计划发布后，仅无依赖或依赖已经成功的子任务创建可认领 Assignment；其他子任务保持 BLOCKED，并由确定性服务解锁。
- 认领必须预留 lease、heartbeat 和 fencing token 机制，防止重复执行和过期 Worker 回写。
- 子任务依赖、状态转换、认领、重试和完成必须由确定性领域服务校验，不能只依赖 LLM 自觉遵守。

### 3.3 SharedContext

- 每个 MasterTask 拥有一个逻辑独立的 SharedTaskContext；不得建立所有任务共用且可任意覆盖的全局大 JSON。
- SharedContext 是所有 Agent 共享的逻辑事实来源，但每次模型调用只得到按角色裁剪的 Context View。
- SharedContext 保存状态、摘要、决策、短期记忆和长期记忆引用，不保存 Mem0 长期记忆正文、完整 PDF、完整图谱或大型数据集。
- Agent 不得直接覆盖 SharedContext。所有写入必须通过 ContextService、TaskStateService、AssignmentService、ArtifactService、MemoryService 或 HumanReviewService 等确定性服务。
- SharedContext 采用结构化状态加追加事件的方式维护，关键变更必须可审计和可追溯。
- 不得保存或暴露模型隐藏思维过程。只保存结构化结论、决策代码、理由摘要、证据引用和执行指标。

### 3.4 Skill 与 Skill Registry

- Skill 不存在用户可选或任务级的 `LLM/SCRIPT/REAL` 模式。Registry 每个 Skill 只绑定一个当前执行器：有 Python 资源时进入隔离 Runner；无 Python 资源时加载 `SKILL.md` 正文作为专业 Agent 提示词；内置 `implementation.py` 表示该 Skill 已有专用代码。
- 8002 验证实验室与正式端均禁止提供模拟输入、默认模拟本体或 Mock 执行模式。
- 切换实现不得改变 Skill 名称、Owner、allowlist、Artifact 输入输出契约、PublishGuard 或 Human Review 门禁。
- 内置业务 Skill 必须声明名称、版本、Owner Agent、允许调用的 Agent、输入 Artifact 类型、输出 Artifact 类型、副作用、超时、重试策略、实现状态和生命周期状态。自定义 Skill 的调用权限以所属自定义 Agent 修订中不可变的 `permissions.yaml` 为唯一事实来源；其元数据不持久化第二份 Agent allowlist。
- 专业 Agent 只能调用其 allowlist 中的业务 Skill；自定义 Agent 的 allowlist 必须由 `permissions.yaml` 解析，不能由模型或 Skill 正文绕过。
- 自定义 Agent / Skill 的物理删除只允许在其状态已为 `DISABLED` 时进行；删除定义包不得级联删除历史 MasterTask、Assignment、ExecutionTrace、日志或 Artifact，删除 Skill 前必须确认没有活动自定义 Agent 的权限 YAML 正在引用它。
- 共享基础设施 Skill 可以被多个 Agent 使用，但必须通过统一服务接口访问上下文、Artifact、记忆、追踪和模型。
- Skill 的输入输出必须使用 Pydantic 契约；不得在 Agent 之间传递无版本、无类型的自由 JSON。
- 将提示词执行流程替换为专用算法时，应保持 Skill 公开输入输出契约兼容，避免修改 Agent 主流程。
- 新增 Agent 或 Skill 必须通过 Registry 注册和启动期校验，不得把名称硬编码到总控 Agent 的分支逻辑中。
- 面向非开发者的自定义能力界面不得要求填写 Artifact 类型、子任务类型、内部标识、YAML 或
  运行实现。自定义 Skill 的业务用途由模型从代码注册的 Contract Profile 中推荐并由用户确认，
  Artifact 契约、集成模式和兼容性必须由代码决定和校验；自定义 Agent 的能力继续从已选 Skill
  确定性推导。
- 不兼容计划可以作为待修正草稿和校验结果展示，但不得发布成 SharedContext 中可领取执行的
  AgentSubTask。终端或旁路能力不要求下游消费者，但其输出必须属于用户目标且可正式持久化。

### 3.5 Artifact

- Agent 之间通过标准 Artifact 交换业务数据，不能把非结构化聊天内容当作正式业务交付物。
- 总控不得为尚未产生的 Artifact 编造 ID；Worker 在依赖成功后根据祖先 AgentRunResult 血缘解析真实输出，并构造本次执行的只读输入视图。
- Artifact 必须具有全局 ID、类型、Schema 版本、生产者、父 Artifact、TaskSpec revision、内容地址、校验和、状态和创建时间。
- 大型 Artifact 内容保存在 MinIO；PostgreSQL 保存元数据、摘要、状态和血缘引用。
- Artifact 默认不可变。修改应产生新 Artifact 或 ChangeSet，并保留血缘。
- `CONTINUE` 和 `MODIFY` 必须优先检查可复用 Artifact。复用判断由确定性 ArtifactReuseService 执行，LLM 可以提出建议但不能绕过兼容性校验。
- 复用至少检查输入校验和、TaskSpec revision 影响、本体版本、父级血缘、Artifact 状态和 Schema 版本兼容性。

### 3.6 本体与图谱审核

- 实例知识抽取必须依赖已经由用户审核并发布的本体版本。
- 若用户指定已有已发布本体，应记录并确认本任务使用的本体及版本。
- 若上传外部本体，V1 按标准 JSON 契约处理，并在发布前完成必要的对齐、融合和用户审核。
- 若不存在本体，应从文档和行业上下文构建候选本体，并在用户审核通过后发布。
- 候选本体不经过自动质量评估，必须直接供用户预览、修改和确认；候选图谱质量评估仍保留。
- 本体审核和候选图谱审核是 Human Review Gate，不是 Agent。
- 候选图谱必须支持以 ChangeSet 表达实体、关系、属性的增删改，以及实体合并、拆分和证据查看。
- 只有图谱管理更新 Agent 可以调用发布类业务 Skill。
- 本体或图谱发布必须经过确定性 PublishGuard 校验对应 Approval，禁止任何 Agent 绕过审核直接发布。

## 4. V1 技术边界

已确认的基础技术栈：

- Python 3.12。
- FastAPI 提供后端 API。
- LangGraph 实现每个 Agent 的小型运行状态机。
- Pydantic v2 定义数据契约和校验 LLM 结构化输出。
- DeepSeek 通过可替换的 ModelGateway 接入，具体模型名由环境变量配置。
- SQLAlchemy 2 和 Alembic 管理 PostgreSQL 持久化及迁移。
- PostgreSQL 保存任务、上下文、分配、短期记忆、Mem0 记忆引用、审核、Artifact 元数据、
  执行轨迹，以及 PublishedGraph 的版本化节点—边关系投影。
- Mem0 Cloud 是长期记忆正文的唯一 V1 存储与语义召回服务；未配置时必须显式禁用，不得把长期记忆正文静默降级到 PostgreSQL。
- Mem0 API Key 只能存在于服务端环境变量。总控和专业 Agent 只能通过 MemoryService 获取裁剪后的记忆摘要与引用；适配器按 Mem0 Platform 当前公开契约使用 V3 新增/搜索和 V1 按 ID 更新端点。
- 8002 验证实验室可以复用同一 Mem0 API Key，但必须使用独立的 `app_id` 与 `user_id`；测试端和正式端的搜索都必须同时过滤这两个维度，禁止跨环境召回。
- 验证实验室只在完整任务成功后写入一条聚合公开经验，不上传完整会话历史、文档正文、单个专业 Agent 碎片经验或隐藏推理。
- MinIO 保存上传文档和大型 Artifact 内容。
- 用户明确要求纯本机 uv 联调时，ADR-074 允许正式端与测试端分别使用隔离 SQLite 文件和
  受控本地 Artifact 目录；该例外不得进入正式部署，也不得让用户脚本在宿主机执行。
- Docker Compose 用于 Linux 环境部署。
- pytest 用于单元、集成、Agent、API、评测和端到端测试。
- uv 管理 Python 依赖和锁文件；Ruff、mypy 负责代码质量检查。

V1 不得擅自引入：

- RAG-Anything。
- GraphRAG 查询链路或用户知识查询功能。
- Neo4j、Apache AGE 或其他外部/扩展图数据库；V1 只允许把审核发布后的 PublishedGraph
  确定性投影到现有 PostgreSQL 关系表，不得把候选图谱或未审核结果写入发布表。
- 向量数据库。
- Redis、Celery、Temporal 等额外任务运行时。
- AutoGen、CrewAI 或其他第二套 Agent 编排框架。
- 登录、权限中心、多租户和运营管理界面。
- iDME、OP、造物平台、MCP 等外部集成。
- DWG 解析。

如确需引入上述组件，必须先新增 ADR 并获得用户确认。

## 5. 代码组织与依赖规则

- 采用 `api -> services -> domain/repositories -> infrastructure` 的依赖方向。
- `domain` 不得依赖 FastAPI、SQLAlchemy ORM 实现、MinIO SDK 或特定模型 SDK。
- Agent 通过服务和工具协议访问外部能力，不直接操作数据库 Session 或对象存储客户端。
- API 路由只负责请求校验、调用应用服务和构造响应，不放置 Agent 决策或业务状态机。
- 数据库 ORM 模型与领域 Pydantic 模型分离，通过 Repository 映射。
- 配置、Prompt、Agent 描述和 Skill 元数据必须版本化。
- 所有外部副作用应具备幂等键；重试不得产生重复 Artifact、重复审核或重复发布。
- Skill 的完整模型原始回答只允许写入任务日志目录下的受控独立文件；任务 JSONL 只记录
  文件引用、校验和和大小。总控/专业 Agent 回答、Prompt、隐藏推理、API Key 和输入文档
  原文不得按此方式留档。
- 专业 Agent 持久化 Skill 输出后，AgentRunResult 必须保存每次 Skill 调用对应的真实输入、
  输出 Artifact ID 和类型；下游必须通过这些 ID/依赖血缘读取对象内容并再次执行 Schema
  与 checksum 校验，禁止直接传递模型原始文本。
- 禁止使用自由字符串散落表示核心状态；已冻结状态应集中定义，尚未冻结的状态必须标注 TODO。

## 6. 中文注释与文档规范

- 每个源代码文件必须有中文模块 Docstring，说明文件职责和不负责的内容。
- 每个公共类必须有中文 Docstring，说明职责、生命周期和协作边界。
- 每个公共函数必须用中文说明用途、参数、返回值和可能抛出的业务异常。
- LangGraph 节点必须说明读取哪些状态、执行什么动作、写回哪些字段。
- 复杂状态转换、并发控制、幂等、复用和人工审核校验必须有中文注释。
- 不为显而易见的赋值、导入或简单返回添加重复代码含义的无效注释。
- 修改公共契约、状态或架构时，必须同步更新相关文档和测试。

## 7. 测试规则

- Agent 测试默认使用可编排的 FakeModelAdapter，不调用真实 DeepSeek API。
- 测试必须使用 FakeModelAdapter 或正式确定性 Skill，禁止恢复 Mock Skill。
- 每个状态转换、权限限制、Registry 校验和 Artifact 复用规则必须有单元测试。
- 所有发布路径必须测试“无 Approval 拒绝发布”和“Approval 版本不匹配拒绝发布”。
- 总控 Agent 必须使用 Golden Dataset 评估意图识别、子任务拆解、Agent 分配和 Artifact 复用。
- 新增 Agent、Skill、Artifact 类型或 TaskSpec 字段时，必须同时增加契约测试和至少一个路由评测案例。
- 禁止通过降低验收阈值、删除失败案例或放宽 Schema 来掩盖回归。

## 8. 修改原则

- 开始修改前先阅读 `docs/current_task.md`，只实施当前任务范围内的工作。
- 优先小步提交可验证的变更，不同时改动无关模块。
- 不覆盖或删除用户已有修改；发现冲突时先说明。
- 不修改已接受架构以迎合局部实现便利。
- 不把尚未确认的设计伪装成既定事实；使用 `TODO` 明确记录。
- 每阶段完成后运行对应测试，并在交付说明中列出已验证内容和未完成项。
