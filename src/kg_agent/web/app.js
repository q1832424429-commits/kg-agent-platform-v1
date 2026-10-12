/**
 * 简易 Agent 工作台浏览器端控制器。
 * 本文件只调用正式公开 API 和渲染可审计结果，不复制后端业务规则或 Agent 决策逻辑。
 */

"use strict";

// 集中保存页面当前选择，刷新任务快照时只替换服务端返回的公开事实。
const state = {
  taskId: null,
  conversationId: null,
  snapshot: null,
  conversations: [],
  conversationSnapshot: null,
  activeTab: "route",
  reviewId: null,
  presentedReviewIds: new Set(),
  eventSource: null,
  eventRefreshTimer: null,
  lastEventSequence: 0,
  toastTimer: null,
  // 运行事实保留内部 key；页面仅通过此只读目录显示用户创建时填写的名称。
  customAgentNames: {},
  customSkillNames: {},
};

// 正式 SSE 保留领域事件名；浏览器需要显式订阅这些公开事件类型。
const taskEventTypes = [
  "MASTER_TASK_CREATED", "MASTER_TASK_STATUS_CHANGED", "TASK_SPEC_REVISED",
  "DECOMPOSITION_PUBLISHED", "SUBTASK_PUBLISHED", "SUBTASK_STATUS_CHANGED",
  "ASSIGNMENT_CREATED", "ASSIGNMENT_CLAIMED", "ASSIGNMENT_STARTED",
  "ASSIGNMENT_HEARTBEAT", "ASSIGNMENT_EXPIRED", "ASSIGNMENT_COMPLETED",
  "CONTEXT_VERSION_CREATED", "ARTIFACT_CREATED", "ARTIFACT_STATUS_CHANGED",
  "HUMAN_REVIEW_REQUESTED", "HUMAN_REVIEW_RESOLVED",
];

// 缓存固定 DOM 节点，避免各个渲染函数重复查询页面。
const dom = {
  apiStatus: document.querySelector("#api-status"),
  taskProgress: document.querySelector("#task-progress"),
  taskProgressValue: document.querySelector("#task-progress-value"),
  taskProgressStage: document.querySelector("#task-progress-stage"),
  taskProgressBar: document.querySelector("#task-progress-bar"),
  currentTaskLabel: document.querySelector("#current-task-label"),
  conversationTaskSelect: document.querySelector("#conversation-task-select"),
  taskList: document.querySelector("#task-list"),
  newTaskButton: document.querySelector("#new-task-button"),
  newTaskInConversationButton: document.querySelector("#new-task-in-conversation-button"),
  conversationMenu: document.querySelector("#conversation-menu"),
  refreshTasksButton: document.querySelector("#refresh-tasks-button"),
  welcomeCard: document.querySelector("#welcome-card"),
  conversation: document.querySelector("#conversation"),
  composer: document.querySelector("#composer"),
  messageInput: document.querySelector("#message-input"),
  fileInput: document.querySelector("#file-input"),
  ontologyInput: document.querySelector("#ontology-input"),
  publishedOntologySelect: document.querySelector("#published-ontology-select"),
  publishedGraphSelect: document.querySelector("#published-graph-select"),
  refreshAssetsButton: document.querySelector("#refresh-assets-button"),
  fileTray: document.querySelector("#file-tray"),
  composerMode: document.querySelector("#composer-mode"),
  sendButton: document.querySelector("#send-button"),
  inspectorContent: document.querySelector("#inspector-content"),
  reviewDialog: document.querySelector("#review-dialog"),
  reviewMaterials: document.querySelector("#review-materials"),
  reviewTitle: document.querySelector("#review-title"),
  reviewTitleLabel: document.querySelector("#review-title-label"),
  reviewFeedback: document.querySelector("#review-feedback"),
  reviewFeedbackLabel: document.querySelector("#review-feedback-label"),
  reviewOperations: document.querySelector("#review-operations"),
  reviewOperationsLabel: document.querySelector("#review-operations-label"),
  reviewActions: document.querySelector("#review-actions"),
  artifactDialog: document.querySelector("#artifact-dialog"),
  artifactDialogTitle: document.querySelector("#artifact-dialog-title"),
  artifactDialogContent: document.querySelector("#artifact-dialog-content"),
  toast: document.querySelector("#toast"),
};

/** 把不可信文本转义后再插入模板，防止任务内容破坏工作台 DOM。 */
function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

/** 将后端枚举转换成便于中文用户理解的显示文本。 */
function label(value) {
  const labels = {
    START: "新建任务", CONTINUE: "继续任务", MODIFY: "修改任务",
    document_parsing_agent: "数据源解析 Agent",
    knowledge_extraction_agent: "知识抽取 Agent",
    knowledge_fusion_agent: "知识融合 Agent",
    quality_evaluation_agent: "质量评估 Agent",
    graph_management_agent: "图谱管理更新 Agent",
    PENDING: "等待中", READY: "可认领", CLAIMED: "已认领", RUNNING: "执行中",
    SUCCESS: "成功", SUCCEEDED: "已完成", COMPLETED: "已完成", FAILED: "失败", CANCELLED: "已取消",
    ACTIVE: "进行中", WAITING_USER: "等待用户", WAITING_HUMAN: "等待人工审核",
    APPROVED: "已批准", REJECTED: "已拒绝", CHANGES_REQUESTED: "已提交修改",
  };
  return state.customAgentNames[value]
    ? capabilityLabel(state.customAgentNames[value], "Agent")
    : labels[value] || value || "—";
}

/** 为自定义能力补齐类型后缀；用户已填写后缀时去重，保持所有页面表达一致。 */
function capabilityLabel(name, kind) {
  const normalized = String(name || "").trim().replace(/\s+(Agent|Skill)$/i, "");
  return normalized ? `${normalized} ${kind}` : kind;
}

/** 返回面向业务用户的 Skill 名称，内部 key 只保留在技术详情和后端契约中。 */
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

/** Artifact 内部类型只用于取数；用户看到的是交付物的业务名称。 */
function artifactTypeLabel(value) {
  return ({
    RawDocument: "原始文档", ParsedDocument: "解析文档", SemanticDocument: "语义文档",
    CandidateOntology: "候选本体", PublishedOntology: "已发布本体",
    EntityDataset: "实体", RelationDataset: "关系", PropertyDataset: "属性",
    EvidenceDataset: "证据", ConflictReport: "冲突报告",
    CandidateGraph: "候选图谱", PublishedGraph: "已发布图谱", TextReport: "报告",
  })[value] || value || "产物";
}

