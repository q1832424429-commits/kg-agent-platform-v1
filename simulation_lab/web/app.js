/**
 * Agent 人工观察台前端控制器。
 * 页面只显示隔离 API 返回的公开结构化日志，不执行自动评分或正确性判断。
 */

"use strict";

// 保存当前实验上下文，便于第二轮人工观察 CONTINUE 或 MODIFY。
const state = {
  taskId: null,
  conversationId: null,
  report: null,
  activeTab: "intent",
  eventSource: null,
  liveLogCount: 0,
  liveProgress: { total: 0, completed: 0, percent: 0 },
  reviewId: null,
  presentedPlanRevision: null,
  planSubtaskOrder: [],
  // 运行记录使用不可变 key；这里仅把它们翻译成用户创建时填写的业务名称。
  customAgentNames: {},
  customSkillNames: {},
};

const SESSION_TASK_KEY = "kg-agent-simulation-task-id";
const SESSION_CONVERSATION_KEY = "kg-agent-simulation-conversation-id";

/** 保存当前任务身份；刷新页面后仍可向同一 SharedContext 发送下一轮消息。 */
function persistTaskIdentity() {
  if (state.taskId && state.conversationId) {
    window.sessionStorage.setItem(SESSION_TASK_KEY, state.taskId);
    window.sessionStorage.setItem(SESSION_CONVERSATION_KEY, state.conversationId);
  }
}

/** 从当前浏览器会话恢复任务身份；用户主动清空时才创建新的 MasterTask。 */
function restoreTaskIdentity() {
  state.taskId = window.sessionStorage.getItem(SESSION_TASK_KEY);
  state.conversationId = window.sessionStorage.getItem(SESSION_CONVERSATION_KEY);
  if (state.taskId && state.conversationId) {
    dom.runMode.textContent = `继续当前实验 · ${state.taskId.slice(0, 8)}`;
  } else {
    state.taskId = null;
    state.conversationId = null;
  }
}

// 缓存固定页面节点，后续渲染仅消费服务端返回的公开事实。
const dom = {
  form: document.querySelector("#simulation-form"),
  question: document.querySelector("#question"),
  documentInput: document.querySelector("#document-input"),
  documentFileLabel: document.querySelector("#document-file-label"),
  documentFileList: document.querySelector("#document-file-list"),
  ontologyInput: document.querySelector("#ontology-input"),
  ontologyFileLabel: document.querySelector("#ontology-file-label"),
  publishedOntologySelect: document.querySelector("#published-ontology-select"),
  publishedGraphSelect: document.querySelector("#published-graph-select"),
  refreshAssetsButton: document.querySelector("#refresh-assets-button"),
  runButton: document.querySelector("#run-button"),
  resetButton: document.querySelector("#reset-button"),
  runMode: document.querySelector("#run-mode"),
  healthStatus: document.querySelector("#health-status"),
  progressRing: document.querySelector("#progress-ring"),
  progressValue: document.querySelector("#progress-value"),
  progressStage: document.querySelector("#progress-stage"),
  progressDetail: document.querySelector("#progress-detail"),
  planReviewPanel: document.querySelector("#plan-review-panel"),
  logCount: document.querySelector("#log-count"),
  logStream: document.querySelector("#log-stream"),
  evidenceContent: document.querySelector("#evidence-content"),
  planReviewDialog: document.querySelector("#plan-review-dialog"),
  planReviewMaterials: document.querySelector("#plan-review-materials"),
  planReviewFeedback: document.querySelector("#plan-review-feedback"),
  reviewDialog: document.querySelector("#review-dialog"),
  reviewMaterials: document.querySelector("#review-materials"),
  reviewTitle: document.querySelector("#review-title"),
  reviewTitleLabel: document.querySelector("#review-title-label"),
  reviewFeedback: document.querySelector("#review-feedback"),
  reviewFeedbackLabel: document.querySelector("#review-feedback-label"),
  reviewOperations: document.querySelector("#review-operations"),
  reviewOperationsLabel: document.querySelector("#review-operations-label"),
  reviewActions: document.querySelector("#review-actions"),
  toast: document.querySelector("#toast"),
};

/** 转义用户和 Agent 输出，防止日志文本被当作 HTML 执行。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 将内置或用户自定义 Agent 类型翻译成业务用户可读的名称。 */
function agentLabel(value) {
  const builtin = {
    controller_agent: "总控 Agent",
    document_parsing_agent: "数据源解析 Agent",
    knowledge_extraction_agent: "知识抽取 Agent",
    knowledge_fusion_agent: "知识融合 Agent",
    quality_evaluation_agent: "质量评估 Agent",
    graph_management_agent: "图谱管理更新 Agent",
    assignment_service: "任务分配服务",
    simulation_lab: "验证实验室",
  };
  return state.customAgentNames[value]
    ? capabilityLabel(state.customAgentNames[value], "Agent")
    : builtin[value] || value || "—";
}

/** 为自定义能力补齐类型后缀；用户已填写后缀时去重，保持所有页面表达一致。 */
function capabilityLabel(name, kind) {
  const normalized = String(name || "").trim().replace(/\s+(Agent|Skill)$/i, "");
  return normalized ? `${normalized} ${kind}` : kind;
}

/** 返回用户填写的 Skill 名称；内部 key 只在可展开的原始技术详情中保留。 */
function skillLabel(value) {
  const builtin = {
    parse_document: "文档解析 Skill",
    build_document_manifest: "文档结构整理 Skill",
    extract_ontology_candidates: "候选本体抽取 Skill",
    align_ontology: "本体对齐 Skill",
    fuse_ontology: "本体融合 Skill",
    apply_ontology_changeset: "本体修改应用 Skill",
    publish_ontology: "本体发布 Skill",
    extract_knowledge: "知识抽取 Skill",
    align_entities: "实体对齐 Skill",
    normalize_relations: "关系标准化 Skill",
    analyze_conflicts: "冲突分析 Skill",
    build_candidate_graph: "候选图谱构建 Skill",
    evaluate_evidence_coverage: "证据覆盖评估 Skill",
    evaluate_graph_quality: "图谱质量评估 Skill",
    generate_graph_changeset: "图谱增量变更 Skill",
    apply_graph_changeset: "图谱修改应用 Skill",
    publish_graph: "图谱发布 Skill",
  };
  return state.customSkillNames[value]
    ? capabilityLabel(state.customSkillNames[value], "Skill")
    : builtin[value] || value || "—";
}

/** Worker 身份不是独立 Agent；把内部执行器名称映射回它代表的业务 Agent。 */
function actorLabel(value) {
  const workerPrefix = "demo-worker-";
  if (String(value || "").startsWith(workerPrefix)) {
    return `${agentLabel(String(value).slice(workerPrefix.length))} 执行器`;
  }
  return agentLabel(value);
}

