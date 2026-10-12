# 第五阶段验收记录

> 历史记录：本阶段当时沿用的 Mock 方案已由 ADR-059 全部废止并从当前代码删除；以下内容只保留阶段演进证据。

验收日期：2026-09-04

## 1. 实施范围

本阶段实现五个真实专业 Agent 共用的运行骨架、角色 Prompt、Worker 和安全结果提交：

- `BaseProfessionalAgent` 使用固定的小型 LangGraph 执行“计划 → 确定性校验 → 修复 → Skill 执行”，不承担全局业务编排。
- 五类专业 Agent 的能力来自 Agent Registry，角色职责来自版本化 Prompt，不在 Agent 主流程硬编码角色分支。
- `ProfessionalPlanValidator` 校验 Skill 注册状态、角色 allowlist、输入 Artifact、预期输出、副作用关闭状态和 ProposedSubTask 引用。
- 专业 Agent 只能执行与 Assignment、AgentSubTask、Context View、Worker 和输入 Artifact 完全一致的运行请求。
- V1 业务 Skill 继续使用第三阶段的确定性 Mock；专业 Agent 本身通过 ModelGateway 调用真实 LLM。
- Skill 输出通过 ArtifactWriter 写成不可变 Artifact，并保留生产 Agent、Skill、Run、TaskSpec revision 和父级血缘。
- `AgentRunResult` 保留成功、部分成功、返工、等待用户、可重试失败、永久失败和取消语义。
- `AgentRunService` 在持久化前校验 Assignment 身份、角色、有效租约、fencing token、Skill 权限和输出 Artifact。
- `AssignmentWorker` 只负责按角色认领、启动、心跳、调用专业 Agent 与提交结果，不选择 Skill，也不发布正式子任务。
- 专业 Agent 只能在 AgentRunResult 中返回 `proposed_subtasks`；第五阶段没有新增正式子任务发布入口。
- 新增 AgentRunResult 与 ExecutionTrace 的 SQLAlchemy Repository、ORM 表和 Alembic 迁移。
- AgentRunResult 与 ExecutionTrace 引用进入版本化 SharedContext 快照。

本阶段没有实现 HumanReviewService、PublishGuard、审核 API 或发布 Skill；这些内容仍属于第六阶段。没有实现 FastAPI、真实知识算法、Neo4j、向量数据库或前端。

## 2. AgentRunResult 状态处理

| AgentRunStatus | Assignment / Task 处理 |
|---|---|
| SUCCESS | Assignment 与子任务成功，确定性解锁依赖已满足的下游任务 |
| PARTIAL_SUCCESS | 保存已有 Artifact 和运行结果，本次 Assignment/子任务失败，等待总控后续处理 |
| REWORK_REQUIRED | 本次 Assignment/子任务失败，由总控根据结果决定是否发布返工子任务 |
| NEEDS_USER_INPUT | 本次 Assignment/子任务结束，MasterTask 进入 WAITING_USER |
| RETRYABLE_FAILED | 当前 Assignment 标记失败；未超过 max_attempts 时创建递增 attempt 的新 Assignment |
| FAILED | 本次 Assignment/子任务最终失败，不解锁下游 |
| CANCELLED | 当前 Assignment 与子任务取消 |

专业 Agent 的建议和非成功结果不会自行创建正式 AgentSubTask；总控 Agent 仍独占正式子任务发布权。

## 3. 关键安全和一致性结论

- Assignment、AgentSubTask、Context View、worker_id、角色和输入 Artifact 任一不一致时，专业 Agent 请求会被拒绝。
- 未注册、非 ACTIVE、越权或带发布副作用的 Skill 在执行前被拒绝；模型可在限定次数内根据结构化错误修复。
- 第六阶段 PublishGuard 完成前，`publish_ontology` 与 `publish_graph` 仍不可调用。
- 结果提交再次校验 Assignment 与 fencing token；不能把一个子任务结果写入另一条同角色 Assignment。
- SUCCESS 必须存在有效输出，并覆盖 AgentSubTask 的全部 expected_output_types。
- AgentRunResult、ExecutionTrace、Assignment 状态和 SharedContext 引用在同一数据库事务提交。
- ExecutionTrace 只保存公开结构化结果和指标，不保存模型隐藏思维过程。
- Worker 对专业 Agent 异常生成结构化失败结果，但不替专业 Agent 决定 Skill 或后续子任务。

## 4. 验收结果

| 验收项 | 结果 | 自动化证据 |
|---|---|---|
| 五类注册专业 Agent 均可使用统一真实 Agent 骨架 | 通过 | Professional Agent factory/Worker 测试 |
| Agent 只执行匹配角色和 Worker 的 RUNNING Assignment | 通过 | 请求一致性与角色隔离测试 |
| Skill 只能来自角色 allowlist | 通过 | 越权 Skill 修复测试 |
| Skill 输入必须来自当前子任务 Artifact | 通过 | ProfessionalPlanValidator 与请求契约测试 |
| 输出覆盖 expected_output_types | 通过 | 专业 Agent 成功路径与结果提交校验 |
| ProposedSubTask 只进入 AgentRunResult | 通过 | 等待用户分支测试 |
| 部分成功、返工、重试与永久失败语义 | 通过 | Mock 场景参数化测试 |
| RETRYABLE_FAILED 产生递增 Assignment attempt | 通过 | AgentRunService 集成测试 |
| NEEDS_USER_INPUT 使 MasterTask 进入 WAITING_USER | 通过 | AgentRunService/Worker 集成测试 |
| 过期或交叉 Assignment 结果不可回写 | 通过 | fencing 与防交叉回写测试 |
| Worker 成功贯通 Agent、Mock Skill、Artifact、Result | 通过 | Worker 完整成功路径集成测试 |
| AgentRunResult、ExecutionTrace 和 SharedContext 引用持久化 | 通过 | Repository 集成测试 |
| Alembic 升级与降级 | 通过 | 全量迁移测试 |
| 真实 DeepSeek 专业 Agent 选择 parse_document | 通过 | 在线内存 Artifact 冒烟测试 |

## 5. 验证命令

~~~bash
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy src
uv run pytest
~~~

环境型测试继续使用专用开关：PostgreSQL 实库锁语义测试需要 `KG_AGENT_TEST_DATABASE_URL`，MinIO 实服务往返测试需要 `KG_AGENT_TEST_MINIO_*`。普通自动测试不依赖真实 DeepSeek Key。

## 6. 最终结果

- Ruff：通过。
- Ruff format：通过，128 个 Python 文件格式正确。
- mypy strict：通过，82 个源文件无问题。
- pytest：112 passed，2 skipped。
- Alembic：阶段二、三、五迁移的升级、全量降级和重新升级通过。
- DeepSeek 在线冒烟：数据源解析 Agent 正确选择 `parse_document`，返回 SUCCESS，并产生 ParsedDocument 与 SemanticDocument。

两个跳过项仍是 PostgreSQL 实库锁语义和 MinIO 实服务往返测试，需要专用环境变量；不代表阶段五功能失败。