/** 将本体 JSON 投影为业务列表，不要求用户阅读或编辑内部 Schema。 */
function ontologySummary(content) {
  const classes = content.classes || [];
  const relations = content.relations || [];
  const properties = content.properties || [];
  const classNames = Object.fromEntries(classes.map((item) => [item.class_id, item.name]));
  const section = (title, rows) => `<section class="ontology-section"><h3>${title} · ${rows.length}</h3>
    <ul class="ontology-list">${rows.map((row) => `<li>${row}</li>`).join("") || "<li>暂无</li>"}</ul></section>`;
  return `<p>${escapeHtml(content.name || "候选本体")}</p>`
    + section("实体类型", classes.map((item) => `<strong>${escapeHtml(item.name)}</strong>${item.description ? `<small>${escapeHtml(item.description)}</small>` : ""}`))
    + section("关系类型", relations.map((item) => `<strong>${escapeHtml(item.name)}</strong><small>${escapeHtml(classNames[item.domain_class_id] || item.domain_class_id)} → ${escapeHtml(classNames[item.range_class_id] || item.range_class_id)}</small>`))
    + section("属性类型", properties.map((item) => `<strong>${escapeHtml(item.name)}</strong><small>属于 ${escapeHtml(classNames[item.owner_class_id] || item.owner_class_id)} · ${escapeHtml(item.value_type)}</small>`));
}

/** 返回适用于状态文本的样式类，不参与任何后端状态判断。 */
function statusClass(value) {
  return `status-${String(value || "").toLowerCase().replaceAll("_", "-")}`;
}

/** 统一调用 JSON API，并把 FastAPI 或领域错误转为可读异常。 */
async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    let detail = `请求失败（HTTP ${response.status}）`;
    try {
      const payload = await response.json();
      detail = payload.message || payload.detail || detail;
      if (Array.isArray(detail)) detail = detail.map((item) => item.msg).join("；");
    } catch (_) {
      // 非 JSON 错误保持通用 HTTP 提示，避免二次解析失败覆盖原错误。
    }
    throw new Error(detail);
  }
  return response.status === 204 ? null : response.json();
}

/** 显示短时全局反馈；错误会使用更明显的边框和文字颜色。 */
function showToast(message, isError = false) {
  window.clearTimeout(state.toastTimer);
  dom.toast.textContent = message;
  dom.toast.classList.toggle("is-error", isError);
  dom.toast.hidden = false;
  state.toastTimer = window.setTimeout(() => { dom.toast.hidden = true; }, 4200);
}

/** 检测正式 API 是否存活，并更新顶栏连接状态。 */
async function checkHealth() {
  try {
    await api("/health/live");
    dom.apiStatus.textContent = "服务已连接";
    dom.apiStatus.className = "status-pill status-online";
  } catch (_) {
    dom.apiStatus.textContent = "服务未连接";
    dom.apiStatus.className = "status-pill status-offline";
  }
}

/** 加载当前环境用户自定义能力的显示名称，不影响内置能力或任务执行。 */
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
    // 仅刷新显示文字，不重发任务、改变计划或触发 Agent 调用。
    if (state.snapshot) {
      renderTaskProgress();
      renderInspector();
    }
  } catch (_) {
    // 自定义能力目录暂不可用时回退显示内部 key，主工作台仍可继续使用。
  }
}

/** 加载最近会话，任务继续保存在各自的 MasterTask 中。 */
async function loadTasks() {
  try {
    state.conversations = await api("/api/v1/conversations?limit=50");
    renderTaskList();
  } catch (error) {
    showToast(error.message, true);
  }
}

/** 渲染会话导航；操作按钮仅在右键或菜单点击后出现。 */
function renderTaskList() {
  if (!state.conversations.length) {
    dom.taskList.innerHTML = '<div class="empty-state compact">暂无会话</div>';
    return;
  }
  dom.taskList.innerHTML = state.conversations.map((conversation) => `
    <div class="task-list-row">
      <button class="task-item ${conversation.conversation_id === state.conversationId ? "is-active" : ""}" data-conversation-id="${escapeHtml(conversation.conversation_id)}" type="button">
        <strong>${escapeHtml(conversation.title || "未命名会话")}</strong>
        <span>${escapeHtml(conversation.task_count)} 个任务 · ${escapeHtml(new Date(conversation.updated_at).toLocaleString("zh-CN"))}</span>
      </button>
      <button class="task-menu-button" data-conversation-menu-id="${escapeHtml(conversation.conversation_id)}" type="button" aria-label="管理会话 ${escapeHtml(conversation.title || "未命名会话")}">⋯</button>
    </div>
  `).join("");
}

/** 确认后清理会话私有数据；已发布资产由后端保留。 */
async function deleteConversation(conversationId) {
  const conversation = state.conversations.find((item) => item.conversation_id === conversationId);
  if (!conversation) return;
  if (!window.confirm(`删除会话「${conversation.title}」及其中所有任务、对话、文件和日志？已发布本体、图谱及合并后的长期记忆会保留。`)) return;
  try {
    const result = await api(`/api/v1/conversations/${encodeURIComponent(conversationId)}`, { method: "DELETE" });
    if (state.conversationId === conversationId) startNewTask();
    await loadTasks();
    showToast(result.cleanup_warnings?.length ? `会话已删除；${result.cleanup_warnings.join("；")}` : "会话已删除。");
  } catch (error) {
    showToast(error.message, true);
  }
}