/** 将服务端审计日志中的内部 key 转为前端可读消息，原始详情仍保持可追溯。 */
function logMessage(entry) {
  const details = entry.details || {};
  if (entry.action === "RECALL_LONG_TERM_MEMORY") {
    const memories = details.memories || [];
    if (!memories.length) return entry.message || "本轮没有召回符合阈值的长期记忆。";
    const summaries = memories.map((item, index) => {
      const score = item.score ? `，相关度 ${item.score}` : "";
      return `${index + 1}. ${item.summary || "无摘要"}${score}`;
    });
    return `本轮召回 ${memories.length} 条长期记忆：${summaries.join("；")}`;
  }
  if (entry.action === "CREATE_ARTIFACT" && details.producer_skill) {
    return `Skill 产物：${skillLabel(details.producer_skill)} 生成 ${details.artifact_type || "Artifact"}。`;
  }
  if (entry.action === "INVOKE_SKILL" && details.skill_name) {
    return `调用 Skill（顺序 ${details.skill_order || "—"}）：${skillLabel(details.skill_name)}。`;
  }
  if (entry.action === "CREATE_ASSIGNMENT" && details.agent_type) {
    return `将子任务分配给 ${agentLabel(details.agent_type)}。`;
  }
  if (entry.action === "CLAIM_ASSIGNMENT") {
    return `${actorLabel(entry.actor)} 已认领子任务。`;
  }
  if (entry.action === "RUN_SUBTASK") {
    return `${actorLabel(entry.actor)} 完成子任务，AgentRun 状态为 ${entry.status || "未知"}。`;
  }
  return entry.message || "";
}

/** 统一显示计时操作名，避免“Skill：内部 key”泄露到用户可见计时卡。 */
function timingOperationLabel(value) {
  const prefix = "Skill：";
  return String(value || "").startsWith(prefix)
    ? `${prefix}${skillLabel(String(value).slice(prefix.length))}`
    : value || "—";
}

/** 将日志阶段翻译为人工检查使用的短标签。 */
function phaseLabel(value) {
  return {
    INPUT: "任务输入", INTENT: "意图", TASK_SPEC: "任务目标",
    MEMORY: "长期记忆",
    COORDINATION: "依赖调度", DECOMPOSITION: "任务拆解", ASSIGNMENT: "任务分配", CLAIM: "任务认领",
    PLAN_REVIEW: "人工计划审核",
    AGENT_RUN: "Agent 执行", SKILL: "Skill 调用", TIMING: "运行耗时", ARTIFACT: "产物", COMPLETE: "结束",
  }[value] || value;
}

/** 调用隔离实验室 API，并把结构化错误转换为可读提示。 */
async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let message = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = await response.json();
      message = payload.message || payload.detail || message;
      if (Array.isArray(message)) message = message.map((item) => item.msg).join("；");
    } catch (_) {
      // 非 JSON 响应保留通用 HTTP 提示。
    }
    throw new Error(message);
  }
  return response.json();
}

/** 显示短时非阻塞消息。 */
function showToast(message, isError = false) {
  dom.toast.textContent = message;
  dom.toast.classList.toggle("is-error", isError);
  dom.toast.hidden = false;
  window.setTimeout(() => { dom.toast.hidden = true; }, 4200);
}

/** 检查当前页面连接的确实是隔离实验室服务。 */
async function checkHealth() {
  try {
    const result = await api("/health");
    // 后端固定返回 isolated-agent-validation；此前前端误比较为不存在的 simulation 值，
    // 导致健康服务实际正常时仍显示“模式异常”。
    dom.healthStatus.textContent = result.mode === "isolated-agent-validation" ? "观察台已连接" : "模式异常";
    dom.healthStatus.className = "health-pill is-online";
  } catch (_) {
    dom.healthStatus.textContent = "观察台未连接";
    dom.healthStatus.className = "health-pill is-error";
  }
}

/** 加载测试端隔离的自定义能力目录，只用于前端显示名称映射。 */
async function loadCustomCapabilityNames() {
  try {
    const [agents, skills] = await Promise.all([
      api("/api/v1/extensions/agents"),
      api("/api/v1/extensions/skills"),
    ]);
    state.customAgentNames = Object.fromEntries(
      agents.map((item) => [item.agent_key, item.name]),
    );
    state.customSkillNames = Object.fromEntries(
      skills.map((item) => [item.skill_key, item.name]),
    );
    if (state.report) {
      renderEvidence();
      renderPlanReview();
    }
  } catch (_) {
    // 目录加载失败不影响测试任务、SSE 或人工审核，页面会回退显示 key。
  }
}

/** 更新任务完成百分比和当前阶段，仅反映实际执行状态。 */
function setProgress(percent, stage, detail = "") {
  const safePercent = Math.max(0, Math.min(100, Math.round(percent)));
  dom.progressRing.style.setProperty("--progress", safePercent);
  dom.progressValue.textContent = `${safePercent}%`;
  dom.progressStage.textContent = stage;
  if (detail) dom.progressDetail.textContent = detail;
}

/** 清空实验上下文，下一次问题将创建新的 MasterTask。 */
function resetExperiment() {
  stopSimulationStream();
  state.taskId = null;
  state.conversationId = null;
  state.report = null;
  state.reviewId = null;
  state.presentedPlanRevision = null;
  state.planSubtaskOrder = [];
  window.sessionStorage.removeItem(SESSION_TASK_KEY);
  window.sessionStorage.removeItem(SESSION_CONVERSATION_KEY);
  state.liveLogCount = 0;
  state.liveProgress = { total: 0, completed: 0, percent: 0 };
  dom.documentInput.value = "";
  dom.ontologyInput.value = "";
  dom.publishedOntologySelect.value = "";
  dom.publishedGraphSelect.value = "";
  renderDocumentFiles();
  renderOntologyFile();
  dom.runMode.textContent = "新实验任务";
  dom.logCount.textContent = "0 条";
  dom.logCount.className = "overall-badge is-idle";
  dom.logStream.innerHTML = '<div class="empty-state"><span>⌁</span><strong>等待运行</strong><p>日志将严格按照公开业务事件顺序编号。</p></div>';
  dom.planReviewPanel.hidden = true;
  dom.planReviewPanel.innerHTML = "";
  dom.runButton.disabled = false;
  dom.runButton.querySelector("span").textContent = "运行并输出日志";
  if (dom.planReviewDialog.open) dom.planReviewDialog.close();
  setProgress(0, "等待真实输入", "运行后将按顺序输出意图、拆解、分配、认领、Agent、Skills 和 Artifacts。");
  renderEvidence();
}

/** 展示当前选择的业务文件；真实内容仍由隔离 Artifact 服务保存和校验。 */
function renderDocumentFiles() {
  const files = [...(dom.documentInput.files || [])];
  dom.documentFileLabel.textContent = files.length
    ? `已选择 ${files.length} 个业务文件`
    : "选择一个或多个真实文件";
  dom.documentFileList.innerHTML = files
    .map((file) => `<span>${escapeHtml(file.name)}</span>`)
    .join("");
}

/** 展示用户是否选择了真实外部本体文件。 */
function renderOntologyFile() {
  const file = dom.ontologyInput.files?.[0] || null;
  dom.ontologyFileLabel.textContent = file ? file.name : "选择本体 JSON";
}

/** 加载测试环境内真实发布且可跨任务复用的资产。 */
async function loadPublishedAssets() {
  try {
    const [ontologies, graphs] = await Promise.all([
      api("/api/v1/published-assets/ontologies"),
      api("/api/v1/published-assets/graphs"),
    ]);
    dom.publishedOntologySelect.innerHTML = '<option value="">不选择</option>' + ontologies.map((item) =>
      `<option value="${escapeHtml(item.artifact_id)}">${escapeHtml(item.publication_title || item.content_summary || item.ontology_version || item.artifact_id)}</option>`
    ).join("");
    dom.publishedGraphSelect.innerHTML = '<option value="">不选择</option>' + graphs.map((item) =>
      `<option value="${escapeHtml(item.artifact_id)}">${escapeHtml(item.publication_title || item.content_summary || item.artifact_id)}</option>`
    ).join("");
  } catch (error) {
    showToast(`已发布资产加载失败：${error.message}`, true);
  }
}

