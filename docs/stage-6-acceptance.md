# 第六阶段验收记录

验收日期：2026-09-04

## 1. 实施范围

本阶段完成候选本体与候选图谱的确定性人工审核和模拟发布保护：

- `HumanReviewService` 创建、查询并处理 `HumanReviewRequest`，审核门禁不是 Agent。
- 审核目标仅允许 `CandidateOntology`、`FusedCandidateOntology` 或 `CandidateGraph`，并固定记录目标 Artifact ID 与 checksum。
- 创建审核前必须提供指向当前目标的本体或图谱质量报告；冲突报告和证据 Artifact 可作为审核附件。
- `get_review_materials` 返回候选结果、质量报告、冲突报告和证据的轻量 `ArtifactRef`，供第七阶段 HTTP 接口展示，不把大型内容写入 SharedContext。
- 用户批准生成 `OntologyApproval` 或 `GraphApproval`；用户结构化修改生成 `OntologyChangeSet` 或 `GraphChangeSet`；拒绝必须记录反馈且不产生 Approval。
- ChangeSet 绑定原候选 ID 与 checksum，不直接修改候选 Artifact；修改后必须由总控 Agent 安排应用修改、重新质量评估和新一轮审核。
- `PublishGuard` 在发布 Skill 执行前确定性校验 Agent 权限、候选类型、任务隔离、Artifact 状态、Approval 血缘、内容 checksum、目标版本和真实审核终态。
- 发布 Skill 默认关闭；只有 `SkillRegistry` 绑定 PublishGuard 后才可能执行。
- 只有图谱管理更新 Agent 可调用 `publish_ontology` 和 `publish_graph`；发布结果继续是 V1 的确定性 Mock `PublishedOntology` / `PublishedGraph` Artifact。
- HumanReviewRequest、状态、决策 Artifact、用户反馈、任务事件和 ExecutionTrace 持久化；SharedContext 保存审核与轨迹引用。
- 新增第六阶段 SQLAlchemy Repository、ORM 表和 Alembic 迁移。

本阶段没有实现 FastAPI 路由、前端图编辑器、真实图数据库写入、真实知识算法或第七阶段端到端 API。

## 2. 审核状态与产物

| 用户操作 | 审核终态 | 决策 Artifact | 后续约束 |
|---|---|---|---|
| `APPROVE` | `APPROVED` | OntologyApproval / GraphApproval | 仅当前候选 ID 与 checksum 可发布 |
| `SUBMIT_CHANGES` | `CHANGES_REQUESTED` | OntologyChangeSet / GraphChangeSet | 应用修改后重新评估并重新审核 |
| `REJECT` | `REJECTED` | 无 | 总控 Agent 根据反馈安排返工 |

创建审核时 MasterTask 从 `ACTIVE` 进入 `WAITING_HUMAN`。最后一个待处理审核被解决后，MasterTask 返回 `ACTIVE`，由总控 Agent 根据 Approval、ChangeSet 或拒绝反馈安排后续正式子任务。

## 3. 发布保护结论

- 没有 Approval，发布被拒绝。
- Approval 指向其他候选 ID 或旧 checksum，发布被拒绝。
- Approval 内容被篡改或内容 checksum 不一致，发布被拒绝。
- Approval 没有关联状态为 `APPROVED` 的 HumanReviewRequest，发布被拒绝。
- ChangeSet 不能替代 Approval，也不能直接触发发布。
- 非图谱管理更新 Agent 即使持有真实 Approval，也不能调用发布 Skill。
- Published Artifact 必须同时继承一个候选结果和对应 Approval，并由带有效 Assignment/fencing 的图谱管理更新 Agent 写入。
- 用户修改不会原地覆盖候选结果；因此旧 Approval 不能被修改后的新候选复用。

## 4. 验收结果

| 验收项 | 结果 | 自动化证据 |
|---|---|---|
| 候选图谱审核请求、材料查询和状态切换 | 通过 | HumanReviewService 集成测试 |
| 融合候选本体审核和发布授权 | 通过 | 本体审核专项测试 |
| Approval 绑定目标 ID 与 checksum | 通过 | Approval 内容和血缘断言 |
| 无 Approval/ChangeSet 代替 Approval 时拒绝发布 | 通过 | PublishGuard 拒绝测试 |
| 旧 Approval 不能发布新候选版本 | 通过 | 目标版本不匹配测试 |
| 错误 checksum 的伪造 Approval 不能落库 | 通过 | ArtifactService 保护测试 |
| 非图谱管理 Agent 不能发布 | 通过 | Skill allowlist 专项测试 |
| 图谱管理 Agent 审核后完整发布闭环 | 通过 | Worker + Agent + Skill + Artifact 集成测试 |
| 拒绝反馈进入持久化审核记录 | 通过 | REJECT 专项测试 |
| 审核与轨迹引用进入 SharedContext | 通过 | 上下文快照断言 |
| Alembic 升级、全量降级和重新升级 | 通过 | 全量迁移测试 |

## 5. 验证命令

```bash
uv run ruff check src tests migrations
uv run ruff format --check src tests migrations
uv run mypy src
uv run pytest
```

PostgreSQL 实库锁语义测试和 MinIO 实服务往返测试仍需专用环境变量显式启用；普通自动测试使用 SQLite 和内存对象存储即可验证本阶段业务闭环。

## 6. 最终结果

- 第六阶段 9 个审核/发布专项测试通过。
- pytest：121 passed，2 skipped。
- Ruff：通过；Ruff format：136 个 Python 文件格式正确；mypy strict：87 个源文件无问题。
- Alembic：阶段二、三、五、六迁移的升级、全量降级和重新升级通过。
- 没有进入第七阶段，也没有创建 FastAPI 路由。