/** 使用业务标题重命名会话，不改动 TaskSpec 和历史消息。 */
async function renameConversation(conversationId) {
  const conversation = state.conversations.find((item) => item.conversation_id === conversationId);
  const title = window.prompt("输入新的会话名称", conversation?.title || "")?.trim();
  if (!title || title === conversation?.title) return;
  try {
    await api(`/api/v1/conversations/${encodeURIComponent(conversationId)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    await loadTasks();
  } catch (error) {
    showToast(error.message, true);
  }
}

/** 在点击或右键位置显示当前会话的命名和删除操作。 */
function openConversationMenu(conversationId, x, y) {
  dom.conversationMenu.innerHTML = `
    <button data-rename-conversation-id="${escapeHtml(conversationId)}" type="button">重命名</button>
    <button data-delete-conversation-id="${escapeHtml(conversationId)}" type="button">删除会话</button>`;
  dom.conversationMenu.hidden = false;
  dom.conversationMenu.style.left = `${Math.min(x, window.innerWidth - 150)}px`;
  dom.conversationMenu.style.top = `${Math.min(y, window.innerHeight - 90)}px`;
}

/** 清理选中任务，准备创建独立 MasterTask。 */
function startNewTask() {
  stopEventStream();
  state.taskId = null;
  state.conversationId = null;
  state.snapshot = null;
  state.conversationSnapshot = null;
  state.lastEventSequence = 0;
  state.presentedReviewIds.clear();
  dom.currentTaskLabel.textContent = "尚未选择任务";
  dom.taskProgress.hidden = true;
  dom.conversationTaskSelect.hidden = true;
  dom.composerMode.textContent = "新建任务";
  dom.newTaskInConversationButton.hidden = true;
  dom.welcomeCard.hidden = false;
  dom.conversation.innerHTML = "";
  dom.inspectorContent.innerHTML = '<div class="empty-state"><div class="empty-icon">◎</div><strong>等待任务</strong><span>提交任务后，这里会展示意图、Agent 分配和 Skill 执行结果。</span></div>';
  renderTaskList();
  dom.messageInput.focus();
  history.replaceState(null, "", "/workbench");
}

/** 在当前会话开启新的 MasterTask，同时保留会话历史给总控 Agent。 */
function startTaskInConversation() {
  stopEventStream();
  state.taskId = null;
  state.snapshot = null;
  dom.welcomeCard.hidden = true;
  dom.taskProgress.hidden = true;
  dom.conversationTaskSelect.hidden = true;
  dom.composerMode.textContent = "当前会话中的新任务";
  dom.currentTaskLabel.textContent = "等待新任务";
  history.replaceState(null, "", `/workbench?conversation=${encodeURIComponent(state.conversationId)}`);
  renderTaskList();
  dom.messageInput.focus();
}

/** 选择已有会话，恢复多轮消息并展示最近一个 MasterTask 的执行状态。 */
async function selectConversation(conversationId) {
  stopEventStream();
  state.presentedReviewIds.clear();
  state.conversationId = conversationId;
  state.lastEventSequence = 0;
  await loadConversation(true);
  state.taskId = state.conversationSnapshot?.tasks?.[0]?.task_id || null;
  dom.welcomeCard.hidden = true;
  dom.newTaskInConversationButton.hidden = false;
  dom.composerMode.textContent = state.taskId ? "继续 / 修改当前任务" : "当前会话中的新任务";
  history.replaceState(null, "", `/workbench?conversation=${encodeURIComponent(conversationId)}`);
  if (state.taskId) await loadSnapshot(true);
  renderConversationTasks();
  startEventStream();
  renderTaskList();
}

/** 重新读取会话公开消息，页面刷新与 SSE 均使用同一份持久化事实。 */
async function loadConversation(showError = false) {
  if (!state.conversationId) return;
  try {
    const snapshot = await api(`/api/v1/conversations/${encodeURIComponent(state.conversationId)}`);
    const sameConversation = state.conversationSnapshot?.conversation?.conversation_id === snapshot.conversation?.conversation_id;
    const oldScrollTop = dom.conversation.scrollTop;
    const followLatest = !sameConversation || dom.conversation.scrollHeight - dom.conversation.clientHeight - oldScrollTop < 100;
    state.conversationSnapshot = snapshot;
    state.lastEventSequence = Math.max(state.lastEventSequence, Number(snapshot.last_event_sequence) || 0);
    dom.conversation.innerHTML = (snapshot.messages || []).map((message) => `
      <article class="message message-${message.role === "USER" ? "user" : "agent"}">
        <div class="message-label">${message.role === "USER" ? "你" : "总控 Agent"}</div>
        <div class="message-bubble">${escapeHtml(message.content)}</div>
        ${(message.artifact_refs || []).length ? `<div class="result-artifacts">${message.artifact_refs.map((ref) =>
          `<button type="button" data-artifact-id="${escapeHtml(ref.artifact_id)}">查看${escapeHtml(artifactTypeLabel(ref.artifact_type))}</button>`
        ).join("")}</div>` : ""}
      </article>`).join("");
    renderProcessingIndicator();
    dom.conversation.scrollTop = followLatest ? dom.conversation.scrollHeight : oldScrollTop;
    renderConversationTasks();
  } catch (error) {
    if (showError) showToast(error.message, true);
  }
}

/** 展示同一聊天内的多个业务任务，用户可切换查看任一任务的执行详情。 */
function renderConversationTasks() {
  const tasks = state.conversationSnapshot?.tasks || [];
  dom.conversationTaskSelect.hidden = !state.conversationId || tasks.length === 0;
  dom.conversationTaskSelect.innerHTML = tasks.map((task) => `
    <option value="${escapeHtml(task.task_id)}">${escapeHtml((task.title || "未命名任务").slice(0, 48))} · ${escapeHtml(label(task.status))}</option>`).join("");
  dom.conversationTaskSelect.value = state.taskId || "";
}

/** 获取服务端任务快照；静默刷新不重复弹出成功消息。 */
async function loadSnapshot(showError = false) {
  if (!state.taskId) return;
  try {
    state.snapshot = await api(`/api/v1/tasks/${state.taskId}`);
    state.lastEventSequence = Math.max(
      state.lastEventSequence,
      ...(state.snapshot.events || []).map((event) => Number(event.sequence) || 0),
    );
    state.conversationId = state.snapshot.master_task?.conversation_id || state.conversationId;
    dom.currentTaskLabel.textContent = state.snapshot.master_task?.title || state.taskId;
    renderTaskProgress();
    renderProcessingIndicator();
    renderTaskList();
    renderInspector();
    void maybeAutoOpenReview();
    // 会话 SSE 仍需覆盖之后在同一聊天里创建的其他 MasterTask。
  } catch (error) {
    if (showError) showToast(error.message, true);
  }
}

/** 只为仍在运行的任务显示 Agent 处理气泡；刷新后从本轮用户消息恢复计时。 */
function renderProcessingIndicator() {
  const followLatest = dom.conversation.scrollHeight - dom.conversation.clientHeight - dom.conversation.scrollTop < 100;
  dom.conversation.querySelector(".message-runtime")?.remove();
  const master = state.snapshot?.master_task;
  if (!master || master.task_id !== state.taskId || master.conversation_id !== state.conversationId || master.status !== "ACTIVE") return;
  const messages = state.conversationSnapshot?.messages || [];
  const latestUser = [...messages].reverse().find((item) => item.role === "USER" && item.task_id === state.taskId);
  const startedAt = Date.parse(latestUser?.created_at || master.created_at);
  if (!Number.isFinite(startedAt)) return;
  const stage = calculateTaskProgress().stage;
  dom.conversation.insertAdjacentHTML("beforeend", `<article class="message message-agent message-pending message-runtime">
    <div class="message-label">总控 Agent <span class="message-elapsed" data-started-at="${startedAt}"></span></div>
    <div class="message-bubble">${escapeHtml(stage)}</div>
  </article>`);
  updateElapsedIndicators();
  if (followLatest) dom.conversation.scrollTop = dom.conversation.scrollHeight;
}

/** 仅更新可见计时文字，不触发聊天重新渲染或抢夺用户的滚动位置。 */
function updateElapsedIndicators() {
  document.querySelectorAll(".message-elapsed[data-started-at]").forEach((element) => {
    const seconds = Math.max(0, Math.floor((Date.now() - Number(element.dataset.startedAt)) / 1000));
    element.textContent = `已处理 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒`;
  });
}

/** 待发布的中间产物自动弹窗一次；最终候选目标由聊天结果按钮供用户查看。 */
async function maybeAutoOpenReview() {
  if (dom.reviewDialog.open || dom.artifactDialog.open) return;
  const targets = state.snapshot?.shared_context?.current_task_spec?.target_output_types || [];
  const pending = (state.snapshot?.human_reviews || []).find((review) => {
    if (review.status !== "PENDING" || state.presentedReviewIds.has(review.review_id)) return false;
    const targetType = review.target_type === "ONTOLOGY" ? "CandidateOntology" : "CandidateGraph";
    return !targets.includes(targetType);
  });
  if (!pending) return;
  state.presentedReviewIds.add(pending.review_id);
  await openReview(pending.review_id);
}

/** 只返回最近一个实际发布过子任务的拆解修订，避免历史计划重复计入进度。 */
function currentPlanSubtasks(snapshot) {
  const subtasks = snapshot?.subtasks || [];
  if (!subtasks.length) return [];
  const latestTaskBearingRevision = Math.max(
    ...subtasks.map((item) => Number(item.decomposition_revision) || 0),
  );
  return subtasks.filter(
    (item) => (Number(item.decomposition_revision) || 0) === latestTaskBearingRevision,
  );
}

/** 按真实前置依赖分阶段；同阶段按审核后持久化的 Worker 优先级展示。 */
function routeStages(subtasks) {
  const byId = new Map(subtasks.map((item) => [item.subtask_id, item]));
  const pending = new Map(byId);
  const completed = new Set();
  const stages = [];
  while (pending.size) {
    const ready = [...pending.values()].filter((item) =>
      (item.dependency_subtask_ids || []).every(
        (dependencyId) => !byId.has(dependencyId) || completed.has(dependencyId),
      ),
    );
    // 正式计划由服务端校验为无环；异常旧数据也不能让浏览器无限循环。
    const current = ready.length ? ready : [...pending.values()];
    current.sort((left, right) =>
      (Number(right.priority) || 0) - (Number(left.priority) || 0)
      || String(left.created_at || "").localeCompare(String(right.created_at || ""))
      || String(left.subtask_id).localeCompare(String(right.subtask_id)),
    );
    stages.push(current);
    current.forEach((item) => {
      pending.delete(item.subtask_id);
      completed.add(item.subtask_id);
    });
  }
  return stages;
}

/**
 * 根据确定性任务状态计算生产工作台进度。
 * 进度只用于展示：总控发布占 10%，子任务生命周期占 80%，目标完成占 10%。
 */
function calculateTaskProgress() {
  const snapshot = state.snapshot || {};
  const master = snapshot.master_task || {};
  const subtasks = currentPlanSubtasks(snapshot);
  const reviews = snapshot.human_reviews || [];
  const statusWeights = {
    PENDING: 0, BLOCKED: 0, READY: 0.1, ASSIGNED: 0.2, CLAIMED: 0.35,
    RUNNING: 0.65, WAITING_HUMAN: 0.85, SUCCEEDED: 1, SUPERSEDED: 1,
    FAILED: 0, CANCELLED: 0,
  };
  if (master.status === "SUCCEEDED") return { percent: 100, stage: "任务已完成" };
  if (master.status === "FAILED") return { percent: 10, stage: "任务执行失败" };
  if (master.status === "CANCELLED") return { percent: 0, stage: "任务已取消" };

  const average = subtasks.length
    ? subtasks.reduce((sum, item) => sum + (statusWeights[item.status] || 0), 0) / subtasks.length
    : 0;
  const percent = Math.min(95, Math.round(10 + average * 80));
  const pendingReview = reviews.find((item) => item.status === "PENDING");
  if (pendingReview || master.status === "WAITING_HUMAN") return { percent: Math.max(percent, 90), stage: "等待人工审核" };
  if (master.status === "WAITING_USER") return { percent, stage: "等待用户补充" };
  const orderedSubtasks = routeStages(subtasks).flat();
  const active = orderedSubtasks.find((item) => ["RUNNING", "CLAIMED", "ASSIGNED", "READY"].includes(item.status))
    || orderedSubtasks.find((item) => !["SUCCEEDED", "SUPERSEDED"].includes(item.status));
  if (active) return { percent, stage: `${label(active.assigned_agent_type)} · ${label(active.status)}` };
  if (subtasks.length) return { percent: Math.max(percent, 90), stage: "检查目标产物" };
  return { percent: 10, stage: "总控正在拆解任务" };
}

/** 将当前任务完成百分比和阶段写入顶栏紧凑进度条。 */
function renderTaskProgress() {
  if (!state.snapshot) {
    dom.taskProgress.hidden = true;
    return;
  }
  const progress = calculateTaskProgress();
  dom.taskProgress.hidden = false;
  dom.taskProgressValue.textContent = `${progress.percent}%`;
  dom.taskProgressStage.textContent = progress.stage;
  dom.taskProgressBar.style.width = `${progress.percent}%`;
  dom.taskProgress.querySelector(".task-progress-track").setAttribute("aria-valuenow", String(progress.percent));
}

/** 将一批连续 SSE 事件合并为一次快照刷新，避免事件突发时重复请求完整状态。 */
function scheduleSnapshotRefresh() {
  window.clearTimeout(state.eventRefreshTimer);
  state.eventRefreshTimer = window.setTimeout(async () => {
    await Promise.all([loadConversation(false), loadSnapshot(false), loadTasks()]);
  }, 80);
}

/** 订阅当前会话全部任务的 PostgreSQL 事件；断线续传依赖数据库全局 sequence。 */
function startEventStream() {
  stopEventStream();
  if (!state.conversationId) return;
  const conversationId = state.conversationId;
  const path = `/api/v1/conversations/${encodeURIComponent(conversationId)}/events/stream?after=${state.lastEventSequence}`;
  const source = new EventSource(path);
  state.eventSource = source;
  const handleTaskEvent = (event) => {
    if (state.conversationId !== conversationId || state.eventSource !== source) return;
    const sequence = Number(event.lastEventId);
    if (Number.isFinite(sequence)) state.lastEventSequence = Math.max(state.lastEventSequence, sequence);
    scheduleSnapshotRefresh();
  };
  taskEventTypes.forEach((eventType) => source.addEventListener(eventType, handleTaskEvent));
  source.onopen = () => {
    dom.apiStatus.textContent = "实时进度已连接";
    dom.apiStatus.className = "status-pill status-online";
  };
  source.onerror = () => {
    if (state.eventSource === source) {
      dom.apiStatus.textContent = "实时进度重连中";
      dom.apiStatus.className = "status-pill status-offline";
    }
  };
}

/** 关闭旧会话的 SSE 和待执行刷新，避免切换会话后串流到错误页面。 */
function stopEventStream() {
  window.clearTimeout(state.eventRefreshTimer);
  state.eventRefreshTimer = null;
  if (state.eventSource) state.eventSource.close();
  state.eventSource = null;
}

/** 分别渲染普通文档和外部本体，让用户在提交前核对业务类型。 */
function renderFiles() {
  const files = Array.from(dom.fileInput.files || []);
  const ontology = dom.ontologyInput.files?.[0] || null;
  dom.fileTray.hidden = files.length === 0 && !ontology;
  dom.fileTray.innerHTML = [
    ...files.map((file) => `<span class="file-token">文档 · ${escapeHtml(file.name)}</span>`),
    ...(ontology ? [`<span class="file-token ontology-token">本体 · ${escapeHtml(ontology.name)}</span>`] : []),
  ].join("");
}

/** 加载可跨任务复用的已发布本体和图谱目录。 */
async function loadPublishedAssets() {
  try {
    const [ontologies, graphs] = await Promise.all([
      api("/api/v1/published-assets/ontologies"),
      api("/api/v1/published-assets/graphs"),
    ]);
    const selectedOntology = dom.publishedOntologySelect.value;
    const selectedGraph = dom.publishedGraphSelect.value;
    dom.publishedOntologySelect.innerHTML = '<option value="">不选择</option>' + ontologies.map((item) =>
      `<option value="${escapeHtml(item.artifact_id)}">${escapeHtml(item.publication_title || item.content_summary || item.ontology_version || item.artifact_id)}</option>`
    ).join("");
    dom.publishedGraphSelect.innerHTML = '<option value="">不选择</option>' + graphs.map((item) =>
      `<option value="${escapeHtml(item.artifact_id)}">${escapeHtml(item.publication_title || item.content_summary || item.artifact_id)}</option>`
    ).join("");
    dom.publishedOntologySelect.value = selectedOntology;
    dom.publishedGraphSelect.value = selectedGraph;
  } catch (error) {
    showToast(`已发布资产加载失败：${error.message}`, true);
  }
}

/** 根据是否有文档、本体及已有任务，选择对应的正式 JSON 或 multipart 路由。 */
async function submitTask(event) {
  event.preventDefault();
  const message = dom.messageInput.value.trim();
  const files = Array.from(dom.fileInput.files || []);
  const ontology = dom.ontologyInput.files?.[0] || null;
  const publishedOntologyId = dom.publishedOntologySelect.value;
  const publishedGraphId = dom.publishedGraphSelect.value;
  if (!message) return;

  // 用户发送后立即进入对话视图；不能等待总控 Agent 的长请求返回才隐藏欢迎示例。
  dom.welcomeCard.hidden = true;
  dom.sendButton.disabled = true;
  dom.sendButton.textContent = "处理中…";
  addMessage("user", message, ontology ? [...files, ontology] : files);
  const pending = document.createElement("article");
  pending.className = "message message-agent message-pending";
  pending.innerHTML = `<div class="message-label">总控 Agent <span class="message-elapsed" data-started-at="${Date.now()}"></span></div><div class="message-bubble">正在理解任务并拆解子任务…</div>`;
  updateElapsedIndicators();
  dom.conversation.appendChild(pending);
  dom.conversation.scrollTop = dom.conversation.scrollHeight;
  try {
    let path = state.taskId ? `/api/v1/tasks/${state.taskId}/messages` : "/api/v1/tasks";
    let options;
    if (files.length || ontology || publishedOntologyId || publishedGraphId) {
      path = state.taskId ? `/api/v1/tasks/${state.taskId}/messages/upload` : "/api/v1/tasks/upload";
      const body = new FormData();
      body.append("message", message);
      body.append("request_id", crypto.randomUUID());
      if (state.conversationId) body.append("conversation_id", state.conversationId);
      files.forEach((file) => body.append("files", file));
      if (ontology) body.append("ontology_file", ontology);
      if (publishedOntologyId) body.append("published_ontology_artifact_id", publishedOntologyId);
      if (publishedGraphId) body.append("published_graph_artifact_id", publishedGraphId);
      options = { method: "POST", body };
    } else {
      options = {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ message, conversation_id: state.conversationId, request_id: crypto.randomUUID() }),
      };
    }
    const accepted = await api(path, options);
    pending.remove();
    state.taskId = accepted.requires_clarification && !state.taskId ? null : accepted.task_id;
    state.conversationId = accepted.conversation_id;
    dom.welcomeCard.hidden = true;
    dom.newTaskInConversationButton.hidden = false;
    dom.composerMode.textContent = state.taskId ? "继续 / 修改当前任务" : "补充任务信息";
    history.replaceState(null, "", `/workbench?conversation=${encodeURIComponent(state.conversationId)}`);
    dom.messageInput.value = "";
    dom.fileInput.value = "";
    dom.ontologyInput.value = "";
    dom.publishedOntologySelect.value = "";
    dom.publishedGraphSelect.value = "";
    renderFiles();
    await Promise.all([loadConversation(true), loadSnapshot(true), loadTasks()]);
    if (!(state.conversationSnapshot?.messages || []).length) addAgentReply(accepted);
    startEventStream();
  } catch (error) {
    pending.remove();
    addMessage("agent", `本轮任务未能提交：${error.message}`);
    showToast(error.message, true);
  } finally {
    dom.sendButton.disabled = false;
    dom.sendButton.textContent = "发送任务";
  }
}

/** 将用户消息和可选附件摘要加入当前浏览器会话。 */
function addMessage(role, text, files = []) {
  const fileText = files.length ? `\n附件：${files.map((file) => file.name).join("、")}` : "";
  dom.conversation.insertAdjacentHTML("beforeend", `
    <article class="message message-${role}">
      <div class="message-label">${role === "user" ? "你" : role === "agent" ? "总控 Agent" : "系统"}</div>
      <div class="message-bubble">${escapeHtml(text + fileText)}</div>
    </article>
  `);
  dom.conversation.scrollTop = dom.conversation.scrollHeight;
}

/** 把总控 Agent 的结构化输出转换为简洁、可追踪的对话反馈。 */
function addAgentReply(accepted) {
  const agentNames = [...new Set((accepted.subtasks || []).map((item) => label(item.assigned_agent_type)))];
  const text = accepted.requires_clarification
    ? `总控 Agent 需要补充信息：${accepted.clarification_question}`
    : `已识别为“${label(accepted.task_intent)}”，拆分 ${accepted.subtasks.length} 个子任务${agentNames.length ? `，分配给：${agentNames.join("、")}` : ""}。`;
  dom.conversation.insertAdjacentHTML("beforeend", `
    <article class="message message-agent">
      <div class="message-label">总控 Agent</div>
      <div class="message-bubble">${escapeHtml(text)}</div>
    </article>
  `);
  dom.conversation.scrollTop = dom.conversation.scrollHeight;
}

/** 根据当前标签页把同一个任务快照投影成对应视图。 */
function renderInspector() {
  if (!state.snapshot) return;
  const renderers = {
    route: renderRoute,
    artifacts: renderArtifacts,
    reviews: renderReviews,
    events: renderEvents,
  };
  dom.inspectorContent.innerHTML = renderers[state.activeTab]();
}

/** 展示总控意图、任务拆解、专业 Agent 分配及 Skill 使用情况。 */
function renderRoute() {
  const snapshot = state.snapshot;
  const master = snapshot.master_task || {};
  const context = snapshot.shared_context || {};
  const taskSpec = context.current_task_spec || context.task_spec || {};
  const assignments = Object.fromEntries((snapshot.assignments || []).map((item) => [item.subtask_id, item]));
  const runs = Object.fromEntries((snapshot.agent_runs || []).map((item) => [item.subtask_id, item]));
  const currentSubtasks = currentPlanSubtasks(snapshot);
  const stages = routeStages(currentSubtasks);
  const nodes = stages.map((stage, stageIndex) => `<section class="route-stage">
    <div class="route-stage-heading">阶段 ${stageIndex + 1}<span>${stage.length > 1 ? "同阶段 · 按优先级认领" : stageIndex === 0 ? "可立即执行" : "等待前置产物"}</span></div>
    <div class="route-stage-nodes">${stage.map((subtask) => {
    const assignment = assignments[subtask.subtask_id] || {};
    const run = runs[subtask.subtask_id] || {};
    const skills = (run.skills_used || []).map((skill) => {
      const executionLabel = "Agent 调用 Skill";
      return `<span class="skill-chip">${escapeHtml(skillLabel(skill))} · ${escapeHtml(executionLabel)}</span>`;
    }).join("");
    return `<div class="route-node">
      <strong>${escapeHtml(subtask.name || subtask.goal || subtask.subtask_type)}</strong>
      <small>${escapeHtml(label(subtask.assigned_agent_type || assignment.assigned_agent_type))} · <span class="${statusClass(run.status || assignment.status)}">${escapeHtml(label(run.status || assignment.status))}</span></small>
      ${skills || '<span class="skill-chip">等待专业 Agent 选择 Skill</span>'}
    </div>`;
    }).join("")}</div>
  </section>`).join("");
  return `
    <div class="section-title"><h3>总控识别</h3><span>修订 ${escapeHtml(master.current_decomposition_revision || 1)}</span></div>
    <div class="detail-card accent">
      <div class="detail-row"><span class="detail-key">任务意图</span><strong class="detail-value">${escapeHtml(label(taskSpec.task_intent || master.task_intent))}</strong></div>
      <div class="detail-row"><span class="detail-key">业务目标</span><span class="detail-value">${escapeHtml(taskSpec.business_goal || master.title)}</span></div>
      <div class="detail-row"><span class="detail-key">任务状态</span><span class="detail-value ${statusClass(master.status)}">${escapeHtml(label(master.status))}</span></div>
    </div>
    <div class="section-title"><h3>Agent 路由</h3><span>${currentSubtasks.length} 个当前子任务</span></div>
    <div class="route-flow">${nodes || '<div class="empty-state compact">当前没有已发布子任务</div>'}</div>
  `;
}

/** 展示 Artifact 类型、生产者、血缘和内容预览入口。 */
function renderArtifacts() {
  const artifacts = state.snapshot.artifacts || [];
  if (!artifacts.length) return '<div class="empty-state"><div class="empty-icon">◇</div><strong>尚无产物</strong><span>文档接入或 Skill 执行完成后会生成可追溯 Artifact。</span></div>';
  return `
    <div class="section-title"><h3>Artifact</h3><span>${artifacts.length} 项</span></div>
  ${artifacts.map((item) => {
    const ref = item.ref || item;
    return `<button class="detail-card artifact-card full-width" data-artifact-id="${escapeHtml(ref.artifact_id)}" type="button">
      <div class="detail-row"><strong>${escapeHtml(artifactTypeLabel(ref.artifact_type))}</strong><span class="type-chip">v${escapeHtml(ref.schema_version)}</span></div>
      <div class="detail-row"><span class="detail-key">生产者</span><span class="detail-value">${escapeHtml(ref.producer_skill ? skillLabel(ref.producer_skill) : label(ref.producer_agent || "用户上传"))}</span></div>
      <div class="detail-row"><span class="detail-key">状态</span><span class="detail-value">${escapeHtml(label(ref.status))}</span></div>
      <small class="detail-key">点击查看内容与血缘</small>
    </button>`;
  }).join("")}
  `;
}

/** 从对话或右栏打开同一个只读预览；从服务端读取真实 Artifact ID 和内容。 */
async function previewArtifact(artifactId) {
  if (!artifactId) return;
  dom.artifactDialogTitle.textContent = "查看产物";
  dom.artifactDialogContent.textContent = "正在读取产物…";
  if (!dom.artifactDialog.open) dom.artifactDialog.showModal();
  try {
    const artifact = await api(`/api/v1/artifacts/${encodeURIComponent(artifactId)}`);
    const ref = artifact.ref || artifact;
    dom.artifactDialogTitle.textContent = artifactTypeLabel(ref.artifact_type);
    if (["CandidateGraph", "PublishedGraph"].includes(ref.artifact_type)) {
      const graph = await api(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/graph-preview`);
      dom.artifactDialogContent.innerHTML = '<div class="graph-preview-slot"></div>';
      window.KGGraphView?.render(dom.artifactDialogContent.querySelector(".graph-preview-slot"), graph);
      return;
    }
    const response = await fetch(`/api/v1/artifacts/${encodeURIComponent(artifactId)}/content`);
    if (!response.ok) throw new Error(`内容读取失败（HTTP ${response.status}）`);
    const type = response.headers.get("content-type") || "";
    if (type.includes("json")) {
      const content = await response.json();
      dom.artifactDialogContent.innerHTML = ["CandidateOntology", "FusedCandidateOntology", "PublishedOntology"].includes(ref.artifact_type)
        ? ontologySummary(content)
        : `<pre class="artifact-preview">${escapeHtml(JSON.stringify(content, null, 2))}</pre>`;
    } else if (type.startsWith("text/")) {
      dom.artifactDialogContent.innerHTML = `<pre class="artifact-preview">${escapeHtml(await response.text())}</pre>`;
    } else {
      dom.artifactDialogContent.innerHTML = `<p>该产物是文件，可单独下载查看。</p><a href="/api/v1/artifacts/${encodeURIComponent(artifactId)}/content" download>下载${escapeHtml(artifactTypeLabel(ref.artifact_type))}</a>`;
    }
  } catch (error) {
    dom.artifactDialogContent.textContent = error.message;
  }
}

