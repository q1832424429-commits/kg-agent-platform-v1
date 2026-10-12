# V1 从 0 到 1 实施计划

> 状态说明：阶段 0 至阶段 9 已完成并进入联调。本文件下方的阶段描述保留最初实施历史；其中 Mock Skill、模拟输入和默认模拟本体方案已经由 ADR-059 废止，当前实现只允许 LLM 或正式确定性 Skill。

## 1. 实施原则

- 按契约优先、确定性服务优先、Agent 后接入、端到端最后贯通的顺序开发。
- 每个阶段必须有独立可执行的验收，不依赖“全部完成后再测试”。
- 阶段 0 至阶段 9 已完成；当前进行真实输入、LLM 准确率、已发布资产复用和生产算法替换联调。
- 任何与已接受 ADR 冲突的实现必须停止并先更新决策文档。

## 2. 阶段 0：方案固化与仓库建立

### 目标

把聊天中最终确认的业务范围、架构、契约和决策固化到仓库，形成后续开发的唯一项目依据。

### 前置依赖

- 最终需求和架构讨论已经完成。

### 输入

- 已确认的项目目标、Agent 架构、V1 范围、非 V1 范围和技术选型。

### 输出

- `AGENTS.md`
- `PROJECT.md`
- `docs/architecture.md`
- `docs/contracts.md`
- `docs/decisions.md`
- `docs/implementation-plan.md`
- `docs/current_task.md`

### 验收标准

- 文档之间不存在明显冲突。
- 已否决方案均在 ADR 中明确记录。
- 未确认内容以 TODO 标记，没有被自行补全为既定架构。
- 仓库中没有业务代码。

## 3. 阶段 1：项目基础骨架与核心数据契约

### 目标

建立可安装、可测试的 Python 项目骨架，实现核心 Pydantic 契约，并跑通“用户对话 → 总控 Agent → 意图识别 → TaskSpec → AgentSubTask 拆解和专业 Agent 分配 → 校验后发布到内存 SharedContext”的最小闭环。

### 前置依赖

- 阶段 0 经用户审核通过。
- 未冻结的状态枚举在本阶段继续按 `str` 占位，不实现依赖这些枚举的完整状态机。

### 输入

- `AGENTS.md`
- `PROJECT.md`
- `docs/contracts.md`
- `docs/decisions.md`

### 输出

- `pyproject.toml`、依赖锁文件和基础包结构。
- 配置读取和统一异常骨架。
- TaskSpec、MasterTask、AgentSubTask、AgentAssignment、ArtifactRef、SharedTaskContext、AgentRunResult、HumanReviewRequest、ExecutionTrace 等 Pydantic 模型。
- Agent、Skill 和 Artifact 注册元数据模型。
- DeepSeek ModelGateway、FakeModelAdapter、总控 Agent 小型 LangGraph、SubTaskValidator 和内存 SharedContext。
- 覆盖 START、CONTINUE、MODIFY、澄清、非法分配和审核门禁的开发测试案例。
- JSON Schema 导出能力。
- 契约单元测试。

### 验收标准

- 所有模型可正确序列化、反序列化和生成 JSON Schema。
- 非法 TaskIntent、负 revision、缺失关键字段等输入被拒绝。
- `NEEDS_USER_INPUT` 缺少问题、`SUCCESS` 缺少预期输出等跨字段约束有测试。
- 总控 Agent 能生成或修订 TaskSpec，并为 AgentSubTask 指定已注册的专业 Agent。
- 非法模型输出、未知 Agent、能力不匹配、循环依赖和错误审核门禁不能写入 SharedContext。
- 自动测试使用 FakeModelAdapter，不需要 DeepSeek API Key。
- 代码通过 Ruff、mypy 和 pytest。
- 不包含专业 Agent 运行、业务 Skill、数据库、MinIO、FastAPI 或完整发布逻辑。

## 4. 阶段 2：持久化、状态服务与 SharedContext

### 目标

实现不依赖 LLM 的任务事实来源、状态迁移、事件记录和上下文视图。

### 前置依赖

- 阶段 1 核心契约稳定。
- MasterTask、Assignment、Artifact、HumanReview 状态枚举已按 ADR-029 冻结。

### 输入

- 领域契约。
- 状态迁移规则。

### 输出

- SQLAlchemy ORM 模型和 Alembic 初始迁移。
- Repository 层。
- TaskStateService、AssignmentService、ContextService。
- SharedContext 快照与追加事件。
- Context View 构建器。
- Assignment lease、heartbeat、fencing token 和过期恢复。
- PostgreSQL 集成测试。

