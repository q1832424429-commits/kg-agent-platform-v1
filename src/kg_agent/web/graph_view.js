/*
 * 提供正式端与测试端共用的 G6 知识图谱工作台组件。
 *
 * 本模块只把服务端返回的 GraphPreview 转换为浏览器图形交互；节点拖拽仅调整临时视图，
 * 不会写入不可变 CandidateGraph。实体、关系和属性的业务修改会被收集为既有
 * GraphChangeSet 操作，并且仍需由人工审核服务和图谱管理更新 Agent 处理。
 */

(() => {
  "use strict";

  /** G6 在同一容器重复渲染前必须销毁旧实例，避免残留画布和事件监听器。 */
  const graphInstances = new WeakMap();

  /** 使用稳定调色板区分实体类型，避免让类型颜色随每次布局变化。 */
  function colorForType(type) {
    const palette = ["#39d9ac", "#69a8ff", "#f3bf5c", "#d695ff", "#ff7d92", "#61d4dc"];
    let hash = 0;
    for (const character of String(type || "unknown")) {
      hash = ((hash << 5) - hash) + character.charCodeAt(0);
    }
    return palette[Math.abs(hash) % palette.length];
  }

  /** 转义详情面板中的动态文本，防止 Artifact 内容被当作 HTML 解释。 */
  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#039;");
  }

  /** 复制服务端投影，所有浏览器编辑只在这份草稿上进行。 */
  function clonePreview(preview) {
    if (globalThis.structuredClone) return globalThis.structuredClone(preview);
    return JSON.parse(JSON.stringify(preview));
  }

  /** 生成 ChangeOperation 的稳定可追踪标识。 */
  function operationId() {
    return globalThis.crypto?.randomUUID?.()
      || `graph-ui-${Date.now()}-${Math.random().toString(16).slice(2)}`;
  }

  /** 给用户新建的图元素生成与现有 Artifact 不冲突的临时业务标识。 */
  function draftId(prefix) {
    return `${prefix}-ui-${globalThis.crypto?.randomUUID?.() || Date.now()}`;
  }

  /** 返回编辑预览中指定 ID 的节点，缺失时返回 undefined。 */
  function nodeById(draft, id) {
    return draft.nodes.find((node) => node.id === id);
  }

  /** 返回编辑预览中指定 ID 的边，缺失时返回 undefined。 */
  function edgeById(draft, id) {
    return draft.edges.find((edge) => edge.id === id);
  }

  /** 将证据列表变为可读摘要，不回显完整文档或模型隐藏推理。 */
  function evidenceSummary(evidence) {
    if (!evidence?.length) return "暂无证据";
    return evidence
      .map((item) => `${item.locator || "未知位置"}${item.quote ? `：${item.quote}` : ""}`)
      .join("\n");
  }

  /** 把可视化节点映射为 G6 数据；data 保留业务字段，style 只控制显示。 */
  function toG6Nodes(nodes) {
    return nodes.map((node) => ({
      id: node.id,
      data: node,
      type: "circle",
      style: {
        size: 46,
        fill: colorForType(node.class_id),
        stroke: "#e9fff8",
        lineWidth: 1.5,
        shadowColor: "rgba(0, 0, 0, 0.32)",
        shadowBlur: 10,
        labelText: String(node.label || node.id),
        labelFill: "#eaf7f3",
        labelFontSize: 12,
        labelPlacement: "bottom",
        labelMaxWidth: 108,
        labelWordWrap: true,
        labelBackground: true,
        labelBackgroundFill: "rgba(5, 16, 18, 0.78)",
        labelBackgroundRadius: 4,
        labelPadding: [2, 4],
      },
    }));
  }

  /** 把可视化关系映射为 G6 数据，并让所有关系带方向箭头与可读标签。 */
  function toG6Edges(edges) {
    return edges.map((edge) => ({
      id: edge.id,
      source: edge.source,
      target: edge.target,
      data: edge,
      type: edge.source === edge.target ? "quadratic" : "line",
      style: {
        stroke: "rgba(166, 210, 201, 0.78)",
        lineWidth: 1.45,
        endArrow: true,
        endArrowFill: "rgba(166, 210, 201, 0.86)",
        labelText: String(edge.label || edge.relation_type_id),
        labelFill: "#c2d9d4",
        labelFontSize: 10,
        labelBackground: true,
        labelBackgroundFill: "rgba(5, 16, 18, 0.84)",
        labelBackgroundRadius: 3,
        labelPadding: [2, 4],
      },
    }));
  }

  /** 根据用户选中的布局类型生成 G6 配置；手动布局不会重置拖拽后的坐标。 */
  function layoutFor(name) {
    const layouts = {
      force: {
        type: "d3-force",
        link: { distance: 150 },
        manyBody: { strength: -520 },
        collide: { radius: 42 },
        alphaDecay: 0.028,
      },
      dagre: {
        type: "dagre",
        rankdir: "LR",
        nodesep: 46,
        ranksep: 110,
      },
      circular: {
        type: "circular",
        radius: 220,
        ordering: "degree",
      },
      preset: { type: "preset" },
    };
    return layouts[name] || layouts.force;
  }

  /** 把当前草稿筛选为用户真正想观察的局部可视元素。 */
  function visibleData(draft, typeFilter, searchText) {
    const needle = searchText.trim().toLocaleLowerCase();
    const nodes = draft.nodes.filter((node) => {
      const byType = !typeFilter || node.class_id === typeFilter;
      const searchable = [node.label, node.class_id, ...(node.aliases || [])]
        .join(" ")
        .toLocaleLowerCase();
      return byType && (!needle || searchable.includes(needle));
    });
    const nodeIds = new Set(nodes.map((node) => node.id));
    return {
      nodes,
      edges: draft.edges.filter((edge) => nodeIds.has(edge.source) && nodeIds.has(edge.target)),
    };
  }

  /** 返回可展示的操作摘要，方便用户在提交前人工复核修改草稿。 */
  function operationLabel(operation) {
    const target = operation.target_id || operation.value?.entity_id || operation.value?.relation_id || "新元素";
    const kind = { ENTITY: "实体", RELATION: "关系", PROPERTY: "属性" }[operation.target_kind]
      || operation.target_kind;
    const action = {
      ADD: "新增", UPDATE: "修改", DELETE: "删除", MERGE: "合并", SPLIT: "拆分",
    }[operation.action] || operation.action;
    return `${action}${kind}：${target}`;
  }

  /** 在指定容器创建具备拖拽、布局和审核编辑能力的 G6 图谱工作台。 */
  function render(container, preview, options = {}) {
    if (!container) return;
    const previous = graphInstances.get(container);
    if (previous?.graph && !previous.graph.destroyed) previous.graph.destroy();
    if (!globalThis.G6?.Graph) {
      container.innerHTML = '<div class="kg-graph-unavailable">图谱引擎未加载，请刷新页面后重试。</div>';
      return;
    }

    const draft = clonePreview(preview || { nodes: [], edges: [], statistics: {}, warnings: [] });
    const state = {
      graph: null, draft, operations: [], redoOperations: [], selectedNodeIds: new Set(),
      selectedEdgeIds: new Set(), layout: "force", filter: "", search: "",
    };
    container.innerHTML = `
      <section class="kg-workbench" aria-label="知识图谱工作台">
        <header class="kg-workbench-header">
          <div><strong>知识图谱预览</strong><span class="kg-artifact-meta">${escapeHtml(draft.artifact_type || "CandidateGraph")}</span></div>
          <div class="kg-workbench-stats">${draft.statistics?.entity_count ?? draft.nodes.length} 实体 · ${draft.statistics?.relation_count ?? draft.edges.length} 关系</div>
        </header>
        <div class="kg-workbench-toolbar">
          <input class="kg-search" type="search" placeholder="搜索实体、别名或类型" aria-label="搜索实体" />
          <select class="kg-type-filter" aria-label="按实体类型筛选"><option value="">全部实体类型</option></select>
          <select class="kg-layout" aria-label="选择图谱布局"><option value="force">力导向布局</option><option value="dagre">层级布局</option><option value="circular">环形布局</option><option value="preset">手动布局</option></select>
          <button type="button" class="kg-tool" data-graph-command="fit">适配画布</button>
          <button type="button" class="kg-tool" data-graph-command="clear">清除选择</button>
          ${options.editable ? `<span class="kg-toolbar-divider"></span><button type="button" class="kg-tool" data-graph-command="add-entity">新增实体</button><button type="button" class="kg-tool" data-graph-command="add-relation">连接实体</button><button type="button" class="kg-tool" data-graph-command="merge">合并实体</button><button type="button" class="kg-tool" data-graph-command="split">拆分实体</button><button type="button" class="kg-tool danger" data-graph-command="delete">删除所选</button><button type="button" class="kg-tool" data-graph-command="undo">撤销</button><button type="button" class="kg-tool" data-graph-command="redo">重做</button>` : ""}
        </div>
        <div class="kg-workbench-body"><div class="kg-canvas-host" aria-label="可拖拽知识图谱画布"></div><aside class="kg-inspector"><div class="kg-inspector-empty">点击节点或关系可查看详情、属性和来源证据。</div></aside></div>
        <footer class="kg-workbench-footer"><div class="kg-warning-list"></div>${options.editable ? '<div class="kg-draft-list"></div>' : ""}</footer>
      </section>`;

    const searchInput = container.querySelector(".kg-search");
    const filterInput = container.querySelector(".kg-type-filter");
    const layoutInput = container.querySelector(".kg-layout");
    const canvas = container.querySelector(".kg-canvas-host");
    const inspector = container.querySelector(".kg-inspector");
    const warningList = container.querySelector(".kg-warning-list");
    const draftList = container.querySelector(".kg-draft-list");

    /** 将整个操作数组同步给页面审核表单；不直接调用后端或写图谱 Artifact。 */
    function notifyOperations() {
      if (options.onOperationsChange) options.onOperationsChange([...state.operations]);
      else if (options.onChangeOperation && state.operations.length) options.onChangeOperation(state.operations.at(-1));
    }

    /** 更新工具栏按钮可用性与用户可审查的修改摘要。 */
    function updateDraftPanel() {
      const undo = container.querySelector('[data-graph-command="undo"]');
      const redo = container.querySelector('[data-graph-command="redo"]');
      if (undo) undo.disabled = state.operations.length === 0;
      if (redo) redo.disabled = state.redoOperations.length === 0;
      if (!draftList) return;
      draftList.innerHTML = state.operations.length
        ? `<strong>待提交修改（${state.operations.length}）</strong><ol>${state.operations.map((operation, index) => `<li><span>${escapeHtml(operationLabel(operation))}</span><button type="button" data-remove-operation="${index}">移除</button></li>`).join("")}</ol>`
        : '<span class="kg-draft-empty">暂无业务修改。拖拽节点只调整当前浏览器视图，不会修改图谱。</span>';
      draftList.querySelectorAll("[data-remove-operation]").forEach((button) => button.addEventListener("click", () => {
        const index = Number(button.dataset.removeOperation);
        if (!Number.isInteger(index)) return;
        state.redoOperations.push(state.operations[index]);
        state.operations.splice(index, 1);
        rebuildDraft();
        notifyOperations();
      }));
    }

    /** 渲染告警，告警仅提示历史数据问题，不能替代质量评估或审核。 */
    function updateWarnings() {
      warningList.innerHTML = (draft.warnings || []).length
        ? `<div class="kg-warnings">${draft.warnings.map((warning) => `<div>⚠ ${escapeHtml(warning.message)}</div>`).join("")}</div>`
        : "";
    }

    /** 根据当前选择显示实体、关系及可编辑操作。 */
    function updateInspector() {
      const selectedNode = state.selectedNodeIds.size === 1 ? nodeById(draft, [...state.selectedNodeIds][0]) : null;
      const selectedEdge = state.selectedEdgeIds.size === 1 ? edgeById(draft, [...state.selectedEdgeIds][0]) : null;
      if (selectedNode) {
        const properties = selectedNode.properties?.length
          ? selectedNode.properties.map((property) => `<li><b>${escapeHtml(property.property_type_id)}</b><span>${escapeHtml(JSON.stringify(property.value))}</span></li>`).join("")
          : "<li>暂无属性</li>";
        inspector.innerHTML = `<div class="kg-inspector-heading"><strong>${escapeHtml(selectedNode.label)}</strong><span>${escapeHtml(selectedNode.class_id)}</span></div><div class="kg-inspector-meta">ID：${escapeHtml(selectedNode.id)}<br />别名：${escapeHtml(selectedNode.aliases?.join("、") || "无")}</div><section><h4>属性</h4><ul>${properties}</ul></section><section><h4>证据</h4><pre>${escapeHtml(evidenceSummary(selectedNode.evidence))}</pre></section>${options.editable ? '<div class="kg-inspector-actions"><button type="button" data-inspector-action="edit-node">编辑实体</button><button type="button" data-inspector-action="edit-property">新增/修改属性</button></div>' : ""}`;
      } else if (selectedEdge) {
        inspector.innerHTML = `<div class="kg-inspector-heading"><strong>${escapeHtml(selectedEdge.label)}</strong><span>关系</span></div><div class="kg-inspector-meta">${escapeHtml(selectedEdge.source)} → ${escapeHtml(selectedEdge.target)}<br />ID：${escapeHtml(selectedEdge.id)}</div><section><h4>证据</h4><pre>${escapeHtml(evidenceSummary(selectedEdge.evidence))}</pre></section>${options.editable ? '<div class="kg-inspector-actions"><button type="button" data-inspector-action="edit-edge">编辑关系</button></div>' : ""}`;
      } else if (state.selectedNodeIds.size > 1) {
        inspector.innerHTML = `<div class="kg-inspector-heading"><strong>已选择 ${state.selectedNodeIds.size} 个实体</strong></div><p>可使用“连接实体”或“合并实体”。按住 Shift 点击可以继续多选。</p>`;
      } else {
        inspector.innerHTML = '<div class="kg-inspector-empty">点击节点或关系可查看详情、属性和来源证据。</div>';
      }
      inspector.querySelector("[data-inspector-action='edit-node']")?.addEventListener("click", editSelectedNode);
      inspector.querySelector("[data-inspector-action='edit-property']")?.addEventListener("click", editSelectedProperty);
      inspector.querySelector("[data-inspector-action='edit-edge']")?.addEventListener("click", editSelectedEdge);
    }

    /** 销毁旧画布并从当前草稿重新生成 G6 图；业务草稿保持不变。 */
    async function drawGraph({ fit = true } = {}) {
      if (state.graph && !state.graph.destroyed) state.graph.destroy();
      const visible = visibleData(draft, state.filter, state.search);
      const width = Math.max(canvas.clientWidth || 720, 520);
      const height = Math.max(canvas.clientHeight || 500, 440);
      canvas.replaceChildren();
      state.graph = new globalThis.G6.Graph({
        container: canvas, width, height,
        data: { nodes: toG6Nodes(visible.nodes), edges: toG6Edges(visible.edges) },
        layout: layoutFor(state.layout),
        behaviors: ["drag-canvas", { type: "zoom-canvas", sensitivity: 1.15 }, "drag-element", "click-select", { type: "hover-activate", degree: 1 }],
        node: { state: { selected: { halo: true, haloStroke: "#ffffff", haloLineWidth: 3 } } },
        edge: { state: { selected: { stroke: "#47eaba", lineWidth: 3 } } },
        animation: false,
      });
      state.graph.on("node:click", (event) => selectNode(event));
      state.graph.on("edge:click", (event) => selectEdge(event));
      state.graph.on("canvas:click", () => { state.selectedNodeIds.clear(); state.selectedEdgeIds.clear(); updateInspector(); });
      await state.graph.render();
      if (fit && visible.nodes.length) state.graph.fitView({ padding: 44 });
      graphInstances.set(container, state);
    }

    /** 选择节点；Shift 多选用于连线、合并等编辑动作。 */
    function selectNode(event) {
      const id = event.target?.id;
      if (!id) return;
      const multi = Boolean(event.originalEvent?.shiftKey || event.nativeEvent?.shiftKey);
      if (!multi) { state.selectedNodeIds.clear(); state.selectedEdgeIds.clear(); }
      if (multi && state.selectedNodeIds.has(id)) state.selectedNodeIds.delete(id);
      else state.selectedNodeIds.add(id);
      updateInspector();
    }

    /** 选择一条关系；关系选择与实体多选互斥，避免编辑目标产生歧义。 */
    function selectEdge(event) {
      const id = event.target?.id;
      if (!id) return;
      state.selectedNodeIds.clear(); state.selectedEdgeIds.clear(); state.selectedEdgeIds.add(id);
      updateInspector();
    }

    /** 重新从原始 Preview 和未撤销操作构造草稿，确保撤销不会遗漏级联影响。 */
    function rebuildDraft() {
      const original = clonePreview(preview || { nodes: [], edges: [], statistics: {}, warnings: [] });
      draft.nodes = original.nodes || [];
      draft.edges = original.edges || [];
      draft.statistics = original.statistics || {};
      draft.warnings = original.warnings || [];
      state.operations.forEach((operation) => applyDraftOperation(operation));
      state.selectedNodeIds.clear(); state.selectedEdgeIds.clear();
      updateDraftPanel(); updateInspector(); drawGraph();
    }

    /** 对浏览器草稿应用一个 GraphChangeSet 操作，便于用户在提交前查看实际差异。 */
    function applyDraftOperation(operation) {
      const value = operation.value || {};
      if (operation.action === "ADD" && operation.target_kind === "ENTITY") {
        if (!nodeById(draft, operation.target_id)) draft.nodes.push({ id: operation.target_id, label: value.name || operation.target_id, class_id: value.class_id || "Unclassified", aliases: value.aliases || [], properties: [], evidence: [] });
      } else if (operation.action === "ADD" && operation.target_kind === "RELATION") {
        if (!edgeById(draft, operation.target_id)) draft.edges.push({ id: operation.target_id, source: value.source_entity_id, target: value.target_entity_id, label: value.relation_type_id || "RELATED_TO", relation_type_id: value.relation_type_id || "RELATED_TO", evidence: [] });
      } else if (operation.action === "ADD" && operation.target_kind === "PROPERTY") {
        const owner = nodeById(draft, value.entity_id);
        if (owner) owner.properties = [...(owner.properties || []), { ...value, property_id: operation.target_id, evidence: [] }];
      } else if (operation.action === "UPDATE" && operation.target_kind === "ENTITY") {
        const node = nodeById(draft, operation.target_id);
        if (node) Object.assign(node, value, { label: value.name ?? node.label });
      } else if (operation.action === "UPDATE" && operation.target_kind === "RELATION") {
        const edge = edgeById(draft, operation.target_id);
        if (edge) Object.assign(edge, value, { label: value.relation_type_id ?? edge.label });
      } else if (operation.action === "UPDATE" && operation.target_kind === "PROPERTY") {
        draft.nodes.forEach((node) => { node.properties = (node.properties || []).map((property) => property.property_id === operation.target_id ? { ...property, ...value } : property); });
      } else if (operation.action === "DELETE" && operation.target_kind === "ENTITY") {
        draft.nodes = draft.nodes.filter((node) => node.id !== operation.target_id);
        draft.edges = draft.edges.filter((edge) => edge.source !== operation.target_id && edge.target !== operation.target_id);
      } else if (operation.action === "DELETE" && operation.target_kind === "RELATION") {
        draft.edges = draft.edges.filter((edge) => edge.id !== operation.target_id);
      } else if (operation.action === "DELETE" && operation.target_kind === "PROPERTY") {
        draft.nodes.forEach((node) => { node.properties = (node.properties || []).filter((property) => property.property_id !== operation.target_id); });
      } else if (operation.action === "MERGE" && operation.target_kind === "ENTITY") {
        const primaryId = operation.target_id;
        const sourceIds = new Set(value.source_entity_ids || []);
        draft.edges.forEach((edge) => { if (sourceIds.has(edge.source)) edge.source = primaryId; if (sourceIds.has(edge.target)) edge.target = primaryId; });
        const primary = nodeById(draft, primaryId);
        draft.nodes.filter((node) => sourceIds.has(node.id)).forEach((node) => {
          if (primary) {
            primary.aliases = [...new Set([...(primary.aliases || []), node.label, ...(node.aliases || [])])];
            primary.properties = [...(primary.properties || []), ...(node.properties || [])].map((property) => ({ ...property, entity_id: primaryId }));
          }
        });
        draft.nodes = draft.nodes.filter((node) => !sourceIds.has(node.id));
      } else if (operation.action === "SPLIT" && operation.target_kind === "ENTITY") {
        const replacements = value.entities || [];
        const firstId = replacements[0]?.entity_id;
        if (!firstId) return;
        draft.nodes = draft.nodes.filter((node) => node.id !== operation.target_id);
        draft.nodes.push(...replacements.map((node) => ({ id: node.entity_id, label: node.name, class_id: node.class_id, aliases: node.aliases || [], properties: node.properties || [], evidence: node.evidence || [] })));
        draft.edges.forEach((edge) => { if (edge.source === operation.target_id) edge.source = firstId; if (edge.target === operation.target_id) edge.target = firstId; });
      }
    }

    /** 追加一个业务操作、清空重做栈，并立即让用户看到草稿图。 */
    function commitOperation(operation) {
      state.operations.push(operation); state.redoOperations = []; rebuildDraft(); notifyOperations();
    }

    /** 新增实体并附加为 ADD ENTITY 操作；证据只能由后续抽取或人工流程补充。 */
    function addEntity() {
      const name = globalThis.prompt("实体名称");
      if (!name?.trim()) return;
      const classId = globalThis.prompt("实体类型", "Unclassified")?.trim() || "Unclassified";
      const id = draftId("entity");
      commitOperation({ operation_id: operationId(), action: "ADD", target_kind: "ENTITY", target_id: id, value: { entity_id: id, name: name.trim(), class_id: classId, aliases: [] } });
    }

    /** 对一个选中实体修改名称和类型，属性编辑交由独立操作避免隐式覆盖。 */
    function editSelectedNode() {
      const node = state.selectedNodeIds.size === 1 ? nodeById(draft, [...state.selectedNodeIds][0]) : null;
      if (!node) return;
      const name = globalThis.prompt("实体名称", node.label);
      if (!name?.trim()) return;
      const classId = globalThis.prompt("实体类型", node.class_id);
      if (!classId?.trim()) return;
      commitOperation({ operation_id: operationId(), action: "UPDATE", target_kind: "ENTITY", target_id: node.id, value: { name: name.trim(), class_id: classId.trim() } });
    }

    /** 增加新属性或更新同类型现有属性，值允许通过 JSON 表达数字、对象和数组。 */
    function editSelectedProperty() {
      const node = state.selectedNodeIds.size === 1 ? nodeById(draft, [...state.selectedNodeIds][0]) : null;
      if (!node) return;
      const propertyType = globalThis.prompt("属性名称/类型，例如 serial_no");
      if (!propertyType?.trim()) return;
      const existing = (node.properties || []).find((item) => item.property_type_id === propertyType.trim());
      const rawValue = globalThis.prompt("属性值（支持 JSON；普通文字可直接输入）", existing ? JSON.stringify(existing.value) : "");
      if (rawValue === null) return;
      let value = rawValue;
      try { value = JSON.parse(rawValue); } catch (_) { /* 普通字符串无需 JSON 解析。 */ }
      if (existing) commitOperation({ operation_id: operationId(), action: "UPDATE", target_kind: "PROPERTY", target_id: existing.property_id, value: { value } });
      else {
        const id = draftId("property");
        commitOperation({ operation_id: operationId(), action: "ADD", target_kind: "PROPERTY", target_id: id, value: { property_id: id, entity_id: node.id, property_type_id: propertyType.trim(), value } });
      }
    }

    /** 连接恰好两个选择节点；第一个是起点，第二个是终点。 */
    function addRelation() {
      const ids = [...state.selectedNodeIds];
      if (ids.length !== 2) { globalThis.alert("请按住 Shift 依次选择两个实体，再创建关系。"); return; }
      const relationType = globalThis.prompt("关系类型", "RELATED_TO");
      if (!relationType?.trim()) return;
      const id = draftId("relation");
      commitOperation({ operation_id: operationId(), action: "ADD", target_kind: "RELATION", target_id: id, value: { relation_id: id, source_entity_id: ids[0], target_entity_id: ids[1], relation_type_id: relationType.trim() } });
    }

    /** 编辑当前关系类型；端点修改应通过删除后重新连线，保持操作语义清晰。 */
    function editSelectedEdge() {
      const edge = state.selectedEdgeIds.size === 1 ? edgeById(draft, [...state.selectedEdgeIds][0]) : null;
      if (!edge) return;
      const relationType = globalThis.prompt("关系类型", edge.relation_type_id);
      if (!relationType?.trim()) return;
      commitOperation({ operation_id: operationId(), action: "UPDATE", target_kind: "RELATION", target_id: edge.id, value: { relation_type_id: relationType.trim() } });
    }

    /** 合并多个实体到第一个选中实体，关系端点、属性与别名在草稿中可见。 */
    function mergeNodes() {
      const ids = [...state.selectedNodeIds];
      if (ids.length < 2) { globalThis.alert("请按住 Shift 选择至少两个实体，再执行合并。"); return; }
      const primaryId = ids[0];
      if (!globalThis.confirm(`将其余 ${ids.length - 1} 个实体合并到“${nodeById(draft, primaryId)?.label || primaryId}”吗？`)) return;
      commitOperation({ operation_id: operationId(), action: "MERGE", target_kind: "ENTITY", target_id: primaryId, value: { source_entity_ids: ids.slice(1) } });
    }

    /** 拆分一个实体；默认将已有关系临时保留到第一个新实体，用户可继续调整。 */
    function splitNode() {
      const node = state.selectedNodeIds.size === 1 ? nodeById(draft, [...state.selectedNodeIds][0]) : null;
      if (!node) { globalThis.alert("请选择一个实体后再执行拆分。"); return; }
      const names = globalThis.prompt("请输入拆分后的实体名称，使用中文逗号或英文逗号分隔", `${node.label}（一）,${node.label}（二）`);
      if (!names?.trim()) return;
      const values = names.split(/[，,]/).map((item) => item.trim()).filter(Boolean);
      if (values.length < 2) { globalThis.alert("至少需要两个新实体。"); return; }
      const entities = values.map((name) => ({ entity_id: draftId("entity"), name, class_id: node.class_id, aliases: [], evidence: [] }));
      commitOperation({ operation_id: operationId(), action: "SPLIT", target_kind: "ENTITY", target_id: node.id, value: { entities } });
    }

    /** 删除当前所选实体或关系；实体删除的级联规则由后端再次确定性校验。 */
    function deleteSelection() {
      const operations = [];
      state.selectedNodeIds.forEach((id) => operations.push({ operation_id: operationId(), action: "DELETE", target_kind: "ENTITY", target_id: id, value: {} }));
      state.selectedEdgeIds.forEach((id) => operations.push({ operation_id: operationId(), action: "DELETE", target_kind: "RELATION", target_id: id, value: {} }));
      if (!operations.length || !globalThis.confirm(`确认删除当前选择的 ${operations.length} 项吗？`)) return;
      operations.forEach((operation) => state.operations.push(operation)); state.redoOperations = []; rebuildDraft(); notifyOperations();
    }

    /** 撤销最后一个业务修改；视图拖拽不属于业务修改，因此不进入此栈。 */
    function undo() {
      const operation = state.operations.pop();
      if (!operation) return;
      state.redoOperations.push(operation); rebuildDraft(); notifyOperations();
    }

    /** 重新应用最近撤销的业务修改。 */
    function redo() {
      const operation = state.redoOperations.pop();
      if (!operation) return;
      state.operations.push(operation); rebuildDraft(); notifyOperations();
    }

    /** 将工具栏事件集中到一个命令入口，避免业务操作散落在 DOM 回调中。 */
    function bindToolbar() {
      const classes = [...new Set(draft.nodes.map((node) => node.class_id).filter(Boolean))].sort();
      filterInput.innerHTML = `<option value="">全部实体类型</option>${classes.map((item) => `<option value="${escapeHtml(item)}">${escapeHtml(item)}</option>`).join("")}`;
      searchInput.addEventListener("input", () => { state.search = searchInput.value; drawGraph(); });
      filterInput.addEventListener("change", () => { state.filter = filterInput.value; drawGraph(); });
      layoutInput.addEventListener("change", () => { state.layout = layoutInput.value; drawGraph(); });
      container.querySelectorAll("[data-graph-command]").forEach((button) => button.addEventListener("click", () => {
        switch (button.dataset.graphCommand) {
          case "fit": state.graph?.fitView({ padding: 44 }); break;
          case "clear": state.selectedNodeIds.clear(); state.selectedEdgeIds.clear(); updateInspector(); break;
          case "add-entity": addEntity(); break;
          case "add-relation": addRelation(); break;
          case "merge": mergeNodes(); break;
          case "split": splitNode(); break;
          case "delete": deleteSelection(); break;
          case "undo": undo(); break;
          case "redo": redo(); break;
          default: break;
        }
      }));
    }

    bindToolbar(); updateWarnings(); updateDraftPanel(); updateInspector(); drawGraph();
  }

  window.KGGraphView = { render };
})();