/** 用图谱工作台的完整操作栈覆盖 GraphChangeSet 草稿，确保撤销与移除操作可生效。 */
function replaceGraphChangeOperations(operations) {
  try {
    dom.reviewOperations.value = JSON.stringify(operations, null, 2);
    showToast(`已同步 ${operations.length} 项 GraphChangeSet 草稿；请确认后点击“提交修改”。`);
  } catch (error) {
    showToast(`无法同步图谱修改：${error.message}`, true);
  }
}

/** 展示人工审核门禁，并只为待审核项目提供用户操作入口。 */
function renderReviews() {
  const reviews = state.snapshot.human_reviews || [];
  if (!reviews.length) return '<div class="empty-state"><div class="empty-icon">✓</div><strong>暂无待审核内容</strong><span>候选本体或候选图谱准备好后会在这里请求确认。</span></div>';
  return `
    <div class="section-title"><h3>人工审核</h3><span>${reviews.length} 项</span></div>
    ${reviews.map((review) => `<div class="detail-card ${review.status === "PENDING" ? "accent" : ""}">
      <div class="detail-row"><strong>${escapeHtml(label(review.target_type))}</strong><span class="status-chip ${statusClass(review.status)}">${escapeHtml(label(review.status))}</span></div>
      <div class="detail-row"><span class="detail-key">候选 Artifact</span><span class="detail-value">${escapeHtml(review.target_artifact_id)}</span></div>
      <button class="primary-button full-width review-button" data-review-id="${escapeHtml(review.review_id)}" type="button">${review.status === "PENDING" ? "打开审核" : "查看候选内容"}</button>
    </div>`).join("")}
  `;
}