/** 提交用户问题、真实业务文件与真实外部本体。 */
async function runSimulation(event) {
  event.preventDefault();
  stopSimulationStream();
  dom.runButton.disabled = true;
  dom.runButton.querySelector("span").textContent = "Agent 运行中…";
  dom.logStream.innerHTML = `<div class="empty-state"><span>⋯</span><strong>正在调用真实 Agent</strong><p>等待总控拆解和专业 Agent 的 Skill 结果。</p></div>`;
  setProgress(5, "正在接收任务输入", "已上传的真实文件会登记为 Artifact；未上传文件时按纯文本任务处理。");
  const previousTaskId = state.taskId;
  const previousConversationId = state.conversationId;
  try {
    // 先生成 ID 并建立 SSE，再提交耗时任务，避免漏掉 Mem0 和总控开始事件。
    state.taskId = state.taskId || crypto.randomUUID();
    state.conversationId = state.conversationId || crypto.randomUUID();
    persistTaskIdentity();
    state.liveLogCount = 0;
    state.liveProgress = { total: 0, completed: 0, percent: 5 };
    await startSimulationStream(state.taskId);
    const body = new FormData();
    body.append("question", dom.question.value.trim());
    body.append("max_worker_runs", "30");
    [...(dom.documentInput.files || [])].forEach((file) => body.append("files", file));
    const ontology = dom.ontologyInput.files?.[0] || null;
    if (ontology) body.append("ontology_file", ontology);
    if (dom.publishedOntologySelect.value) {
      body.append("published_ontology_artifact_id", dom.publishedOntologySelect.value);
    }
    if (dom.publishedGraphSelect.value) {
      body.append("published_graph_artifact_id", dom.publishedGraphSelect.value);
    }
    body.append("task_id", state.taskId);
    body.append("conversation_id", state.conversationId);
    state.report = await api("/api/v1/simulations/run", { method: "POST", body });
    state.taskId = state.report.observed.task_id;
    state.conversationId = state.report.observed.conversation_id;
    persistTaskIdentity();
    dom.documentInput.value = "";
    dom.ontologyInput.value = "";
    dom.publishedOntologySelect.value = "";
    dom.publishedGraphSelect.value = "";
    renderDocumentFiles();
    renderOntologyFile();
    dom.runMode.textContent = `继续当前实验 · ${state.taskId.slice(0, 8)}`;
    renderCompletedLogs();
    dom.healthStatus.textContent = "观察台已连接";
    dom.healthStatus.className = "health-pill is-online";
  } catch (error) {
    state.taskId = previousTaskId;
    state.conversationId = previousConversationId;
    if (state.taskId && state.conversationId) {
      persistTaskIdentity();
    } else {
      window.sessionStorage.removeItem(SESSION_TASK_KEY);
      window.sessionStorage.removeItem(SESSION_CONVERSATION_KEY);
    }
    dom.logStream.innerHTML = `<div class="empty-state"><span>!</span><strong>运行失败</strong><p>${escapeHtml(error.message)}</p></div>`;
    setProgress(0, "实验运行失败", error.message);
    showToast(error.message, true);
  } finally {
    stopSimulationStream();
    const waitingForPlanReview = state.report?.plan_review?.status === "PENDING";
    dom.runButton.disabled = waitingForPlanReview;
    dom.runButton.querySelector("span").textContent = waitingForPlanReview
      ? "请先审核当前计划"
      : "运行并输出日志";
  }
}

/** 按计划内部依赖计算执行阶段，并在同一阶段内保留用户当前的优先级选择。 */
function planStages(subtasks, preferredOrder = []) {
  const byId = new Map(subtasks.map((item) => [item.subtask_id, item]));
  const preferredIndex = new Map(preferredOrder.map((id, index) => [id, index]));
  const pending = new Map(byId);
  const completed = new Set();
  const stages = [];
  while (pending.size) {
    const ready = [...pending.values()].filter((item) =>
      (item.dependency_subtask_ids || []).every(
        (dependencyId) => !byId.has(dependencyId) || completed.has(dependencyId),
      ),
    );
    // 候选计划已由后端校验为无环；此保护仅防止异常数据让浏览器审核弹窗无限循环。
    const current = ready.length ? ready : [...pending.values()];
    current.sort(
      (left, right) =>
        (preferredIndex.get(left.subtask_id) ?? Number.MAX_SAFE_INTEGER) -
        (preferredIndex.get(right.subtask_id) ?? Number.MAX_SAFE_INTEGER),
    );
    stages.push(current);
    current.forEach((item) => {
      pending.delete(item.subtask_id);
      completed.add(item.subtask_id);
    });
  }
  return stages;
}

/** 返回满足依赖的默认审核顺序，确保弹窗初次展示即等于真实可执行顺序。 */
function topologicalPlanOrder(subtasks) {
  return planStages(subtasks).flat().map((item) => item.subtask_id);
}

/** 绘制带阶段标题的任务卡；跨阶段不会提供排序按钮。 */
function renderPlanStages(stages, { editable = false } = {}) {
  return stages.map((items, stageIndex) => `
    <section class="plan-review-stage">
      <p class="field-note">阶段 ${stageIndex + 1} · ${stageIndex === 0 ? "可立即执行" : "等待前置阶段产物"}</p>
      <div class="plan-review-tasks">
        ${items.map((item, itemIndex) => `
          <div class="plan-review-task">
            <span>${itemIndex + 1}</span>
            <strong>${escapeHtml(item.name)}</strong>
            <small>${escapeHtml(agentLabel(item.assigned_agent_type))}</small>
            ${editable ? `<span class="plan-order-buttons">
              <button type="button" data-plan-move="up" data-subtask-id="${escapeHtml(item.subtask_id)}" ${itemIndex === 0 ? "disabled" : ""}>↑</button>
              <button type="button" data-plan-move="down" data-subtask-id="${escapeHtml(item.subtask_id)}" ${itemIndex === items.length - 1 ? "disabled" : ""}>↓</button>
            </span>` : ""}
          </div>`).join("")}
      </div>
    </section>`).join("");
}

