# 知识图谱构建 Agent 平台 V1

本项目是面向制造业知识图谱构建的后端平台。总控 Agent 识别用户的 `START / CONTINUE / MODIFY` 意图，将总任务拆成专业子任务并发布到同一个 `SharedContext`；五个专业 Agent 由独立 Worker 认领任务，在各自 allowlist 内调用 Skills，通过标准 Artifact 交换结果。没有 Python 资源的 Skill 以正文作为专业提示词；含 Python 资源的 Skill 由隔离 Runner 执行。候选本体和候选图谱必须经过用户审核后才能发布。

## 已实现能力

- 用户问题、文档上传、多轮继续/修改与已完成 Artifact 复用。
- DeepSeek 驱动的总控 Agent和五个专业 Agent。
- 数据源解析、知识抽取、知识融合、质量评估、图谱管理更新五类专业 Agent。
- 17 个注册化 Skill；接口、权限、输入输出、执行与 Artifact 落库均为正式实现，生产级知识算法仍持续增强。
- 总控一次生成完整 Agent 级计划，SharedContext 按依赖解锁；不同专业 Agent 的可执行任务可并行认领，全部终止后由总控检查最终目标。
- PostgreSQL 任务事实、版本化 TaskSpec/SharedContext、Assignment 租约与 fencing token。
- MinIO Artifact 内容、checksum、血缘、状态和容量控制。
- Mem0 Cloud 长期记忆存储、用户隔离语义召回和 SharedContext 轻量引用注入。
- 候选本体/图谱审核、ChangeSet、Approval 与发布门禁。
- PublishedGraph 在 MinIO 保留不可变原文，并同步写入 PostgreSQL 图版本、节点、边、属性和
  证据表；`publish_graph` 原样保留用户批准的 CandidateGraph，不让模型重写事实。
- 正式工作台与验证实验室共用本地 G6 候选图谱工作台：支持拖拽、缩放、布局、筛选、检索、
  属性/证据查看及 ChangeSet 草稿编辑；浏览器视图不修改 Artifact。
- 正式 FastAPI、SSE 事件、幂等请求、多轮消息、取消/重试、Registry 查询。
- 正式工作台与 8002 验证实验室均实时消费 SSE；支持事件游标续传和空闲心跳。
- 8002 验证实验室只接收真实业务文档、真实外部本体或纯文本任务；没有可用本体时由总控规划候选本体建设，并进入人工预览、ChangeSet 修改和确认门禁。
- 20 条制造业 Golden Dataset、准确率和安全门禁评测。
- 自定义脚本 Skill 的固定依赖运行环境、静态依赖扫描、隔离契约试运行和发布门禁；
  依赖或 Artifact 输出不兼容时在真实任务执行前阻断发布。
- Linux Docker Compose 交付。

## Agent 清单

当前共注册 **6 个 Agent**：1 个总控 Agent 和 5 个专业 Agent。

| Agent | 注册标识 | 主要职责 |
| --- | --- | --- |
| 意图识别与任务分配总控 Agent | `controller_agent` | 识别 `START / CONTINUE / MODIFY`，维护 TaskSpec，一次拆解完整 Agent 级计划并分配专业 Agent，最后检查目标是否完成。总控 Agent 不直接执行业务 Skill。 |
| 数据源解析 Agent | `document_parsing_agent` | 识别输入文档并生成文档清单、解析结果和语义文档。 |
| 知识抽取 Agent | `knowledge_extraction_agent` | 构建候选本体，基于已发布本体抽取实体、关系、属性和证据。 |
| 知识融合 Agent | `knowledge_fusion_agent` | 执行本体对齐与融合、实体对齐、关系标准化及冲突分析。 |
| 质量评估 Agent | `quality_evaluation_agent` | 评估候选本体、候选图谱和证据覆盖情况。 |
| 图谱管理更新 Agent | `graph_management_agent` | 构建候选图谱、生成增量 ChangeSet 草案、应用用户审核产生的 ChangeSet，并在 PublishGuard 校验通过后发布本体或图谱。 |

总控 Agent 的实现和 Prompt 位于 `src/kg_agent/agents/controller/` 与
`configs/prompts/controller/`；专业 Agent 的通用实现位于
`src/kg_agent/agents/professional/`，五个专业 Agent 的能力配置位于
`configs/agents/`。

## Skill 清单与权限

当前共注册 **17 个业务 Skill**。Skill 不划分 `LLM` 或 `SCRIPT` 模式，也不存在任务级模式开关。Registry 每个 Skill 只绑定一个当前执行器：无 Python 资源时加载 `SKILL.md` 正文作为提示词，含 Python 资源时进入隔离 Runner，内置专用代码则从同目录加载。