/** 按事件 sequence 展示可审计状态轨迹，不展示模型隐藏思维过程。 */
function renderEvents() {
  const events = [...(state.snapshot.events || [])].sort((a, b) => b.sequence - a.sequence);
  const recalledMemories = (state.snapshot.shared_context?.long_term_memory_refs || [])
    .filter((item) => item.memory_type === "LONG_TERM")
    .slice(-3);
  if (!events.length && !recalledMemories.length) return '<div class="empty-state"><div class="empty-icon">↯</div><strong>暂无事件</strong></div>';
  const memorySection = recalledMemories.length ? `
    <div class="section-title"><h3>本轮召回长期记忆</h3><span>${recalledMemories.length} 条</span></div>
    ${recalledMemories.map((item, index) => {
      const scoreRef = (item.source_refs || []).find((value) => String(value).startsWith("score:"));
      const score = scoreRef ? `相关度 ${String(scoreRef).slice(6)}` : "已通过召回筛选";
      return `<div class="detail-card">
        <div class="detail-row"><strong>记忆 ${index + 1}</strong><span class="type-chip">${escapeHtml(score)}</span></div>
        <p>${escapeHtml(item.summary || "无摘要")}</p>
      </div>`;
    }).join("")}
  ` : "";
  return `
    ${memorySection}
    <div class="section-title"><h3>执行轨迹</h3><span>${events.length} 条</span></div>
    <div class="timeline">${events.map((event) => `<div class="timeline-item">
      <strong>${escapeHtml(label(event.event_type))}</strong>
      <span>#${escapeHtml(event.sequence)} · ${escapeHtml(new Date(event.occurred_at || event.created_at).toLocaleString("zh-CN"))}</span>
    </div>`).join("")}</div>
  `;
}

