# Linux Docker Compose 部署与验收

## 配置

从 `.env.example` 创建 `.env`。生产环境必须替换数据库和 MinIO 密码，不得提交真实 DeepSeek 或 Mem0 Key。API 和 Worker 使用同一套 PostgreSQL、MinIO、Mem0 与 Registry 配置。填写 `MEM0_API_KEY` 后可省略 `MEM0_ENABLED`，系统会自动启用；也可显式设为 `true`。

## 启动和迁移

`docker compose up --build -d` 会等待 PostgreSQL，通过一次性 `migrate` 服务执行 `alembic upgrade head`，再启动 API，最后在 API 就绪后启动 Worker。数据库和对象内容分别使用具名卷持久化。

```bash
docker compose ps
docker compose logs -f api worker
docker compose run --rm migrate alembic current
docker compose down                 # 保留数据卷
docker compose down --volumes       # 删除数据，仅限明确需要重置的开发环境
```

## 验收

1. `api`、`worker`、`postgres`、`minio` 状态正常，`migrate` 退出码为 0。
2. `/health/live` 和 `/health/ready` 返回成功。
3. 提交任务后可查询总控意图、TaskSpec、子任务和 Assignment。
4. Worker 认领后产生 AgentRunResult、ExecutionTrace 和经过正式 Schema 校验的 Artifact。
5. 候选本体或图谱进入 `WAITING_HUMAN`，批准后总控继续；缺少 Approval 时发布 Skill 被拒绝。
6. 重复 request_id 不产生第二份任务事实。
7. Ruff、mypy、pytest 与 Golden 门禁均通过。
8. `/api/v1/memories/status` 显示 `mem0-cloud`，授权写入后可通过 `/api/v1/memories/search` 召回。

V1 不提供 Mock Skill、模拟文档或默认模拟本体。需要语义推理的 Skill 使用 LLM 联通实现，DOCX 解析、图谱组装、ChangeSet 应用和发布等已注册能力使用正式确定性实现；生产级复杂多模态解析和知识算法仍需逐步增强。