/** 在总控发布完整子任务后展示人工执行许可，不替用户自动判定。 */
function renderPlanReview() {
  const review = state.report?.plan_review;
  const subtasks = state.report?.observed?.subtasks || [];
  if (!review) {
    dom.planReviewPanel.hidden = true;
    dom.planReviewPanel.innerHTML = "";
    return;
  }
  const pending = review.status === "PENDING";
  const terminalStatuses = new Set(["SUCCEEDED", "FAILED", "CANCELLED"]);
  const hasUnfinishedSubtasks = subtasks.some((item) => !terminalStatuses.has(item.status));
  const hasPendingArtifactReview = (state.report.reviews || []).some(
    (item) => item.status === "PENDING",
  );
  dom.runButton.disabled = pending || hasUnfinishedSubtasks;
  dom.runButton.querySelector("span").textContent = pending
    ? "请先审核当前计划"
    : hasUnfinishedSubtasks
    ? "请先完成当前任务"
    : "运行并输出日志";
  dom.planReviewPanel.hidden = false;
  dom.planReviewPanel.classList.toggle("is-approved", !pending);
  dom.planReviewPanel.innerHTML = `
    <div class="plan-review-heading">
      <strong>候选意图与子任务发布审核</strong>
      <span>${pending ? "尚未发布" : "已批准并发布"} · 修订 ${review.decomposition_revision}</span>
    </div>
    <p><strong>识别意图：</strong>${escapeHtml(review.task_intent)}</p>
    <p><strong>业务目标：</strong>${escapeHtml(review.business_goal)}</p>
    ${renderPlanStages(planStages(subtasks, state.planSubtaskOrder))}
    ${pending
      ? '<div class="plan-review-actions"><button class="secondary-button" data-open-plan-review type="button">打开审核与修改窗口</button></div>'
      : hasUnfinishedSubtasks && !hasPendingArtifactReview
      ? '<button class="run-button" data-resume-approved-plan type="button">继续执行已批准计划</button>'
      : ""}
  `;
  if (pending && state.presentedPlanRevision !== review.decomposition_revision) {
    state.presentedPlanRevision = review.decomposition_revision;
    state.planSubtaskOrder = topologicalPlanOrder(subtasks);
    window.queueMicrotask(openPlanReviewDialog);
  }
}

/** 弹出当前人工计划，用户可以批准，也可以用自然语言要求总控修改。 */
function openPlanReviewDialog() {
  const review = state.report?.plan_review;
  if (!review || review.status !== "PENDING") return;
  const subtasks = state.report?.observed?.subtasks || [];
  if (!state.planSubtaskOrder.length) {
    state.planSubtaskOrder = topologicalPlanOrder(subtasks);
  }
  renderPlanReviewMaterials();
  dom.planReviewFeedback.value = "";
  if (!dom.planReviewDialog.open) dom.planReviewDialog.showModal();
}

/** 按用户当前优先级重绘候选计划；展示和操作都严格保留真实依赖阶段。 */
function renderPlanReviewMaterials() {
  const review = state.report?.plan_review;
  if (!review) return;
  const subtasks = state.report?.observed?.subtasks || [];
  const stages = planStages(subtasks, state.planSubtaskOrder);
  dom.planReviewMaterials.innerHTML = `
    <div class="kv"><span>计划修订</span><span>${review.decomposition_revision}</span></div>
    <div class="kv"><span>发布状态</span><span>尚未发布</span></div>
    <div class="kv"><span>识别意图</span><span>${escapeHtml(review.task_intent)}</span></div>
    <p>${escapeHtml(review.business_goal)}</p>
    <p class="field-note">只能调整同一执行阶段内的优先顺序；前置依赖阶段固定在前。</p>
    ${renderPlanStages(stages, { editable: true })}`;
}

/** 上移或下移一个候选子任务，只改变同一依赖阶段内的获批执行优先级。 */
function movePlanSubtask(subtaskId, direction) {
  const subtasks = state.report?.observed?.subtasks || [];
  const stages = planStages(subtasks, state.planSubtaskOrder);
  const stage = stages.find((items) => items.some((item) => item.subtask_id === subtaskId));
  if (!stage) return;
  const index = stage.findIndex((item) => item.subtask_id === subtaskId);
  const target = direction === "up" ? index - 1 : index + 1;
  if (target < 0 || target >= stage.length) return;
  [stage[index], stage[target]] = [stage[target], stage[index]];
  state.planSubtaskOrder = stages.flat().map((item) => item.subtask_id);
  renderPlanReviewMaterials();
}

