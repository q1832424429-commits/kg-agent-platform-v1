/** 已发布资产目录：查看正文、调整显示名称或移出目录；正文始终不可变。 */

"use strict";

const dom = {
  notice: document.querySelector("#notice"),
  ontologyList: document.querySelector("#ontology-list"),
  graphList: document.querySelector("#graph-list"),
  ontologyCount: document.querySelector("#ontology-count"),
  graphCount: document.querySelector("#graph-count"),
  previewPanel: document.querySelector("#preview-panel"),
  previewTitle: document.querySelector("#preview-title"),
  previewContent: document.querySelector("#preview-content"),
  closePreview: document.querySelector("#close-preview"),
};

/** 把服务端或资产名称作为文本安全插入页面。 */
function text(value) {
  return String(value ?? "");
}

/** 统一读取 JSON API，并保留后端可读领域错误。 */
async function api(path, options = {}) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const payload = await response.json().catch(() => ({}));
    throw new Error(payload.message || payload.detail || `请求失败（HTTP ${response.status}）`);
  }
  return response.json();
}

/** 生成用户可读的发布日期，不把内部任务 ID 当作业务名称展示。 */
function displayTime(value) {
  return value ? new Date(value).toLocaleString("zh-CN") : "时间未知";
}

/** 渲染已发布资产的预览与受控管理动作。 */
function renderList(container, items, kind) {
  container.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "empty";
    empty.textContent = kind === "ontology" ? "暂无已发布本体。" : "暂无已发布图谱。";
    container.append(empty);
    return;
  }
  for (const item of items) {
    const card = document.createElement("article");
    card.className = "asset-card";
    const title = document.createElement("strong");
    title.textContent = item.publication_title || item.content_summary || "未命名资产";
    const details = document.createElement("span");
    details.className = "meta";
    details.textContent = kind === "ontology"
      ? `本体版本：${item.ontology_version || "未标注"} · 发布于 ${displayTime(item.created_at)}`
      : `发布于 ${displayTime(item.created_at)}`;
    const actions = document.createElement("div");
    actions.className = "asset-actions";
    const json = document.createElement("button");
    json.className = "secondary";
    json.textContent = "查看 JSON";
    json.onclick = () => previewJson(item);
    actions.append(json);
    if (kind === "graph") {
      const graph = document.createElement("button");
      graph.textContent = "交互预览图谱";
      graph.onclick = () => previewGraph(item);
      actions.append(graph);
    }
    const rename = document.createElement("button");
    rename.className = "secondary";
    rename.textContent = "修改名称";
    rename.onclick = () => renameAsset(item);
    actions.append(rename);
    const remove = document.createElement("button");
    remove.className = "danger";
    remove.textContent = "删除";
    remove.onclick = () => removeAsset(item);
    actions.append(remove);
    card.append(title, details, actions);
    container.append(card);
  }
}

/** 修改目录名称；已审核 JSON 中的发布名称、checksum 和历史任务保持原样。 */
async function renameAsset(item) {
  const title = window.prompt("新的资产显示名称", item.publication_title || "")?.trim();
  if (!title || title === item.publication_title) return;
  try {
    await api(`/api/v1/published-assets/${encodeURIComponent(item.artifact_id)}`, {
      method: "PATCH", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title }),
    });
    await load();
  } catch (error) {
    dom.notice.textContent = error.message;
    dom.notice.classList.add("error");
  }
}

/** 从可复用目录移除资产，不删除已被任务引用的不可变版本。 */
async function removeAsset(item) {
  if (!window.confirm(`将「${item.publication_title || "未命名资产"}」从资产目录删除？新任务将无法选择它；既有任务引用和审计版本会保留。`)) return;
  try {
    await api(`/api/v1/published-assets/${encodeURIComponent(item.artifact_id)}`, { method: "DELETE" });
    dom.previewPanel.hidden = true;
    await load();
  } catch (error) {
    dom.notice.textContent = error.message;
    dom.notice.classList.add("error");
  }
}

/** 显示完整但只读的发布资产 JSON，方便检查审核后真正固化的内容。 */
async function previewJson(item) {
  dom.previewTitle.textContent = item.publication_title || item.content_summary || "资产 JSON";
  dom.previewPanel.hidden = false;
  dom.previewContent.textContent = "正在读取资产内容…";
  try {
    const response = await fetch(`/api/v1/published-assets/${encodeURIComponent(item.artifact_id)}/content`);
    if (!response.ok) throw new Error(`内容读取失败（HTTP ${response.status}）`);
    const content = JSON.stringify(await response.json(), null, 2);
    const pre = document.createElement("pre");
    pre.className = "asset-json";
    pre.textContent = content;
    dom.previewContent.replaceChildren(pre);
  } catch (error) {
    dom.previewContent.textContent = error.message;
  }
}

/** 使用共享 G6 组件渲染正式 PublishedGraph 的只读、可拖拽预览。 */
async function previewGraph(item) {
  dom.previewTitle.textContent = item.publication_title || item.content_summary || "已发布图谱";
  dom.previewPanel.hidden = false;
  dom.previewContent.textContent = "正在加载图谱预览…";
  try {
    const graph = await api(`/api/v1/published-assets/${encodeURIComponent(item.artifact_id)}/graph-preview`);
    const slot = document.createElement("div");
    slot.className = "graph-container";
    dom.previewContent.replaceChildren(slot);
    window.KGGraphView?.render(slot, graph);
  } catch (error) {
    dom.previewContent.textContent = error.message;
  }
}

/** 读取两个正式资产目录；导入副本已由后端确定性过滤。 */
async function load() {
  try {
    const [ontologies, graphs] = await Promise.all([
      api("/api/v1/published-assets/ontologies"),
      api("/api/v1/published-assets/graphs"),
    ]);
    dom.ontologyCount.textContent = text(ontologies.length);
    dom.graphCount.textContent = text(graphs.length);
    renderList(dom.ontologyList, ontologies, "ontology");
    renderList(dom.graphList, graphs, "graph");
    dom.notice.textContent = "可修改目录显示名称或移除资产；已审核正文不可直接覆盖，内容修订须重新审核发布。";
    dom.notice.classList.remove("error");
  } catch (error) {
    dom.notice.textContent = error.message;
    dom.notice.classList.add("error");
  }
}

dom.closePreview.addEventListener("click", () => { dom.previewPanel.hidden = true; });
load();
