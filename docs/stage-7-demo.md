# 第七阶段最小可观察演示

> 已废止：该演示依赖的 Mock Skill、模拟文件和 `scenario` 参数已由 ADR-059 删除。当前请使用正式工作台或 8002 验证实验室上传真实文件；本文后续命令仅作为历史记录，不可用于当前接口。

## 1. 当前可测试范围

本演示用于验证以下主链路：

```text
用户问题 + 模拟上传文件
  → 总控 Agent 调用 DeepSeek 识别 START / CONTINUE / MODIFY
  → 生成 TaskSpec
  → 拆分 AgentSubTask 并指定专业 Agent
  → 发布 AgentAssignment 到 SharedContext
  → 当前任务范围内的 Worker 认领 Assignment
  → 专业 Agent 调用 DeepSeek 选择自身允许的 Skill
  → Skill 以确定性 Mock 实现执行
  → 生成带血缘的 Mock Artifact
  → API 返回路由、分配、Skill 和 Artifact 预览
```

总控 Agent 和专业 Agent 都是真实 Agent；只有 Skill 内部业务算法是 Mock。上传的 PDF、图片或表格不会在这一阶段被真正解析，Mock Skill 会生成符合正式数据契约的占位 Artifact。

## 2. 启动服务

在仓库根目录执行：

```bash
uv run --env-file .env uvicorn kg_agent.api.app:create_app \
  --factory \
  --host 127.0.0.1 \
  --port 8000
```

打开 `http://127.0.0.1:8000/docs`。使用 `POST /api/v1/demo/runs` 时只填写 `question`，系统自动注入 `simulated-input.pdf`；使用 `POST /api/v1/demo/upload-runs` 时必须选择真实文件并填写问题。

当前入口使用进程内 SQLite 和内存对象存储，专门用于单进程演示；服务重启后任务和文件会清空，不代表第七阶段最终的生产部署形态。

## 3. 使用自动模拟文件并提问

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/demo/runs" \
  -F 'question=只解析模拟设备 PDF，生成 ParsedDocument 和 SemanticDocument；不要抽取实体、不要构建本体或图谱。' \
  -F 'execute_workers=true' \
  -F 'max_worker_runs=10' \
  -F 'scenario=SUCCESS'
```

如果要测试真实上传表单，请把路径改为 `/api/v1/demo/upload-runs`，并增加 `-F 'files=@./equipment.pdf;type=application/pdf'`；`files` 字段可以重复传入多个文件。`scenario` 用于控制 Mock Skill，可使用 `SUCCESS`、`PARTIAL_SUCCESS`、`LOW_QUALITY`、`RETRYABLE_FAILURE` 或 `PERMANENT_FAILURE`。

只想查看总控路由、不执行 Worker 时，将 `execute_workers` 设置为 `false`。

## 4. 响应中重点观察的字段

- `task_intent`：总控识别的新建、继续或修改任务意图。
- `task_spec`：总控形成的结构化任务目标、输入和目标产物。
- `subtasks`：拆分结果、指定的专业 Agent、输入和预期输出。
- `assignments`：任务认领者和最终状态。
- `worker_runs`：专业 Agent 的运行结果，以及它选择的 Mock Skill。
- `artifacts`：上传的 RawDocument 和 Mock Skill 产生的正式类型、血缘及内容预览。

`RawDocument.content_preview` 固定为空，API 不回显上传文件原文；非原始文档 Artifact 可返回用于演示的 JSON 内容预览。

## 5. 继续或修改同一任务

首次响应会返回 `task_id` 和 `conversation_id`。在服务未重启的情况下，再次请求时同时传入这两个字段，即可测试 `CONTINUE` 或 `MODIFY`：

```bash
curl -X POST "http://127.0.0.1:8000/api/v1/demo/runs" \
  -F 'question=修改之前的目标：继续抽取实体，并复用已经生成的语义文档。' \
  -F 'files=@./supplement.pdf;type=application/pdf' \
  -F 'task_id=<首次响应中的 task_id>' \
  -F 'conversation_id=<首次响应中的 conversation_id>' \
  -F 'execute_workers=true'
```

能否复用历史 Artifact 由总控 Agent 按正式契约判断；若校验不兼容，系统会拒绝错误复用。

## 6. 当前尚未包含的第七阶段内容

该入口只是第七阶段的最小演示切片，尚不代表第七阶段全部完成。以下内容仍待后续实现：

- 面向生产数据库和对象存储的完整 API 组合。
- 独立常驻 Worker 和异步任务推进。
- 任务、Artifact、血缘、审核、ChangeSet 的独立查询及操作路由。
- SSE 事件流、取消、重试和完整幂等接口。
- 真实文档解析、知识抽取、融合、质量评估和图谱发布算法。