/** 批准当前 decomposition revision，然后才允许专业 Agent 认领 Assignment。 */
async function approvePlanAndExecute() {
  if (!state.taskId || state.report?.plan_review?.status !== "PENDING") return;
  const button = document.querySelector("#approve-plan-button");
  if (button) {
    button.disabled = true;
    button.textContent = "已批准，正在启动专业 Agent…";
  }
  try {
    state.liveLogCount = 0;
    await startSimulationStream(state.taskId);
    dom.planReviewDialog.close();
    setProgress(22, "计划已批准，正在执行", "专业 Agent 开始认领已发布的子任务。");
    const result = await api(
      `/api/v1/simulations/${encodeURIComponent(state.taskId)}/plan-review/approve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          reviewer_id: "simulation-user",
          ordered_subtask_ids: state.planSubtaskOrder,
        }),
      },
    );
    state.report.plan_review = result.plan_review;
    state.report.observed = result.observed;
    state.report.reviews = result.reviews || [];
    state.report.timings = result.timings || [];
    state.report.logs = result.logs || [];
    state.report.progress = result.progress;
    state.planSubtaskOrder = [];
    renderCompletedLogs();
    showToast(`计划已批准；下一状态：${result.next_action}`);
  } catch (error) {
    showToast(error.message, true);
    // 批准接口在“计划已发布、Worker 启动”后仍可能因执行异常返回 5xx。
    // 此时不能把本地状态盲目退回待审核，否则同一 decomposition 会再弹一次。
    // 重新读取服务端事实：若计划已发布，页面直接进入执行/恢复状态；
    // 只有服务端仍明确返回 PENDING 时才保留审核窗口。
    try {
      state.report = await api(
        `/api/v1/simulations/${encodeURIComponent(state.taskId)}`,
      );
      state.planSubtaskOrder = [];
      renderCompletedLogs();
      if (state.report?.plan_review?.status === "PENDING") {
        openPlanReviewDialog();
      } else {
        showToast("计划已发布，已按服务端状态恢复页面。");
      }
    } catch (refreshError) {
      // 连快照也无法读取时保留当前界面，不自动重弹审核窗。
      showToast(`状态恢复失败：${refreshError.message}`, true);
    }
  } finally {
    stopSimulationStream();
    button.disabled = false;
    button.textContent = "批准并开始执行";
  }
}

/** 把审核意见作为同一任务的新用户轮次交回总控，得到的新计划仍需再次审核。 */
async function revisePlan() {
  if (!state.taskId || state.report?.plan_review?.status !== "PENDING") return;
  const feedback = dom.planReviewFeedback.value.trim();
  if (!feedback) {
    showToast("请先填写需要修改的意图或子任务意见。", true);
    dom.planReviewFeedback.focus();
    return;
  }
  const button = document.querySelector("#revise-plan-button");
  button.disabled = true;
  button.textContent = "总控正在重新规划…";
  try {
    state.liveLogCount = 0;
    await startSimulationStream(state.taskId);
    dom.planReviewDialog.close();
    setProgress(15, "正在根据审核意见重新规划", "总控 Agent 正在生成新的意图和完整子任务计划。");
    state.report = await api(
      `/api/v1/simulations/${encodeURIComponent(state.taskId)}/plan-review/revise`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ reviewer_id: "simulation-user", feedback }),
      },
    );
    state.presentedPlanRevision = null;
    renderCompletedLogs();
    showToast("总控已根据修改意见发布新计划，请再次审核。");
  } catch (error) {
    showToast(error.message, true);
    openPlanReviewDialog();
  } finally {
    stopSimulationStream();
    button.disabled = false;
    button.textContent = "提交修改并重新规划";
  }
}

/** 任务结束后一次渲染服务端完整报告，替换实时阶段中的精简诊断记录。 */
function renderCompletedLogs() {
  const logs = [...state.report.logs].sort((a, b) => a.sequence - b.sequence);
  dom.logStream.innerHTML = "";
  dom.logCount.textContent = `${logs.length} 条`;
  dom.logCount.className = "overall-badge is-pass";
  logs.forEach(appendLog);
  const progress = state.report.progress;
  const progressDetail = progress.stage === "等待意图与子任务审核"
    ? "请审核总控识别的意图和完整子任务计划；批准前专业 Agent 不会认领任务。"
    : progress.stage === "等待人工审核"
    ? `${progress.completed_subtasks}/${progress.total_subtasks} 个子任务完成；请打开“人工审核”标签预览、修改或批准候选本体/图谱。`
    : `${progress.completed_subtasks}/${progress.total_subtasks} 个子任务完成；请查看执行日志与产物。`;
  setProgress(
    progress.percent,
    progress.stage,
    progressDetail,
  );
  renderPlanReview();
  renderEvidence();
}

/** 建立测试端 SSE；连接成功后才允许任务提交，保证首条公开阶段日志可见。 */
function startSimulationStream(taskId) {
  return new Promise((resolve, reject) => {
    const source = new EventSource(
      `/api/v1/simulations/${encodeURIComponent(taskId)}/events/stream?tail=true`,
    );
    state.eventSource = source;
    let settled = false;
    const timeoutId = window.setTimeout(() => {
      if (settled) return;
      settled = true;
      source.close();
      reject(new Error("实时进度连接超时，请确认测试端 SSE 服务可用。"));
    }, 5000);
    const finishOpen = () => {
      if (settled) return;
      settled = true;
      window.clearTimeout(timeoutId);
      dom.healthStatus.textContent = "实时进度已连接";
      dom.healthStatus.className = "health-pill is-online";
      resolve();
    };
    source.onopen = finishOpen;
    source.addEventListener("run_log", (event) => {
      if (state.eventSource !== source) return;
      try {
        appendLiveRecord(JSON.parse(event.data), Number(event.lastEventId) || 0);
      } catch (_) {
        showToast("收到一条无法解析的实时日志，已跳过。", true);
      }
    });
    source.onerror = () => {
      dom.healthStatus.textContent = "实时流重连中";
      dom.healthStatus.className = "health-pill is-error";
    };
  });
}

/** 关闭本轮测试 SSE；任务最终报告仍由原有 POST 响应返回。 */
function stopSimulationStream() {
  if (state.eventSource) state.eventSource.close();
  state.eventSource = null;
}

/** 将一条公开 JSONL 事实映射成实时观察日志和当前进度，不做正确性评分。 */
function appendLiveRecord(record, sequence) {
  const payload = record.payload || {};
  const type = record.record_type || "UNKNOWN";
  const mappings = {
    ORCHESTRATION_STARTED: ["INPUT", "START_ORCHESTRATION", "RUNNING", "已接收任务，开始执行本轮 Agent 流程。"],
    MEMORY_RECALL: ["MEMORY", "RECALL_LONG_TERM_MEMORY", payload.status === "FAILED" ? "WARNING" : "SUCCESS", `长期记忆召回结束，共 ${payload.recalled_count || 0} 条。`],
    COMPLETED_EXPERIENCE_WRITE_STARTED: ["MEMORY", "WRITE_COMPLETED_EXPERIENCE", "RUNNING", "当前用户目标已完成，正在聚合整轮成功经验。"],
    COMPLETED_EXPERIENCE_WRITE: ["MEMORY", "CONSOLIDATE_COMPLETED_EXPERIENCE", "SUCCESS", `整轮长期经验处理完成：${payload.action || "UNKNOWN"}，相似度 ${payload.similarity ?? "无候选"}。`],
    COMPLETED_EXPERIENCE_SKIPPED: ["MEMORY", "SKIP_COMPLETED_EXPERIENCE", "WARNING", `整轮长期经验未写入：${payload.reason || "UNKNOWN"}。`],
    COMPLETED_EXPERIENCE_WRITE_FAILED: ["MEMORY", "WRITE_COMPLETED_EXPERIENCE_FAILED", "WARNING", `整轮长期经验写入失败：${payload.error_type || "UNKNOWN"}；任务成功状态不受影响。`],
    CONTROLLER_STARTED: ["INTENT", "START_CONTROLLER", "RUNNING", "总控 Agent 正在识别意图并拆解完整任务计划。"],
    CONTROLLER_RUN: ["DECOMPOSITION", "COMPLETE_CONTROLLER", "SUCCESS", `总控 Agent 已完成，发布 ${(payload.subtasks || []).length} 个子任务。`],
    PLAN_REVIEW_REQUESTED: ["PLAN_REVIEW", "REVIEW_AGENT_PLAN", "WARNING", "候选意图与子任务已生成，批准前尚未发布。"],
    PLAN_REVIEW_APPROVED: ["PLAN_REVIEW", "APPROVE_AGENT_PLAN", "SUCCESS", "用户已批准意图与子任务，允许专业 Agent 认领。"],
    PLAN_REVIEW_REVISION_REQUESTED: ["PLAN_REVIEW", "REVISE_AGENT_PLAN", "RUNNING", "用户已提交修改意见，总控正在发布新计划。"],
    HUMAN_REVIEW_RESOLVED: ["COORDINATION", "RESOLVE_HUMAN_REVIEW", "SUCCESS", `人工审核已提交：${payload.target_type || "候选产物"} ${payload.status || "已处理"}。`],
    REVIEW_RESUME_STARTED: ["COORDINATION", "RESUME_AFTER_REVIEW", "RUNNING", "审核门禁已解除，正在恢复后续 Assignment。"],
    MANUAL_RESUME_STARTED: ["COORDINATION", "MANUAL_RESUME", "RUNNING", "正在从当前 SharedContext 恢复已发布的后续 Assignment。"],
    ASSIGNMENT_RECOVERED: ["ASSIGNMENT", "RECOVER_EXPIRED_ASSIGNMENT", "WARNING", `已回收 ${payload.recovered_count || 0} 个过期执行租约并创建安全重试。`],
    ASSIGNMENT_READY: ["ASSIGNMENT", "ASSIGNMENT_READY", "SUCCESS", `${agentLabel(record.actor)} 的子任务已解锁并等待认领：${payload.subtask_name || payload.subtask_type || "未命名任务"}。`],
    AGENT_RUN_STARTED: ["AGENT_RUN", "START_AGENT_RUN", "RUNNING", `${agentLabel(record.actor)} 已开始执行：${payload.subtask_name || payload.subtask_type || "未命名任务"}。`],
    AGENT_RUN: ["AGENT_RUN", "COMPLETE_AGENT_RUN", payload.status === "SUCCESS" ? "SUCCESS" : "WARNING", `${agentLabel(record.actor)} 已提交子任务结果，调用 ${(payload.skills_used || []).map(skillLabel).join(" → ") || "无 Skill"}。`],
    WORKER_IDLE: ["COORDINATION", "WORKER_IDLE", "WARNING", `当前没有 Worker 完成任务；协调状态为 ${payload.coordination_action || "UNKNOWN"}。`],
    COORDINATION_STARTED: ["COORDINATION", "START_COORDINATION", "RUNNING", "协调器正在检查依赖、审核门禁和最终目标。"],
    COORDINATION: ["COORDINATION", payload.action || "COORDINATE", "SUCCESS", `协调器返回：${payload.action || "UNKNOWN"}。`],
    ORCHESTRATION_RUN: ["COMPLETE", "END_ORCHESTRATION", "SUCCESS", "本轮服务端执行结束，正在整理完整结果。"],
  };
  const mapped = mappings[type] || ["COORDINATION", type, "INFO", `收到公开执行事件：${type}`];
  if (!state.liveLogCount) dom.logStream.innerHTML = "";
  state.liveLogCount += 1;
  appendLog({
    sequence: sequence || state.liveLogCount,
    phase: mapped[0],
    actor: record.actor || "simulation_lab",
    action: mapped[1],
    status: mapped[2],
    message: mapped[3],
    details: payload,
  });
  dom.logCount.textContent = `${state.liveLogCount} 条实时`;
  dom.logCount.className = "overall-badge is-idle";
  updateLiveProgress(type, payload, mapped[3]);
}

/** 根据已真实到达的公开阶段更新百分比，最终值仍以服务端报告为准。 */
function updateLiveProgress(type, payload, message) {
  if (type === "ORCHESTRATION_STARTED") state.liveProgress.percent = 5;
  if (type === "MEMORY_RECALL") state.liveProgress.percent = Math.max(state.liveProgress.percent, 12);
  if (type === "CONTROLLER_STARTED") state.liveProgress.percent = Math.max(state.liveProgress.percent, 15);
  if (type === "CONTROLLER_RUN") {
    state.liveProgress.total = (payload.subtasks || []).length;
    state.liveProgress.percent = Math.max(state.liveProgress.percent, 25);
  }
  if (type === "PLAN_REVIEW_REQUESTED") state.liveProgress.percent = Math.max(state.liveProgress.percent, 20);
  if (type === "PLAN_REVIEW_APPROVED") state.liveProgress.percent = Math.max(state.liveProgress.percent, 22);
  if (type === "PLAN_REVIEW_REVISION_REQUESTED") state.liveProgress.percent = Math.max(state.liveProgress.percent, 15);
  if (["HUMAN_REVIEW_RESOLVED", "REVIEW_RESUME_STARTED", "ASSIGNMENT_READY", "AGENT_RUN_STARTED"].includes(type)) {
    state.liveProgress.percent = Math.max(state.liveProgress.percent, 25);
  }
  if (type === "AGENT_RUN") {
    state.liveProgress.completed += 1;
    const total = Math.max(state.liveProgress.total, state.liveProgress.completed, 1);
    state.liveProgress.percent = Math.max(
      state.liveProgress.percent,
      Math.min(85, Math.round(25 + state.liveProgress.completed / total * 60)),
    );
  }
  // 协调器会在每个 Agent 批次后运行，不能仅因进入协调检查就把进度跳到 88%。
  if (type === "COORDINATION_STARTED") state.liveProgress.percent = Math.max(state.liveProgress.percent, 25);
  if (type === "COORDINATION") {
    if (payload.action === "TASK_COMPLETED") state.liveProgress.percent = 100;
    else state.liveProgress.percent = Math.max(state.liveProgress.percent, 25);
  }
  if (type === "ORCHESTRATION_RUN" && !payload.internal_continuation) {
    state.liveProgress.percent = Math.max(state.liveProgress.percent, 25);
  }
  setProgress(state.liveProgress.percent, phaseLabel(type === "CONTROLLER_RUN" ? "DECOMPOSITION" : type.split("_")[0]), message);
}

/** 将一条日志及其可展开 details 添加到中间日志流。 */
function appendLog(entry) {
  const detail = Object.keys(entry.details || {}).length
    ? `<details><summary>查看结构化详情</summary><pre>${escapeHtml(JSON.stringify(entry.details, null, 2))}</pre></details>`
    : "";
  dom.logStream.insertAdjacentHTML("beforeend", `
    <article class="log-entry is-${entry.status.toLowerCase()}">
      <span class="log-sequence">${String(entry.sequence).padStart(2, "0")}</span>
      <div class="log-body">
        <div class="log-meta"><strong>${escapeHtml(phaseLabel(entry.phase))}</strong><span>${escapeHtml(actorLabel(entry.actor))}</span><code>${escapeHtml(entry.action)}</code></div>
        <p>${escapeHtml(logMessage(entry))}</p>
        ${detail}
      </div>
      <b>${escapeHtml(entry.status)}</b>
    </article>
  `);
  dom.logStream.scrollTop = dom.logStream.scrollHeight;
}

/** 根据当前标签页组织同一份公开结构化结果，不添加自动结论。 */
function renderEvidence() {
  if (!state.report) {
    dom.evidenceContent.innerHTML = '<div class="empty-state"><span>◎</span><strong>暂无输出</strong><p>完成运行后可分别查看总控意图、子任务与 Skill 调用顺序。</p></div>';
    return;
  }
  const renderers = {
    intent: renderIntent,
    route: renderRoute,
    skills: renderSkills,
    artifacts: renderArtifacts,
    reviews: renderReviews,
    timings: renderTimings,
    raw: renderRaw,
  };
  dom.evidenceContent.innerHTML = renderers[state.activeTab]();
}

/** 展示总控 Agent 的意图、澄清状态和完整 TaskSpec。 */
function renderIntent() {
  const observed = state.report.observed;
  return `
    <article class="result-card">
      <div class="result-title"><strong>总控意图</strong><span>${escapeHtml(observed.task_intent)}</span></div>
      <p>${escapeHtml(observed.task_spec.business_goal)}</p>
      <div class="kv"><span>需澄清</span><span>${observed.requires_clarification ? "是" : "否"}</span></div>
      <div class="kv"><span>本体策略</span><span>${escapeHtml(observed.task_spec.ontology_strategy?.mode)}</span></div>
      <div class="kv"><span>目标产物</span><span>${escapeHtml((observed.task_spec.target_output_types || []).join("、") || "未声明")}</span></div>
    </article>
    <pre class="raw-output">${escapeHtml(JSON.stringify(observed.task_spec, null, 2))}</pre>`;
}

/** 逐项展示子任务、负责 Agent、Assignment 和完成状态。 */
function renderRoute() {
  const observed = state.report.observed;
  const assignments = Object.fromEntries(observed.assignments.map((item) => [item.subtask_id, item]));
  return observed.subtasks.map((item, index) => {
    const assignment = assignments[item.subtask_id] || {};
    return `<article class="route-card">
      <strong>${index + 1}. ${escapeHtml(item.name)}</strong>
      <small>${escapeHtml(agentLabel(item.assigned_agent_type))}</small>
      <div class="kv"><span>任务类型</span><span>${escapeHtml(item.subtask_type)}</span></div>
      <div class="kv"><span>预期输出</span><span>${escapeHtml(item.expected_output_types.join("、"))}</span></div>
      <div class="kv"><span>认领者</span><span>${escapeHtml(assignment.claimed_by ? actorLabel(assignment.claimed_by) : "未认领")}</span></div>
      <div class="kv"><span>状态</span><span>${escapeHtml(item.status)}</span></div>
    </article>`;
  }).join("") || '<div class="empty-state"><strong>总控没有发布子任务</strong></div>';
}

/** 按每个 AgentRun 的原始顺序展示专业 Agent 调用的 Skills。 */
function renderSkills() {
  return state.report.observed.worker_runs.map((run) => `<article class="route-card">
    <strong>${escapeHtml(agentLabel(run.agent_type))}</strong>
    <small>${escapeHtml(run.status)}</small>
    <div class="skill-chain">${run.skills.map((skill, index) => {
      const executionLabel = "Agent 调用 Skill";
      const duration = Number.isFinite(skill.duration_ms) ? ` · ${skill.duration_ms >= 1000 ? `${(skill.duration_ms / 1000).toFixed(2)} 秒` : `${skill.duration_ms.toFixed(2)} 毫秒`}` : "";
      return `<div><span>${index + 1}</span><strong>${escapeHtml(skillLabel(skill.skill_name))}</strong><small>${escapeHtml(executionLabel)}${escapeHtml(duration)} → ${escapeHtml(skill.output_artifact_types.join("、") || "无输出")}</small></div>`;
    }).join("") || '<p>没有 Skill 调用</p>'}</div>
  </article>`).join("") || '<div class="empty-state"><strong>没有 AgentRunResult</strong></div>';
}

/** 展示每个 Artifact 的来源、血缘和已保存的内容预览。 */
function renderArtifacts() {
  return state.report.observed.artifacts.map((artifact) => `<article class="route-card">
    <strong>${escapeHtml(artifact.artifact_type)}</strong>
    <small>${artifact.artifact_type === "ExternalOntology" ? "用户本体输入" : artifact.artifact_type === "RawDocument" ? "用户文档输入" : "系统产物"} · ${escapeHtml(artifact.producer_skill ? skillLabel(artifact.producer_skill) : agentLabel(artifact.producer_agent))}</small>
    <p>${escapeHtml(artifact.content_summary)}</p>
    ${["CandidateGraph", "PublishedGraph"].includes(artifact.artifact_type) ? `<button class="review-action" data-graph-artifact-id="${escapeHtml(artifact.artifact_id)}" type="button">查看图谱预览</button><div class="graph-preview-slot" data-graph-slot="${escapeHtml(artifact.artifact_id)}"></div>` : ""}
    ${artifact.content_preview ? `<details><summary>查看中间产物内容</summary><pre>${escapeHtml(JSON.stringify(artifact.content_preview, null, 2))}</pre></details>` : ""}
  </article>`).join("") || '<div class="empty-state"><strong>没有 Artifact</strong></div>';
}

/** 读取测试任务内的图谱预览，并在当前产物卡片使用共用 SVG 组件展示。 */
async function openGraphArtifactPreview(artifactId) {
  if (!state.taskId) return;
  const slot = document.querySelector(`[data-graph-slot="${CSS.escape(artifactId)}"]`);
  if (!slot) return;
  slot.innerHTML = '<p class="field-note">正在加载图谱预览…</p>';
  try {
    const preview = await api(
      `/api/v1/simulations/${encodeURIComponent(state.taskId)}/artifacts/${encodeURIComponent(artifactId)}/graph-preview`,
    );
    window.KGGraphView?.render(slot, preview);
  } catch (error) {
    slot.innerHTML = `<p class="field-note">${escapeHtml(error.message)}</p>`;
  }
}

/** 用图谱工作台的完整操作栈覆盖 GraphChangeSet 草稿，仍需用户显式提交审核。 */
function replaceGraphChangeOperations(operations) {
  try {
    dom.reviewOperations.value = JSON.stringify(operations, null, 2);
    showToast(`已同步 ${operations.length} 项 GraphChangeSet 草稿；请确认后提交修改。`);
  } catch (error) {
    showToast(`无法同步图谱修改：${error.message}`, true);
  }
}

/** 展示候选本体/图谱的隔离人工审核状态，不替用户生成审核结论。 */
function renderReviews() {
  const reviews = state.report.reviews || [];
  return reviews.map((review) => `<article class="route-card">
    <strong>${review.target_type === "ONTOLOGY" ? "候选本体确认" : "候选图谱确认"}</strong>
    <small>${escapeHtml(review.status)} · ${escapeHtml(review.target_artifact_id)}</small>
    <div class="kv"><span>质量报告</span><span>${(review.quality_report_artifact_ids || []).length} 份</span></div>
    <button class="review-action" data-review-id="${escapeHtml(review.review_id)}" type="button">${review.status === "PENDING" ? "预览并审核" : "查看候选内容"}</button>
    ${review.status === "APPROVED" ? '<button class="review-action" data-resume-task type="button">继续执行后续任务</button>' : ""}
  </article>`).join("") || '<div class="empty-state"><strong>暂无待审核候选内容</strong></div>';
}

/** 读取候选本体完整 JSON，并在隔离审核弹窗中直接预览。 */
async function openReview(reviewId) {
  try {
    const materials = await api(
      `/api/v1/simulations/${encodeURIComponent(state.taskId)}/reviews/${encodeURIComponent(reviewId)}/materials`,
    );
    state.reviewId = reviewId;
    const canResolve = materials.review.status === "PENDING";
    const isGraph = ["CandidateGraph", "PublishedGraph"].includes(materials.target.artifact_type);
    const graphPreview = isGraph
      ? await api(
        `/api/v1/simulations/${encodeURIComponent(state.taskId)}/artifacts/${encodeURIComponent(materials.target.artifact_id)}/graph-preview`,
      )
      : null;
    dom.reviewFeedback.value = "";
    dom.reviewTitle.value = "";
    dom.reviewOperations.value = "[]";
    dom.reviewFeedback.hidden = !canResolve;
    dom.reviewFeedbackLabel.hidden = !canResolve;
    dom.reviewTitle.hidden = !canResolve;
    dom.reviewTitleLabel.hidden = !canResolve;
    dom.reviewOperations.hidden = !canResolve;
    dom.reviewOperationsLabel.hidden = !canResolve;
    dom.reviewActions.hidden = !canResolve;
    dom.reviewMaterials.innerHTML = `
      <div class="kv"><span>候选类型</span><span>${escapeHtml(materials.target.artifact_type)}</span></div>
      <div class="kv"><span>Artifact</span><span>${escapeHtml(materials.target.artifact_id)}</span></div>
      <div class="kv"><span>审核状态</span><span>${escapeHtml(materials.review.status)}</span></div>
      ${isGraph ? '<div id="review-graph-preview"></div>' : ""}
      <details><summary>查看原始 Artifact JSON</summary><pre class="review-preview">${escapeHtml(JSON.stringify(materials.target_content, null, 2))}</pre></details>`;
    if (graphPreview) {
      window.KGGraphView?.render(document.querySelector("#review-graph-preview"), graphPreview, {
        editable: canResolve && materials.target.artifact_type === "CandidateGraph",
        onOperationsChange: replaceGraphChangeOperations,
      });
    }
    dom.reviewDialog.showModal();
  } catch (error) {
    showToast(error.message, true);
  }
}

/** 提交用户真实审核动作；修改会保存为不可变 OntologyChangeSet/GraphChangeSet。 */
async function resolveReview(action) {
  if (!state.reviewId || !state.taskId) return;
  let operations = [];
  try {
    const publicationTitle = dom.reviewTitle.value.trim();
    if (action === "APPROVE" && !publicationTitle) {
      throw new Error("请为即将发布的本体或图谱填写名称");
    }
    if (action === "SUBMIT_CHANGES") {
      operations = JSON.parse(dom.reviewOperations.value || "[]");
      if (!Array.isArray(operations) || !operations.length) {
        throw new Error("提交修改时请至少填写一个 ChangeSet 操作");
      }
    }
    // 审核后的专业 Agent 可能需要较长模型调用。先恢复 SSE 并关闭弹窗，让用户可以
    // 立即看到 Assignment 解锁、Agent 开始、Skill 完成和下一次审核，而不是面对
    // 一个长时间不动的模态框。
    state.liveLogCount = 0;
    await startSimulationStream(state.taskId);
    dom.reviewDialog.close();
    setProgress(
      Math.max(state.report.progress?.percent || 0, 25),
      "审核已提交，正在继续执行",
      "系统正在解锁并认领后续子任务；实时日志会持续更新。",
    );
    const result = await api(
      `/api/v1/simulations/${encodeURIComponent(state.taskId)}/reviews/${encodeURIComponent(state.reviewId)}/resolve`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          review_id: state.reviewId,
          action,
          reviewer_id: "simulation-user",
          reviewer_feedback: dom.reviewFeedback.value.trim() || null,
          publication_title: action === "APPROVE" ? publicationTitle : null,
          operations,
        }),
      },
    );
    const review = (state.report.reviews || []).find((item) => item.review_id === state.reviewId);
    if (review) Object.assign(review, result.review);
    if (result.observed) state.report.observed = result.observed;
    if (result.reviews) state.report.reviews = result.reviews;
    if (result.timings) state.report.timings = result.timings;
    if (result.progress) state.report.progress = result.progress;
    showToast(`审核已提交：${result.review.status}；下一状态：${result.next_action}`);
    if (result.progress) {
      setProgress(
        result.progress.percent,
        result.progress.stage,
        `${result.progress.completed_subtasks}/${result.progress.total_subtasks} 个子任务完成；请查看新产生的 Artifact 或下一审核。`,
      );
    }
    renderEvidence();
    renderPlanReview();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    stopSimulationStream();
  }
}

/** 在审核已完成或页面曾中断时，继续运行当前任务中已解锁的 Assignment。 */
async function resumeCurrentTask() {
  if (!state.taskId || !state.report) return;
  try {
    state.liveLogCount = 0;
    await startSimulationStream(state.taskId);
    setProgress(
      Math.max(state.report.progress?.percent || 0, 25),
      "正在恢复后续任务",
      "不会重新拆解计划，只认领当前已满足依赖和审核门禁的 Assignment。",
    );
    const result = await api(
      `/api/v1/simulations/${encodeURIComponent(state.taskId)}/resume`,
      { method: "POST" },
    );
    if (result.observed) state.report.observed = result.observed;
    if (result.reviews) state.report.reviews = result.reviews;
    if (result.timings) state.report.timings = result.timings;
    if (result.progress) state.report.progress = result.progress;
    if (result.plan_review) state.report.plan_review = result.plan_review;
    showToast(`任务恢复结束；下一状态：${result.next_action}`);
    if (result.progress) {
      setProgress(
        result.progress.percent,
        result.progress.stage,
        `${result.progress.completed_subtasks}/${result.progress.total_subtasks} 个子任务完成。`,
      );
    }
    renderEvidence();
    renderPlanReview();
  } catch (error) {
    showToast(error.message, true);
  } finally {
    stopSimulationStream();
  }
}

/** 展示总控、专业 Agent、各 Skill、依赖协调与端到端墙钟耗时。 */
function renderTimings() {
  const timings = state.report.timings || [];
  return timings.map((item) => `<article class="route-card">
    <strong>${escapeHtml(timingOperationLabel(item.operation))}</strong>
    <small>${escapeHtml(actorLabel(item.actor))} · ${escapeHtml(item.category)}</small>
    <div class="kv"><span>耗时</span><span>${item.duration_ms >= 1000 ? `${(item.duration_ms / 1000).toFixed(2)} 秒` : `${item.duration_ms.toFixed(2)} 毫秒`}</span></div>
    ${item.subtask_id ? `<div class="kv"><span>子任务</span><span>${escapeHtml(item.subtask_id)}</span></div>` : ""}
  </article>`).join("") || '<div class="empty-state"><strong>当前运行没有耗时记录</strong></div>';
}

/** 原样展示服务端观察结果和日志，便于复制后离线分析。 */
function renderRaw() {
  return `<pre class="raw-output">${escapeHtml(JSON.stringify(state.report, null, 2))}</pre>`;
}

/** 注册表单、上下文清理和结果标签页事件。 */
function bindEvents() {
  dom.form.addEventListener("submit", runSimulation);
  dom.documentInput.addEventListener("change", renderDocumentFiles);
  dom.ontologyInput.addEventListener("change", renderOntologyFile);
  dom.ontologyInput.addEventListener("change", () => {
    if (dom.ontologyInput.files?.length) dom.publishedOntologySelect.value = "";
  });
  dom.publishedOntologySelect.addEventListener("change", () => {
    if (dom.publishedOntologySelect.value) dom.ontologyInput.value = "";
    renderOntologyFile();
  });
  dom.refreshAssetsButton.addEventListener("click", loadPublishedAssets);
  dom.resetButton.addEventListener("click", resetExperiment);
  dom.planReviewPanel.addEventListener("click", (event) => {
    if (event.target.closest("[data-open-plan-review]")) openPlanReviewDialog();
    if (event.target.closest("[data-resume-approved-plan]")) resumeCurrentTask();
  });
  document.querySelector("#approve-plan-button").addEventListener("click", approvePlanAndExecute);
  document.querySelector("#revise-plan-button").addEventListener("click", revisePlan);
  dom.planReviewMaterials.addEventListener("click", (event) => {
    const button = event.target.closest("[data-plan-move]");
    if (button) movePlanSubtask(button.dataset.subtaskId, button.dataset.planMove);
  });
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("is-active", item === tab));
    state.activeTab = tab.dataset.tab;
    renderEvidence();
  }));
  dom.evidenceContent.addEventListener("click", (event) => {
    const button = event.target.closest("[data-review-id]");
    if (button) openReview(button.dataset.reviewId);
    const graphButton = event.target.closest("[data-graph-artifact-id]");
    if (graphButton) openGraphArtifactPreview(graphButton.dataset.graphArtifactId);
    const resumeButton = event.target.closest("[data-resume-task]");
    if (resumeButton) resumeCurrentTask();
  });
  document.querySelector("#approve-review-button").addEventListener("click", () => resolveReview("APPROVE"));
  document.querySelector("#reject-review-button").addEventListener("click", () => resolveReview("REJECT"));
  document.querySelector("#changes-review-button").addEventListener("click", () => resolveReview("SUBMIT_CHANGES"));
}

/** 初始化人工观察台。 */
async function initialize() {
  bindEvents();
  window.addEventListener("kg-agent-extension-published", loadCustomCapabilityNames);
  restoreTaskIdentity();
  renderOntologyFile();
  await Promise.all([checkHealth(), loadPublishedAssets(), loadCustomCapabilityNames()]);
  if (!state.taskId) return;
  try {
    state.report = await api(`/api/v1/simulations/${encodeURIComponent(state.taskId)}`);
    state.conversationId = state.report.observed.conversation_id;
    persistTaskIdentity();
    dom.runMode.textContent = `继续当前实验 · ${state.taskId.slice(0, 8)}`;
    renderCompletedLogs();
  } catch (error) {
    // 任务可能被数据库清理；只清除本浏览器的过期身份，不影响服务端任何数据。
    resetExperiment();
    showToast(`无法恢复上次任务：${error.message}`, true);
  }
}

initialize();
