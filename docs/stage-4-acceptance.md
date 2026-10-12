# 第四阶段验收记录

验收日期：2026-09-04

## 1. 实施范围

本阶段补齐真实总控 Agent 的以下能力：

- DeepSeekModelAdapter 与 FakeModelAdapter 继续通过统一 ModelGateway 提供结构化输出。
- 总控 Agent 小型 LangGraph 完成上下文加载、模型拆解、确定性校验、修复和发布。
- 从 SharedContext 加载不可变 TaskSpec 历史、子任务、Artifact、审核和记忆摘要。
- SubTaskValidator 校验意图、不可变 revision、拆解 revision、Agent Registry 能力、依赖、发布权限、本体前置条件和审核祖先链。
- HandoffValidator 校验 Artifact 存在性、任务隔离、生命周期、Schema、输入复用声明和输出类型。
- ArtifactReuseValidator 把 LLM 建议转换为 ArtifactReuseRequest，并检查 TaskSpec 影响、本体、Schema、状态、父级血缘和 checksum。
- PostgresArtifactReuseService 使用短生命周期 Session 读取正式 Artifact 记录。
- PostgresContextService 支持读取历史 TaskSpec，并通过 TaskStateService 原子创建子任务、Assignment、事件和 SharedContext 快照。
- 生产组合入口组装 DeepSeek、PostgreSQL、Agent Registry、Artifact Registry 和版本化 Prompt。
- 拆解发布事件记录本次 TaskSpec revision 和获批复用 Artifact ID。

本阶段没有实现第五阶段的专业 Agent、Worker 和 Skill 选择循环；没有实现 HumanReviewService、PublishGuard、FastAPI、真实业务 Skill、图数据库、向量数据库或前端。

## 2. 关键安全和一致性结论

- LLM 输出必须依次通过 Pydantic、子任务、交接和复用校验，非法候选只能修复或失败，不能先写 SharedContext。
- `CONTINUE` 沿用当前 TaskSpec revision 时必须逐字段完全相同；字段变化必须创建新 revision。
- 即使调用方漏传 current_task_id，总控仍会按 task_id 查找现有任务，不能把已有任务误当 START。
- 历史 Artifact 被子任务使用时必须显式列入 reused_artifact_ids，不能绕过 ArtifactReuseService。
- LLM 提供的 reuse_policy 不具有审批权；兼容 revision 由确定性 TaskSpec 影响比较得出。
- 发布权限读取 Agent Registry 的 can_publish_ontology/can_publish_graph，不依赖总控 Agent 名称分支。
- 人工审核质量任务必须处于发布任务的实际依赖祖先链；无关审核标记不能伪造门禁。
- 第四阶段只验证计划中的审核结构；真实 Approval 内容与副作用发布仍由第六阶段门禁负责。

## 3. 验收结果

| 验收项 | 结果 | 自动化证据 |
|---|---|---|
| START/CONTINUE/MODIFY 与澄清控制结果 | 通过 | Controller Agent 测试 |
| 无当前任务的 CONTINUE 不发布 | 通过 | Controller clarification 测试 |
| 同 revision 不可变与拆解 revision 递增 | 通过 | SubTaskValidator/Controller 测试 |
| Agent 类型、输入、输出和发布权限来自 Registry | 通过 | Validator 测试 |
| 跨任务、失效、未知 Schema Artifact 拒绝交接 | 通过 | HandoffValidator 测试 |
| 历史输入不能省略复用声明 | 通过 | HandoffValidator 测试 |
| TaskSpec 影响或父血缘不兼容时拒绝复用 | 通过 | ArtifactReuseValidator 测试 |
| 目标扩展但执行约束不变时可复用已有成果 | 通过 | ArtifactReuseValidator 测试 |
| 审核标记必须在发布依赖祖先链 | 通过 | Review gate 测试 |
| 实例抽取必须依赖 PublishedOntology | 通过 | Ontology prerequisite 测试 |
| 合法拆解原子创建子任务和 Assignment | 通过 | PostgreSQL 适配集成测试 |
| 非法模型候选不创建 MasterTask | 通过 | PostgreSQL 零写入集成测试 |
| 获批复用 ID 进入拆解审计事件 | 通过 | PostgreSQL 事件集成测试 |
| 真实 DeepSeek 结构化调用和澄清路径 | 通过 | 在线无真实文档冒烟测试 |

## 4. 验证命令

~~~bash
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy src
uv run pytest
~~~

环境型测试继续使用专用开关：PostgreSQL 锁语义测试需要 KG_AGENT_TEST_DATABASE_URL，MinIO 往返测试需要 KG_AGENT_TEST_MINIO_*。普通自动测试不依赖真实 DeepSeek Key。

## 5. 最终结果

- Ruff：通过。
- Ruff format：通过，113 个 Python 文件格式正确。
- mypy strict：通过，71 个源文件无问题。
- pytest：100 passed，2 skipped。
- DeepSeek 在线冒烟：START + NEEDS_CLARIFICATION 控制路径通过，未写入 SharedContext。

两个跳过项仍是 PostgreSQL 实库锁语义和 MinIO 实服务往返测试；它们需要专用测试环境变量，不代表功能失败。