| 专业 Agent | 允许调用的 Skills | 功能 |
| --- | --- | --- |
| 数据源解析 Agent | `build_document_manifest` | 根据原始文档生成文档结构清单。 |
| 数据源解析 Agent | `parse_document` | 将原始文档解析为结构块和语义片段。 |
| 知识抽取 Agent | `extract_ontology_candidates` | 从语义文档归纳候选本体。 |
| 知识抽取 Agent | `extract_knowledge` | 基于语义文档和已发布本体，一次联合抽取实体、关系、属性与统一证据，并确定性核对引用和真实数量。 |
| 知识融合 Agent | `align_ontology` | 对齐候选本体与外部本体。 |
| 知识融合 Agent | `fuse_ontology` | 融合候选本体与平台已发布本体。 |
| 知识融合 Agent | `align_entities` | 执行实体消歧、去重和对齐。 |
| 知识融合 Agent | `normalize_relations` | 根据已发布本体标准化关系类型。 |
| 知识融合 Agent | `analyze_conflicts` | 分析实体、关系和证据之间的冲突，生成冲突报告。 |
| 质量评估 Agent | `evaluate_graph_quality` | 评估候选图谱及冲突处理质量。 |
| 质量评估 Agent | `evaluate_evidence_coverage` | 评估候选图谱的证据覆盖率。 |
| 图谱管理更新 Agent | `apply_ontology_changeset` | 将用户修改应用为新的候选本体 Artifact。 |
| 图谱管理更新 Agent | `publish_ontology` | 在审核和发布门禁通过后发布本体。 |
| 图谱管理更新 Agent | `build_candidate_graph` | 基于对齐后的实体、关系和冲突报告构建候选图谱。 |
| 图谱管理更新 Agent | `generate_graph_changeset` | 比对新增知识与候选图谱基线，生成待用户确认的增量图谱修改草案。 |
| 图谱管理更新 Agent | `apply_graph_changeset` | 将用户修改应用为新的候选图谱 Artifact。 |
| 图谱管理更新 Agent | `publish_graph` | 在审核和发布门禁通过后发布图谱。 |

每个 Skill 位于 `src/kg_agent/skills/catalog/<skill_name>/`，由 `SKILL.md` 的前置元数据和
正文执行说明定义。只有已有正式算法的 Skill 才保留 `implementation.py`；其余无脚本 Skill 由
专业 Agent 加载 `SKILL.md`、输入 Artifact 和输出 Schema 后调用模型。Agent 侧权限由
`configs/agents/*.yaml` 中的 `allowed_skills` 控制，Skill 侧反向权限仅由全局
`configs/skills/v1.yaml` 中的 Skill—Agent 映射控制。系统启动时
`SkillRegistry` 会检查双向权限、Artifact 输入输出能力和生命周期是否一致；运行时
`ProfessionalPlanValidator` 会拒绝专业 Agent 越权调用 Skill。发布类 Skill 还必须通过
人工审核与 `PublishGuard`，仅配置 Skill 权限不能绕过发布门禁。

Registry 扫描所有 `SKILL.md`，并仅在目录存在 `implementation.py` 时加载专用算法；否则使用
提示词执行流程。模型调用的开始、结束与端到端耗时会写入任务 JSONL 的 `MODEL_CALL` 记录。

## 已有 Python 脚本创建 Skill

在正式工作台或 8002 验证实验室打开“自定义能力 → 新增自定义 Skill”，填写用途、
上传原脚本，并确认系统推荐的业务使用位置。脚本已提供
`run(inputs, parameters)` 和标准 `outputs` 时直接试运行。实体转换位置也支持
已有的单参数业务函数（例如 `main(device_name)`）：确认函数、参数与返回的名称
字段后，平台另存 `_kg_skill_entry.py`，不改动用户原有的 `main.py`。
函数可直接返回非空名称字符串，或返回含已确认字段的字典。

发布前须完成固定依赖扫描与隔离 Runner 试运行；通过后再创建/授权自定义 Agent，
总控才可能将该 Agent 安排在合适的上下游子任务之间。生成入口和原脚本同属
不可变 Skill 修订，编辑文件后须重新试运行。零参数、只读命令行/本机文件并打印
结果的 `main()` 不会自动转换；其他业务函数适配目前仍要求标准 `run` 入口。

## 启动

1. 复制 `.env.example` 为 `.env`，填写 `DEEPSEEK_API_KEY` 和模型；需要长期记忆时再填写服务端 `MEM0_API_KEY` 并启用 Mem0。
2. 执行 `docker compose up --build -d`。
3. 检查 `curl http://localhost:8000/health/ready`。
4. OpenAPI 位于 `http://localhost:8000/docs`，MinIO 控制台位于 `http://localhost:9001`。

API 只提交和查询任务，`worker` 服务独立执行专业 Agent。不要使用保留的历史 demo 工厂作为生产入口。

### 不使用 Docker 的本机 uv 联调

本机模式把 8000/Worker 数据保存到 `.local-data/formal`，把 8002 数据保存到
`.local-data/simulation`。进程可使用项目 `.venv` 中的 uvicorn/python 启动；需同时设置
`LOCAL_UV_DATA_DIR` 或 `SIMULATION_LOCAL_UV_DATA_DIR`。该模式使用 SQLite 和受控本地 Artifact
目录，只供单用户联调，不能替代正式 PostgreSQL/MinIO 部署。带 Python 资源的 Skill 不会降级到宿主机
执行；没有独立 Runner 时会明确失败。

正式端任务级公开日志保存在 `logs/formal/task-<task_id>.jsonl`，也可通过
`GET /api/v1/tasks/{task_id}/logs` 下载。8002 验证实验室日志保存在
`logs/simulation/task-<task_id>.jsonl`，下载接口为
`GET /api/v1/simulations/{task_id}/logs`。PostgreSQL 事件和 ExecutionTrace 仍是权威记录。
测试端实时流为 `GET /api/v1/simulations/{task_id}/events/stream`；正式端实时流为
`GET /api/v1/tasks/{task_id}/events/stream`。

## 本地质量检查

```bash
uv sync --dev
uv run ruff check src tests migrations
uv run mypy src/kg_agent
uv run pytest
uv run python -m kg_agent.evaluation.cli \
  --golden evaluation/golden_dataset.jsonl \
  --predictions evaluation/baseline_predictions.jsonl \
  --enforce-thresholds
```

详细接口见 `docs/api.md`，部署和验收见 `docs/deployment.md`。架构与决策仍以 `AGENTS.md` 规定的事实来源优先级为准。