### 验收标准

- TaskSpec revision、decomposition revision 和历史记录不可被覆盖。
- Assignment 原子认领，同一 Assignment 无法被两个 Worker 同时持有。
- 旧 fencing token 无法提交结果。
- 非法状态跳转被确定性拒绝。
- 不同 Agent 得到的 Context View 只包含其所需字段。

## 5. 阶段 3：Artifact、对象存储与 Registry

**实施状态：已完成（2026-09-04）；实服务联调测试需在专用 PostgreSQL/MinIO 测试环境中显式启用。**

### 目标

建立 Agent/Skill 可插拔基础和 Artifact 全生命周期，不实现真实知识算法。

### 前置依赖

- 阶段 1 契约稳定。
- 阶段 2 数据库与上下文服务可用。

### 输入

- Artifact 家族和血缘规则。
- 六个 Agent 的能力声明。
- V1 Mock Skill 清单。

### 输出

- ArtifactService、ArtifactRegistry、ArtifactReuseService。
- MinIO 受控内容存储和 checksum。
- AgentRegistry、SkillRegistry、启动期一致性校验。
- 五类专业 Agent 的 Mock Skill。
- Mock 的成功、部分成功、低质量、可重试失败和永久失败模式。

### 验收标准

- Artifact 元数据、内容、checksum 和父级血缘一致。
- 修改 Artifact 产生新版本或 ChangeSet，不原地覆盖。
- 不兼容本体版本、输入 checksum 或 Schema 版本时拒绝复用。
- 未授权 Agent 无法调用其他 Agent 独有 Skill。
- 新增测试 Skill 无需修改总控核心代码即可被 Registry 发现。

## 6. 阶段 4：ModelGateway 与总控 Agent

### 目标

实现真实 LLM 总控 Agent，并重点验证意图识别、TaskSpec、子任务拆解、Agent 分配和 Artifact 复用。

### 前置依赖

- 阶段 2 的 SharedContext 和状态服务。
- 阶段 3 的 Agent/Artifact Registry。
- DeepSeek API Key 仅在真实联调环境提供；自动测试不依赖真实 Key。

### 输入

- 用户消息、ConversationContext、SharedContext、Agent 能力目录和已有 Artifact。

### 输出

- 可替换 ModelGateway、DeepSeekAdapter、FakeModelAdapter。
- 版本化总控 Prompt。
- 总控 Agent 小型 LangGraph。
- TaskDecompositionResult 结构化输出和修复重试。
- SubTaskValidator、HandoffValidator、权限和人工门禁校验。
- 子任务与 Assignment 原子发布。

### 验收标准

- 只产生 `START`、`CONTINUE`、`MODIFY`，或进入非业务意图 `NEEDS_CLARIFICATION` 控制结果。
- 不存在当前任务时不能错误执行 `CONTINUE`。
- 子任务 Agent 类型、输入输出、依赖和审核节点均可确定性验证。
- 专业 Agent 能力来自 Registry，不来自硬编码条件分支。
- 未经校验的模型输出不能写入 SharedContext。

## 7. 阶段 5：五个专业 Agent 与 Worker

**实施状态：已完成（2026-09-04）；发布类 Skill 在当时保持关闭，现已由第六阶段 PublishGuard 保护。**

### 目标

实现真实专业 Agent 的统一运行骨架、角色配置和后台认领执行。

### 前置依赖

- 阶段 3 的 Mock Skill 和 Registry。
- 阶段 4 的 ModelGateway。

### 输入

- 已发布 AgentSubTask、AgentAssignment 和角色化 Context View。

### 输出

- BaseProfessionalAgent。
- 五个真实专业 Agent 配置和 Prompt。
- Skill 选择、调用、输出检查和 AgentRunResult。
- assignment worker、心跳和安全结果提交。
- 专业 Agent 提出 ProposedSubTask 的机制。

### 验收标准

- Agent 只能认领匹配自身类型的 Assignment。
- Agent 只能选择 allowlist Skill。
- Agent 使用正确输入 Artifact，并输出预期 Artifact 类型。
- AgentRunResult 的成功、返工、用户输入、重试和失败语义正确。
- Worker 只是运行进程，不拥有新的 Agent 决策职责。

## 8. 阶段 6：人工审核与发布保护

**实施状态：已完成（2026-09-04）；已提供应用服务接口，HTTP 路由属于第七阶段。**

### 目标

完成本体和候选图谱的查看、修改、批准、拒绝、返工和模拟发布闭环。

### 前置依赖

- Artifact/ChangeSet 契约。
- 图谱管理更新 Agent 和质量评估 Agent 可运行。