/** 读取审核材料并打开审核对话框。 */
async function openReview(reviewId) {
  try {
    const materials = await api(`/api/v1/reviews/${reviewId}/materials`);
    const targetResponse = await fetch(
      `/api/v1/artifacts/${encodeURIComponent(materials.target.artifact_id)}/content`,
    );
    if (!targetResponse.ok) throw new Error(`候选内容读取失败（HTTP ${targetResponse.status}）`);
    const targetType = targetResponse.headers.get("content-type") || "";
    const targetContent = targetType.includes("json")
      ? await targetResponse.json()
      : await targetResponse.text();
    const isGraph = ["CandidateGraph", "PublishedGraph"].includes(materials.target?.artifact_type);
    const isOntology = materials.review?.target_type === "ONTOLOGY";
    const graphPreview = isGraph
      ? await api(`/api/v1/artifacts/${encodeURIComponent(materials.target.artifact_id)}/graph-preview`)
      : null;
    state.reviewId = reviewId;
    const canResolve = materials.review?.status === "PENDING";
    dom.reviewFeedback.value = "";
    dom.reviewTitle.value = "";
    dom.reviewOperations.value = "[]";
    dom.reviewFeedback.hidden = !canResolve;
    dom.reviewFeedbackLabel.hidden = !canResolve;
    dom.reviewTitle.hidden = !canResolve;
    dom.reviewTitleLabel.hidden = !canResolve;
    dom.reviewOperations.hidden = true;
    dom.reviewOperationsLabel.hidden = true;
    dom.reviewFeedbackLabel.textContent = "审核说明";
    document.querySelector("#changes-review-button").hidden = isOntology || !canResolve;
    dom.reviewActions.hidden = !canResolve;
    dom.reviewMaterials.innerHTML = `
      <div class="detail-card accent">
        <div class="detail-row"><span class="detail-key">审核类型</span><strong>${escapeHtml(label(materials.review?.target_type))}</strong></div>
        <div class="detail-row"><span class="detail-key">目标产物</span><span class="detail-value">${escapeHtml(materials.target?.artifact_type)}</span></div>
        <div class="detail-row"><span class="detail-key">质量报告</span><span class="detail-value">${(materials.quality_reports || []).length} 份</span></div>
        <div class="detail-row"><span class="detail-key">冲突报告</span><span class="detail-value">${(materials.conflict_reports || []).length} 份</span></div>
        <div class="detail-row"><span class="detail-key">证据产物</span><span class="detail-value">${(materials.evidence_artifacts || []).length} 份</span></div>
      </div>
      ${isGraph ? '<div id="review-graph-preview"></div>' : isOntology && typeof targetContent === "object"
        ? ontologySummary(targetContent)
        : `<pre class="artifact-preview">${escapeHtml(typeof targetContent === "string" ? targetContent : JSON.stringify(targetContent, null, 2))}</pre>`}`;
    if (graphPreview) {
      window.KGGraphView?.render(document.querySelector("#review-graph-preview"), graphPreview, {
        editable: canResolve && materials.target?.artifact_type === "CandidateGraph",
        onOperationsChange: replaceGraphChangeOperations,
      });
    }
    dom.reviewDialog.showModal();
  } catch (error) {
    showToast(error.message, true);
  }
}

