# Agent 验证实验室

## 作用

验证实验室用于人工观察 V1 的真实 Agent 决策与 LLM/正式确定性 Skill 执行，不承担生产业务，也不自动判断设计是否正确。它按顺序输出：

1. 用户真实上传文件或纯文本任务的输入事实。
2. 总控 Agent 输出的 `START`、`CONTINUE` 或 `MODIFY` 意图。
3. 总控生成的 TaskSpec 与目标 Artifact 类型。
4. 总控拆解的全部 AgentSubTask 及负责的专业 Agent。
5. AgentAssignment 的创建、Worker 认领者和执行状态。
6. 每个专业 Agent 实际调用的 Skills、调用顺序及其公开输出。
7. 最终产生的 Artifact 类型、生产者和内容预览。
8. 测试端 Mem0 的召回数量、召回摘要引用和本轮异步写入回执。
9. 候选图谱或已发布图谱的原生 SVG 节点—边预览；可查看节点、关系、属性、证据和内容告警。

实验室不提供预期答案、自动评分或 PASS/FAIL。用户根据日志人工检查意图、任务拆解、Agent 分配和 Skill 顺序是否符合原始问题。

## 实时进度

页面会先建立 `GET /api/v1/simulations/{task_id}/events/stream?tail=true` SSE，再提交
本轮问题。执行期间实时显示 Mem0 召回、总控开始/完成、专业 Agent 完成和协调器检查；
POST 返回后使用完整报告校正日志和最终百分比，不再把已完成结果按定时器伪装成实时回放。

SSE 的 `id` 是测试任务 JSONL 的物理行号。浏览器重连时通过 `Last-Event-ID` 继续；
服务端每 10 秒发送一次心跳。`after=<行号>` 可从指定位置读取，`follow=false` 用于只读
取当前已有事件后立即结束。JSONL 仍是测试观察和排障记录，不是生产任务事实来源。

## 隔离边界

- 实验室入口是 `simulation_lab.app:create_app`，不挂载到生产 FastAPI。
- 实验室默认使用 `8002` 端口，生产工作台继续使用 `8000` 端口。
- Docker 模式使用独立 `simulation-db` 具名卷，页面刷新和测试端重启后可恢复任务快照。
- Artifact 内容写入 MinIO 的独立 `kg-agent-simulation-artifacts` Bucket，不进入正式
  `kg-agent-artifacts` Bucket。仅重启 8002 进程不会再丢失历史 Artifact 内容；停止
  `simulation-db` 后任务元数据会按测试环境约定清空，遗留对象不再被任务引用。
- 页面支持上传真实业务文档，选择后以真实文件作为隔离 `RawDocument`；未选择文件时按纯文本任务进入总控，不会伪造任何文件。
- 页面提供独立的真实本体 JSON 选择框。本体文件按正式 `ExternalOntologyContent` 校验，但只写入实验室独立数据库和测试 Bucket，不进入生产环境。
- 未上传外部本体且没有任务内已发布本体时，若用户目标需要实例抽取或构图，总控必须先规划候选本体建设；候选本体不经过自动质量评估，直接等待用户预览、修改和确认。
- 继续任务时若没有新增真实文件，只复用该任务 SharedContext 中已有 Artifact。
- 实验室可以读取正式 Agent、Skill 和 Artifact Registry 配置，但不能修改 Registry。
- 实验结果不会写入生产 SharedContext、任务表、审核表或执行轨迹表。
- 实验室长期记忆使用独立的 `SIMULATION_MEM0_APP_ID` 与 `SIMULATION_MEM0_USER_ID`。它可以复用同一个服务端 API Key，但不会召回正式端命名空间的数据。
- 实验室每轮只向 Mem0 提交当前公开用户消息、公开任务摘要和专业 Agent 的公开经验；不会提交完整会话历史、上传文件内容或模型隐藏推理。
- 外部本体 JSON 内容不会写入 Mem0；日志只展示类型化 Artifact 的公开摘要和实验结果。
- 图谱预览由只读 API 投影生成，不把浏览器布局或点击状态写入测试数据库。候选图谱审核时，
  节点/关系重命名和删除会追加到既有 `GraphChangeSet` 草稿；新增、合并、拆分仍可以通过
  审核窗口内的 ChangeSet JSON 输入，所有修改均须按正常审核路径提交。

## Mem0 配置

实验室在未显式配置开关且检测到可用 `MEM0_API_KEY` 时启用，也可以用以下环境变量显式控制：

```dotenv
SIMULATION_MEM0_ENABLED=true
SIMULATION_MEM0_API_KEY=
SIMULATION_MEM0_BASE_URL=https://api.mem0.ai
SIMULATION_MEM0_USER_ID=kg-agent-v1-simulation-user
SIMULATION_MEM0_APP_ID=kg-agent-platform-simulation
```

`SIMULATION_MEM0_API_KEY` 留空时复用服务端 `MEM0_API_KEY`。这里只复用凭据，数据通过测试 `app_id + user_id` 双重隔离。`GET /health` 会返回是否启用、供应商和 `app_id`，不会返回 Key 或用户标识。

WSL 原生 Docker 若需要通过 Windows 代理访问 Mem0，在 `.env` 中配置：

```dotenv
OUTBOUND_HTTP_PROXY=http://<Windows网关>:<代理端口>
OUTBOUND_HTTPS_PROXY=http://<Windows网关>:<代理端口>
OUTBOUND_NO_PROXY=localhost,127.0.0.1,postgres,minio,simulation-db
```

Windows 网关可用 `ip route show default` 查看。代理软件重启或 WSL 网络重建后，网关或动态代理端口可能变化，需要重新检测并更新这两个值。

## Artifact 持久化配置

测试端复用同一个 MinIO 服务，但必须使用独立 Bucket。Docker Compose 已提供以下默认值：

```dotenv
SIMULATION_MINIO_ENDPOINT=minio:9000
SIMULATION_MINIO_ACCESS_KEY=
SIMULATION_MINIO_SECRET_KEY=
SIMULATION_MINIO_BUCKET=kg-agent-simulation-artifacts
SIMULATION_MINIO_SECURE=false
```

Access Key 和 Secret Key 可以复用 MinIO 服务凭据；Bucket 名不能与正式端
`MINIO_BUCKET` 相同。`GET /health` 会公开当前存储类型和 Bucket 名，便于确认隔离配置，
但不会返回任何凭据。

## 启动

```bash
docker compose --profile simulation up -d simulation
```

浏览器访问 `http://127.0.0.1:8002/`。

停止实验室时执行：

```bash
docker compose --profile simulation stop simulation simulation-db
```