### 输入

- 候选本体/图谱、质量报告、冲突报告和证据 Artifact。

### 输出

- HumanReviewService 和 Review 应用服务 API（不含 HTTP 路由）。
- OntologyChangeSet、GraphChangeSet、OntologyApproval、GraphApproval。
- PublishGuard。
- 模拟 PublishedOntology 和 PublishedGraph Artifact。

### 验收标准

- 无 Approval、Approval 目标版本不符、审核后内容变更时全部拒绝发布。
- 修改后的候选结果按影响进入重新评估。
- 只有图谱管理更新 Agent 可以调用发布 Skill。
- 审核状态和用户反馈完整进入 SharedContext 和 ExecutionTrace。

## 9. 阶段 7：FastAPI、事件接口和端到端闭环

**实施状态：已完成（2026-09-07）；正式任务、上传、多轮、审核、事件、幂等和生产 API 已实现，demo 不再是默认入口。**

### 目标

提供无前端也能完整操作 V1 的后端 API。

### 前置依赖

- 阶段 2 至阶段 6 的应用服务可用。

### 输入

- 对话、文件、任务控制、审核和查询请求。

### 输出

- 对话和消息接口。
- 文档上传接口。
- 任务、子任务和 Assignment 查询及取消/重试接口。
- Artifact 和血缘查询接口。
- 审核与 ChangeSet 接口。
- SSE 任务事件接口。
- 健康检查、统一异常响应和 OpenAPI 文档。

### 验收标准

- API 可完成“上传文档到模拟图谱发布”的完整闭环。
- 创建类接口具备幂等保护。
- 文件名、MIME、大小和存储路径受控。
- 用户能查询当前任务处于 Agent 执行、返工还是等待审核。

## 10. 阶段 8：Golden Dataset 与准确率评测

**实施状态：已完成（2026-09-07）；已提供 20 条制造业案例、指标计算、阈值门禁和回归命令。**

### 目标

建立可重复的 Agent 准确率基线和回归门禁。

### 前置依赖

- 总控和专业 Agent 已可通过 FakeModel 与真实 DeepSeek 运行。

### 输入

- 内置制造业开发案例。
- 后续提供的脱敏真实业务对话。

### 输出

- GoldenEvaluationRecord 数据集。
- 意图、TaskSpec、子任务、Agent 分配、Artifact 复用、门禁和端到端指标。
- 混淆矩阵和失败案例报告。
- CI 可执行回归命令。

### 验收标准

- Task Intent Macro-F1 ≥ 95%。
- Agent 分配准确率 ≥ 95%。
- 必要子任务召回率 ≥ 95%。
- 不必要子任务率 ≤ 5%。
- Artifact 复用判断准确率 ≥ 95%。
- 本体/图谱审核遗漏和未经审核发布均为 0。
- 端到端路由成功率 ≥ 90%。

## 11. 阶段 9：Docker Compose、文档与交付验收

**实施状态：已完成（2026-09-07）；已提供 API、Worker、PostgreSQL、MinIO、自动迁移和交付文档。**

### 目标

使 V1 在 Linux 上能够隔离部署、运行、测试和演示。

### 前置依赖

- 阶段 1 至阶段 8 完成。

### 输入

- API、worker、PostgreSQL、MinIO、迁移和测试。

### 输出

- Dockerfile、Docker Compose、`.env.example`。
- 启动、迁移、测试、评测和 API 使用说明。
- 完整中文 README 和验收报告。

### 验收标准

- `docker compose up -d` 可以启动 api、worker、postgres 和 minio。
- 健康检查通过，数据库迁移自动或按文档可重复执行。
- 没有 DeepSeek Key 时可使用 FakeModel 演示和运行自动测试。
- 配置真实 Key 后可完成真实 Agent 决策联调。
- 代码、契约、架构和 ADR 文档保持一致。

## 12. 阶段依赖关系

```text
阶段0 方案固化
   ↓
阶段1 基础骨架与契约
   ↓
阶段2 持久化与SharedContext
   ↓
阶段3 Artifact、Registry与Mock Skills
   ↓
阶段4 总控Agent
   ↓
阶段5 专业Agent与Worker
   ↓
阶段6 Human Review与发布保护
   ↓
阶段7 API端到端闭环
   ↓
阶段8 Golden Dataset与评测
   ↓
阶段9 Docker Compose与交付
```

阶段 2 和阶段 3 的部分模块可以并行开发，但必须共同依赖阶段 1 的核心契约。后续阶段不得通过复制或私有定义绕过公共契约。
