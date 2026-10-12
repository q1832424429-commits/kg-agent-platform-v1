# 第三阶段验收记录

> 历史记录：本阶段当时使用的 Mock 方案已由 ADR-059 全部废止并从当前代码删除；以下内容只用于说明 2026-09-04 的阶段性验收，不代表当前运行能力。

验收日期：2026-09-04

## 1. 实施范围

本阶段只实现以下能力，没有进入第四阶段及后续业务：

- 22 类 V1 Artifact 的 Schema 1.0.0 内容契约和 ArtifactRegistry。
- Artifact 不可变创建、受控读取、状态变更、SHA-256 校验和父级血缘。
- TaskSpec revision、本体版本、父输入 checksum 和状态驱动的确定性复用。
- MinIO 对象存储适配器、内存测试适配器和 PostgreSQL 元数据/血缘迁移。
- PostgresArtifactService 的事务提交、失败回滚、孤儿对象补偿和提交结果不明保护。
- 第三阶段当时交付了 5 类专业 Agent 的 18 个版本化 Mock Skill、Skill Registry、allowlist 与启动一致性校验；该历史数量已被 ADR-066 的统一知识抽取方案替代，当前为 16 个 Skill。
- SUCCESS、PARTIAL_SUCCESS、LOW_QUALITY、RETRYABLE_FAILURE 和 PERMANENT_FAILURE 五类 Mock 场景。
- 在 HumanReviewService/PublishGuard 完成前关闭发布类副作用 Skill 和 Published Artifact 写入。

本阶段未实现专业 Agent 运行时、真实文档解析、真实知识抽取/融合、人工审核服务、
FastAPI、Neo4j、向量数据库或前端。

## 2. 验收结果

| 验收项 | 结果 | 自动化证据 |
|---|---|---|
| Artifact 元数据、内容、checksum 与血缘一致 | 通过 | Artifact Service 集成测试 |
| 修改产生新 Artifact 并保留旧版本 | 通过 | immutable supersede 测试 |
| 不兼容 Schema、revision、本体或父 checksum 拒绝复用 | 通过 | ArtifactReuseService 测试 |
| 父 Artifact 失效后拒绝复用 | 通过 | invalidated parent 测试 |
| 旧 fencing token、过期 lease、跨任务 Assignment 拒绝写入 | 通过 | claim boundary 测试 |
| 上传期间不持有数据库业务锁并在锁内二次校验 | 通过 | ArtifactService 双阶段校验结构及回归测试 |
| 数据库失败清理本次孤儿对象 | 通过 | repository/commit failure 测试 |
| commit 已成功但响应丢失时不误删对象 | 通过 | uncertain commit 测试 |
| 未授权 Agent 不能调用其他 Agent 的 Skill | 通过 | Skill Registry 权限测试 |
| 发布门禁不能被 Mock Skill 绕过 | 通过 | side-effect closed 测试 |
| 新 YAML Skill 无需修改总控核心代码即可发现 | 通过 | Registry discovery 测试 |
| 五类 Mock 的五种结果模式稳定可重复 | 通过 | Mock scenario 测试 |
| Alembic upgrade/downgrade/upgrade | 通过 | SQLite 迁移往返测试 |

## 3. 验证命令与结果

~~~bash
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy src
uv run pytest
~~~

最终本地结果：

- Ruff：通过。
- Ruff format：通过。
- mypy strict：通过，67 个源文件无问题。
- pytest：87 passed，2 skipped。

两个跳过项不是失败：

1. PostgreSQL FOR UPDATE SKIP LOCKED 实库并发测试需要
   KG_AGENT_TEST_DATABASE_URL 指向名称含 test 的专用数据库。
2. MinIO 实服务往返测试需要 KG_AGENT_TEST_MINIO_* 专用配置和
   kg-agent-test- 前缀 Bucket。

无网络的 MinIO SDK 适配器测试、SQLite Repository/事务测试和 Alembic 迁移测试均已通过。
