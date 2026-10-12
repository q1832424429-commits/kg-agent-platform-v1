# V1 正式 API

生产应用工厂为 `kg_agent.api.app:create_app`，默认不挂载 demo 路由。

浏览器访问 `/` 或 `/workbench` 可打开简易 Agent 工作台。工作台通过同源正式 API 提交问题和文件，并展示总控路由、专业 Agent 分配、业务 Skill、Artifact、人工审核和事件轨迹；它不包含独立业务状态或 Agent 决策逻辑。

生产工作台顶栏根据 MasterTask、AgentSubTask 和 HumanReview 状态显示任务完成百分比与当前阶段。浏览器通过 `GET /api/v1/conversations/{conversation_id}/events/stream` 接收同一会话内各任务的 SSE 事件并刷新快照；任务级 SSE 接口仍供单任务诊断使用。该数值是只读界面投影，不参与后端状态转换。

隔离的 Agent 人工观察实验室使用独立入口和 `8002` 端口，详见 `docs/simulation-lab.md`。实验室支持上传真实业务文件和真实外部本体；未选择文件时只按纯文本任务处理，不会伪造 `RawDocument` 或默认本体。实验室输出顺序执行日志，不进行自动正确性评分，也不会挂载到生产 OpenAPI。实验报告的 `memory` 字段及 `MEMORY` 日志阶段会展示测试命名空间的召回摘要和写入回执；`GET /health` 只公开 Mem0 启用状态、供应商与测试 `app_id`。

验证实验室在提交任务前订阅 `GET /api/v1/simulations/{task_id}/events/stream?tail=true`，
实时显示隔离 JSONL 中新增的公开阶段事实；POST 完成后仍以完整
`SimulationRunReport` 作为本轮最终结果。该 SSE 支持 `Last-Event-ID` 断线续传，
不传 `tail` 时也可用 `after` 指定起始行号。

- `POST /api/v1/tasks`：仅问题新建任务。
- `POST /api/v1/tasks/upload`：问题加可选多文档及可选外部本体新建任务。
- `POST /api/v1/tasks/{task_id}/messages`：继续或修改任务。
- `POST /api/v1/tasks/{task_id}/messages/upload`：追加问题、可选文档或可选外部本体。
- `GET /api/v1/tasks`、`GET /api/v1/tasks/{task_id}`：任务列表和完整公开快照。
- `GET /api/v1/conversations`、`GET /api/v1/conversations/{conversation_id}`：会话列表、公开消息和所属任务摘要；澄清轮次在尚未生成 MasterTask 时也可恢复。
- `PATCH /api/v1/conversations/{conversation_id}`：修改会话显示标题。
- `DELETE /api/v1/conversations/{conversation_id}`：清理该会话私有任务、对话、普通 Artifact、对象与日志，保留正式发布资产及已合并/复用记忆；返回跨存储清理告警。
- `GET /api/v1/conversations/{conversation_id}/events/stream`：该会话各任务的已提交事件 SSE，使用全局事件序号断线续传。
- `POST /api/v1/tasks/{task_id}/cancel`、`POST /api/v1/tasks/{task_id}/retry`：任务控制。
- `DELETE /api/v1/tasks/{task_id}`：兼容旧客户端的任务归档接口；工作台的“删除会话”使用上述会话删除接口。
- `GET /api/v1/tasks/{task_id}/events`：增量事件；`/events/stream` 为 SSE。
- `GET /api/v1/artifacts/{artifact_id}`、`/content`：Artifact 元数据与内容。
- `GET /api/v1/published-assets/ontologies`、`/graphs`：当前环境可复用的正式发布资产目录。
- `GET /api/v1/published-assets/{artifact_id}/content`、`/graph-preview`：查看未移出目录的不可变发布内容。
- `PATCH /api/v1/published-assets/{artifact_id}`：只修改目录显示名称，不改已审核 JSON 和 checksum。
- `DELETE /api/v1/published-assets/{artifact_id}`：从目录移除并禁止新任务选择；既有引用和审计正文保留。8002 使用相同路径但隔离数据库。
- `POST /api/v1/reviews`：创建审核；执行通常会在候选物和质量报告齐备时自动创建。
- `GET /api/v1/reviews/{review_id}/materials`：图谱或本体预览所需材料。
- `POST /api/v1/reviews/{review_id}/resolve`：批准、拒绝或提交结构化 ChangeSet。
- `GET /api/v1/registry/agents`、`/skills`：查看配置化能力。
- `GET /api/v1/memories/status`：查看 Mem0 长期记忆是否启用，不返回密钥或用户标识。
- `POST /api/v1/memories`：显式写入长期记忆，请求必须包含 `authorized=true`。
- `POST /api/v1/memories/search`：按自然语言召回当前单用户命名空间的长期记忆。

创建与消息接口的 `request_id` 是幂等键。同一键重复同一请求返回原结果，重复不同内容返回冲突。所有执行详情来自公开 ExecutionTrace，不包含模型隐藏思维过程。

任务 JSON 消息和 multipart 上传接口均支持 `remember_long_term` 布尔字段，默认 `false`。
只有明确设为 `true` 的本轮公开用户消息与任务摘要才会写入 Mem0。无论本轮是否写入，
总控都会在启用 Mem0 时先按当前目标召回已有长期记忆。

两个 multipart 接口将输入分为两个字段：

- `files`：零个或多个 PDF、DOCX、XLSX、PPTX 或图片，登记为 `RawDocument`。
- `ontology_file`：零个或一个 `.json` 文件，登记为 `ExternalOntology`，不能作为普通文档解析。

外部本体 JSON 的 V1 最小结构如下，顶层额外字段会被拒绝：

```json
{
  "schema_version": "1.0.0",
  "name": "制造业设备本体",
  "version": "1.0.0",
  "classes": [],
  "relations": [],
  "properties": []
}
```

上传本体只完成类型化接入，不代表审核或发布。本体经过必要的对齐/融合后直接进入人工预览、修改和确认门禁，不执行候选本体自动质量评估。

普通文档上传支持 PDF、DOCX、XLSX、PPTX、JPEG、PNG、TIFF 和 WebP。
正式 API 会根据受支持的扩展名规范 MIME，避免浏览器将 DOCX 误报为
`application/octet-stream` 或 `application/zip` 时被错误拒绝；旧版 `.doc` 仍不在 V1 范围内。

Mem0 服务端配置：`MEM0_ENABLED`、`MEM0_API_KEY`、`MEM0_BASE_URL`、
`MEM0_USER_ID`、`MEM0_APP_ID`、`MEM0_TOP_K`、`MEM0_CANDIDATE_TOP_K`、
`MEM0_THRESHOLD`、`MEM0_DEDUP_THRESHOLD`、`MEM0_MERGE_THRESHOLD`、`MEM0_RERANK` 和
`MEM0_TIMEOUT_SECONDS`。`MEM0_TOP_K` 最大为 3，`MEM0_CANDIDATE_TOP_K` 必须不小于
最终返回的 `MEM0_TOP_K`。
单用户 V1 的 `MEM0_USER_ID` 由后端环境统一配置，客户端不能覆盖。