/** 提交确定性的审核决策；修改操作按既有 ChangeOperation 契约传输。 */
async function resolveReview(action) {
  if (!state.reviewId) return;
  let operations = [];
  try {
    const publicationTitle = dom.reviewTitle.value.trim();
    if (action === "APPROVE" && !publicationTitle) {
      throw new Error("请为即将发布的本体或图谱填写名称");
    }
    if (action === "SUBMIT_CHANGES") {
      operations = JSON.parse(dom.reviewOperations.value || "[]");
      if (!Array.isArray(operations) || !operations.length) throw new Error("请至少填写一个修改操作");
    }
    const result = await api(`/api/v1/reviews/${state.reviewId}/resolve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        review_id: state.reviewId,
        action,
        reviewer_id: "default-user",
        reviewer_feedback: dom.reviewFeedback.value.trim() || null,
        publication_title: action === "APPROVE" ? publicationTitle : null,
        operations,
      }),
    });
    dom.reviewDialog.close();
    showToast(`审核已提交：${label(result.review?.status || action)}`);
    await loadSnapshot(true);
    startEventStream();
  } catch (error) {
    showToast(error.message, true);
  }
}

/** 注册所有页面交互，使用事件委托处理动态生成的会话和详情卡片。 */
function bindEvents() {
  dom.composer.addEventListener("submit", submitTask);
  dom.fileInput.addEventListener("change", renderFiles);
  dom.ontologyInput.addEventListener("change", renderFiles);
  dom.ontologyInput.addEventListener("change", () => {
    if (dom.ontologyInput.files?.length) dom.publishedOntologySelect.value = "";
  });
  dom.publishedOntologySelect.addEventListener("change", () => {
    if (dom.publishedOntologySelect.value) dom.ontologyInput.value = "";
    renderFiles();
  });
  dom.refreshAssetsButton.addEventListener("click", loadPublishedAssets);
  dom.newTaskButton.addEventListener("click", startNewTask);
  dom.newTaskInConversationButton.addEventListener("click", startTaskInConversation);
  dom.conversationTaskSelect.addEventListener("change", async () => {
    state.taskId = dom.conversationTaskSelect.value || null;
    dom.composerMode.textContent = state.taskId ? "继续 / 修改当前任务" : "当前会话中的新任务";
    if (state.taskId) await loadSnapshot(true);
  });
  dom.refreshTasksButton.addEventListener("click", loadTasks);
  dom.taskList.addEventListener("click", (event) => {
    const menu = event.target.closest("[data-conversation-menu-id]");
    if (menu) {
      const rect = menu.getBoundingClientRect();
      openConversationMenu(menu.dataset.conversationMenuId, rect.left, rect.bottom + 4);
      return;
    }
    const item = event.target.closest("[data-conversation-id]");
    if (item) selectConversation(item.dataset.conversationId);
  });
  dom.taskList.addEventListener("contextmenu", (event) => {
    const item = event.target.closest("[data-conversation-id]") || event.target.closest("[data-conversation-menu-id]");
    if (!item) return;
    event.preventDefault();
    openConversationMenu(item.dataset.conversationId || item.dataset.conversationMenuId, event.clientX, event.clientY);
  });
  dom.conversationMenu.addEventListener("click", (event) => {
    const rename = event.target.closest("[data-rename-conversation-id]");
    const deletion = event.target.closest("[data-delete-conversation-id]");
    dom.conversationMenu.hidden = true;
    if (rename) renameConversation(rename.dataset.renameConversationId);
    if (deletion) deleteConversation(deletion.dataset.deleteConversationId);
  });
  document.addEventListener("click", (event) => {
    if (!event.target.closest("#conversation-menu, [data-conversation-menu-id]")) dom.conversationMenu.hidden = true;
  });
  document.querySelectorAll(".example-chip").forEach((button) => button.addEventListener("click", () => {
    dom.messageInput.value = button.textContent.trim();
    dom.messageInput.focus();
  }));
  document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach((item) => item.classList.toggle("is-active", item === tab));
    state.activeTab = tab.dataset.tab;
    renderInspector();
  }));
  dom.inspectorContent.addEventListener("click", (event) => {
    const artifact = event.target.closest("[data-artifact-id]");
    const review = event.target.closest("[data-review-id]");
    if (artifact) previewArtifact(artifact.dataset.artifactId);
    if (review) openReview(review.dataset.reviewId);
  });
  dom.conversation.addEventListener("click", (event) => {
    const artifact = event.target.closest("[data-artifact-id]");
    if (artifact) previewArtifact(artifact.dataset.artifactId);
  });
  document.querySelector("#close-artifact-dialog").addEventListener("click", () => dom.artifactDialog.close());
  document.querySelector("#approve-review-button").addEventListener("click", () => resolveReview("APPROVE"));
  document.querySelector("#reject-review-button").addEventListener("click", () => resolveReview("REJECT"));
  document.querySelector("#changes-review-button").addEventListener("click", () => resolveReview("SUBMIT_CHANGES"));
  dom.messageInput.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) dom.composer.requestSubmit();
  });
}

/** 初始化工作台；兼容旧 task 链接，并优先恢复会话级历史。 */
async function initialize() {
  bindEvents();
  window.setInterval(updateElapsedIndicators, 1000);
  window.addEventListener("kg-agent-extension-published", loadCustomCapabilityNames);
  await Promise.all([checkHealth(), loadTasks(), loadPublishedAssets(), loadCustomCapabilityNames()]);
  const params = new URLSearchParams(location.search);
  const conversationId = params.get("conversation");
  const taskId = params.get("task");
  if (conversationId) await selectConversation(conversationId);
  else if (taskId) {
    try {
      const snapshot = await api(`/api/v1/tasks/${encodeURIComponent(taskId)}`);
      if (snapshot.master_task?.conversation_id) await selectConversation(snapshot.master_task.conversation_id);
    } catch (error) {
      showToast(error.message, true);
    }
  }
}

initialize();
