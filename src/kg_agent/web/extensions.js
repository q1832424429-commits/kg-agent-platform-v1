/** 自定义 Agent / Skill 独立管理页面：只调用受控定义包 API，不直接修改运行时状态。 */

const notice = document.querySelector("#notice");
const skillsList = document.querySelector("#skills-list");
const agentsList = document.querySelector("#agents-list");
// 管理入口通过 ?tab=skills 或 ?tab=agents 进入；无参数时保留完整总览页。
const selectedTab = new URLSearchParams(window.location.search).get("tab");

/** 根据管理入口裁剪页面，仅显示用户刚刚选择的 Agent 或 Skill 管理内容。 */
function applySelectedManagementTab() {
  if (selectedTab !== "skills" && selectedTab !== "agents") return;
  document.querySelectorAll("[data-extension-section]").forEach((section) => {
    section.hidden = section.dataset.extensionSection !== selectedTab;
  });
  document.title = selectedTab === "skills" ? "自定义 Skill 管理" : "自定义 Agent 管理";
  document.querySelector("h1").textContent = selectedTab === "skills" ? "自定义 Skill 管理" : "自定义 Agent 管理";
  notice.textContent = selectedTab === "skills" ? "正在读取当前环境的自定义 Skill…" : "正在读取当前环境的自定义 Agent…";
}

/** 显示当前操作的成功或失败状态，避免只在开发者工具中暴露错误。 */
function report(message, error = false) {
  notice.textContent = message;
  notice.classList.toggle("error", error);
}

/** 封装 JSON API；业务错误正文会直接展示给用户。 */
async function request(url, options = {}) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.message || payload.detail || `HTTP ${response.status}`);
  return payload;
}

/** 创建一个业务化编辑弹窗；内部 Artifact、任务类型和运行实现始终不展示给用户。 */
function createEditDialog(title) {
  const dialog = document.createElement("dialog");
  dialog.className = "extension-edit-dialog";
  const card = document.createElement("form");
  card.className = "extension-edit-card";
  card.method = "dialog";
  const heading = document.createElement("div");
  heading.className = "edit-heading";
  const h2 = document.createElement("h2"); h2.textContent = title;
  const close = document.createElement("button"); close.type = "button"; close.className = "secondary"; close.textContent = "取消";
  close.onclick = () => dialog.close();
  heading.append(h2, close);
  card.append(heading);
  dialog.append(card);
  document.body.append(dialog);
  return { dialog, card };
}
/** 以表格汇总脚本字段、标准字段及待确认状态，避免分散下拉框。 */
function createMappingTable(title, sourceTitle = "脚本字段", targetTitle = "平台字段") {
  const section = document.createElement("section");
  section.className = "extension-mapping-section";
  const heading = document.createElement("strong");
  heading.textContent = title;
  const scroll = document.createElement("div");
  scroll.className = "extension-mapping-scroll";
  const table = document.createElement("table");
  table.className = "extension-mapping-table";
  const head = document.createElement("thead");
  const header = document.createElement("tr");
  for (const caption of [sourceTitle, targetTitle, "状态"]) {
    const cell = document.createElement("th");
    cell.textContent = caption;
    header.append(cell);
  }
  head.append(header);
  const body = document.createElement("tbody");
  table.append(head, body);
  scroll.append(table);
  section.append(heading, scroll);
  return { section, body };
}

/** 用户只从合法候选中选择；建议值不代表通过试运行。 */
function appendMappingRow(body, field, select, { required = true, suggested = "", suggestionLabel = "系统建议，待确认", note = "" } = {}) {
  const row = document.createElement("tr");
  const name = document.createElement("td");
  name.textContent = field;
  const target = document.createElement("td");
  select.setAttribute("aria-label", `${field} 对应字段`);
  target.append(select);
  const state = document.createElement("td");
  state.className = "extension-mapping-status";
  const refresh = () => {
    const missing = !select.value;
    state.dataset.state = missing ? (required ? "missing" : "optional") : "pending";
    state.textContent = missing
      ? (required ? "缺失，需处理" : "未映射")
      : (select.value === suggested ? suggestionLabel : "已选择，待试运行");
  };
  select.addEventListener("change", refresh);
  refresh();
  row.append(name, target, state);
  body.append(row);
  if (note) {
    const explanation = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "extension-mapping-note";
    cell.textContent = note;
    explanation.append(cell);
    body.append(explanation);
  }
}


/** 添加不含 HTML 插值的文本输入，避免用户名称或描述进入 DOM 模板。 */
function appendField(card, { name, label, value = "", required = true, rows = 0, hint = "" }) {
  const wrapper = document.createElement("label");
  const caption = document.createElement("span"); caption.textContent = label;
  const control = rows ? document.createElement("textarea") : document.createElement("input");
  control.name = name; control.required = required; control.value = value || "";
  if (rows) control.rows = rows;
  wrapper.append(caption, control);
  if (hint) { const note = document.createElement("small"); note.textContent = hint; wrapper.append(note); }
  card.append(wrapper);
  return control;
}

/** 把资源字节数转换为适合管理页面阅读的大小，原始大小仍以服务端记录为准。 */
function formatResourceSize(sizeBytes) {
  const size = Number(sizeBytes || 0);
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  return `${(size / (1024 * 1024)).toFixed(1)} MB`;
}

/** 判断资源是否为可执行脚本；当前运行时只把 Python 文件作为脚本入口。 */
function isPythonResource(resource) {
  return String(resource.relative_path || resource.name || "").toLowerCase().endsWith(".py");
}

/** 管理页发布已有脚本前，只在映射缺失或与当前脚本字段不符时要求用户确认。 */
async function ensureScriptInputMapping(skillId, detail, force = false) {
  const proposal = await request(`/api/v1/extensions/skills/${skillId}/input-mapping-proposals`, {
    method: "POST",
  });
  if (!proposal.detected_fields.length) return;
  const existing = Array.isArray(detail.metadata?.script_input_bindings)
    ? detail.metadata.script_input_bindings : [];
  if (!force && proposal.detected_fields.every((field) => existing.some((item) => item.script_field === field))) {
    return;
  }
  if (!proposal.source_fields.length) {
    throw new Error("当前资料没有可供脚本字段使用的已注册内容，请先调整能力用途或脚本。");
  }
  const { dialog, card } = createEditDialog("确认脚本字段来源");
  const intro = document.createElement("p");
  intro.className = "edit-note";
  intro.textContent = "系统给出候选来源；请逐项确认，未确认前不会发布脚本。";
  const mapping = createMappingTable("输入字段映射", "脚本需要", "来自已有资料");
  card.append(intro, mapping.section);
  for (const field of proposal.detected_fields) {
    const select = document.createElement("select");
    select.dataset.scriptField = field;
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "请选择已有资料字段";
    select.append(empty);
    const suggested = existing.find((item) => item.script_field === field)
      || proposal.suggested_bindings.find((item) => item.script_field === field);
    for (const source of proposal.source_fields) {
      const option = document.createElement("option");
      option.value = JSON.stringify([source.artifact_type, source.source_pointer]);
      option.textContent = source.business_label;
      option.selected = Boolean(
        suggested && suggested.artifact_type === source.artifact_type
        && suggested.source_pointer === source.source_pointer
      );
      select.append(option);
    }
    appendMappingRow(mapping.body, field, select, {
      suggested: suggested ? JSON.stringify([suggested.artifact_type, suggested.source_pointer]) : "",
      suggestionLabel: existing.some((item) => item.script_field === field)
        ? "当前配置，待复测" : "系统建议，待确认",
      note: !suggested ? "无可靠建议；若资料没有该数据，请调整上游抽取或脚本。" : "",
    });
  }
  const confirm = document.createElement("button");
  confirm.type = "button";
  confirm.textContent = "确认字段来源";
  card.append(confirm);
  await new Promise((resolve, reject) => {
    let completed = false;
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (!completed) reject(new Error("尚未确认脚本字段来源"));
    }, { once: true });
    confirm.onclick = async () => {
      try {
        const bindings = [...card.querySelectorAll("select[data-script-field]")].map((select) => {
          if (!select.value) throw new Error(`请为 ${select.dataset.scriptField} 选择来源`);
          const [artifactType, sourcePointer] = JSON.parse(select.value);
          return {
            script_field: select.dataset.scriptField,
            artifact_type: artifactType,
            source_pointer: sourcePointer,
            required: true,
          };
        });
        await request(`/api/v1/extensions/skills/${skillId}/input-bindings`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(bindings),
        });
        completed = true;
        dialog.close();
        resolve();
      } catch (error) {
        report(`确认脚本字段来源失败：${error.message}`, true);
      }
    };
    dialog.showModal();
  });
}

/** 实体转换脚本只需确认函数及结果字段；平台保留实体身份和证据。 */
async function ensureEntityAdapter(skillId, detail) {
  if (detail.metadata?.contract_profile_id !== "entity_transform"
      || detail.metadata?.entity_record_adapter) return;
  const proposal = await request(`/api/v1/extensions/skills/${skillId}/entity-adapter-proposal`, {
    method: "POST",
  });
  if (proposal.standard_entrypoint) return;
  if (!proposal.functions.length) throw new Error("实体转换脚本需要一个只接收一个参数的函数");
  const { dialog, card } = createEditDialog("确认实体逐项处理方式");
  const note = document.createElement("p");
  note.textContent = "平台逐项传入实体名称，并保留实体 ID、类型和证据；请确认脚本函数及返回的标准名称字段。";
  const functionSelect = document.createElement("select");
  const resultSelect = document.createElement("select");
  const customField = document.createElement("input");
  customField.placeholder = "返回字段未列出时填写，例如 standard_name";
  for (const item of proposal.functions) {
    const option = document.createElement("option");
    option.value = JSON.stringify([item.function_name, item.parameter_name, item.result_fields]);
    option.textContent = `${item.function_name}(${item.parameter_name})`;
    functionSelect.append(option);
  }
  const updateFields = () => {
    const [, , fields] = JSON.parse(functionSelect.value);
    resultSelect.replaceChildren();
    for (const field of fields) {
      const option = document.createElement("option");
      option.value = field;
      option.textContent = field === "__value__" ? "函数直接返回名称字符串" : field;
      resultSelect.append(option);
    }
  };
  functionSelect.addEventListener("change", updateFields);
  updateFields();
  const confirm = document.createElement("button");
  confirm.textContent = "确认并试运行";
  card.append(note, functionSelect, resultSelect, customField, confirm);
  await new Promise((resolve, reject) => {
    let completed = false;
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (!completed) reject(new Error("尚未确认实体脚本适配"));
    }, { once: true });
    confirm.onclick = async () => {
      try {
        const [functionName, parameterName] = JSON.parse(functionSelect.value);
        const resultField = customField.value.trim() || resultSelect.value;
        if (!resultField) throw new Error("请填写脚本返回的标准名称字段");
        await request(`/api/v1/extensions/skills/${skillId}/entity-adapter`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            function_name: functionName,
            parameter_name: parameterName,
            result_field: resultField,
          }),
        });
        completed = true;
        dialog.close();
        resolve();
      } catch (error) { report(error.message, true); }
    };
    dialog.showModal();
  });
}

/** 非标准函数由用户确认每个参数来源；平台生成入口，不猜测函数语义。 */
async function ensureFunctionAdapter(skillId, detail, force = false) {
  if (detail.metadata?.generated_script_entrypoint && !force) return;
  const proposal = await request(`/api/v1/extensions/skills/${skillId}/function-adapter-proposal`, {
    method: "POST",
  });
  if (proposal.standard_entrypoint) return;
  const candidates = proposal.functions || [];
  if (!candidates.length) throw new Error("未发现可适配的顶层函数（支持 1–6 个普通参数）");
  const { dialog, card } = createEditDialog("确认脚本函数与输入");
  const note = document.createElement("p");
  note.textContent = "请选择要执行的函数，并为每个参数确认数据来源。脚本仍须返回符合当前 Skill 契约的标准产物。";
  const functionSelect = document.createElement("select");
  for (const item of candidates) {
    const option = document.createElement("option");
    option.value = item.function_name;
    option.textContent = `${item.function_name}(${item.parameters.join(", ")})`;
    functionSelect.append(option);
  }
  const currentAdapter = detail.metadata?.function_adapter;
  if (candidates.some((item) => item.function_name === currentAdapter?.function_name)) {
    functionSelect.value = currentAdapter.function_name;
  }
  const parameterBox = document.createElement("div");
  const fields = proposal.source_fields || [];
  const renderParameters = () => {
    parameterBox.replaceChildren();
    const selected = candidates.find((item) => item.function_name === functionSelect.value);
    const parameters = createMappingTable("函数参数映射", "脚本参数", "上游内容");
    parameterBox.append(parameters.section);
    for (const parameter of selected.parameters) {
      const source = document.createElement("select");
      source.dataset.parameter = parameter;
      const empty = document.createElement("option");
      empty.value = "";
      empty.textContent = "请选择脚本参数需要的内容";
      source.append(empty);
      const options = [
        ["TASK_PAYLOAD", "当前任务与全部上游资料"],
        ["INPUTS", "全部上游产物列表"],
        ...(proposal.input_artifact_types || []).map((artifactType) => [
          JSON.stringify(["CONTENT", artifactType]),
          `完整上游内容（${proposal.input_description || artifactType}）`,
        ]),
        ...fields.map((item) => [
          JSON.stringify([item.artifact_type, item.source_pointer]),
          `${item.business_label}（来自上游资料）`,
        ]),
      ];
      for (const [value, text] of options) {
        const option = document.createElement("option");
        option.value = value;
        option.textContent = text;
        source.append(option);
      }
      const current = currentAdapter?.function_name === selected.function_name
        ? currentAdapter.arguments?.find((item) => item.parameter_name === parameter) : null;
      const suggestion = current || proposal.suggested_bindings
        ?.find((item) => item.function_name === selected.function_name)
        ?.arguments.find((item) => item.parameter_name === parameter);
      if (suggestion?.source === "ARTIFACT_FIELD") {
        source.value = JSON.stringify([suggestion.artifact_type, suggestion.source_pointer]);
      } else if (suggestion?.source === "ARTIFACT_CONTENT") {
        source.value = JSON.stringify(["CONTENT", suggestion.artifact_type]);
      } else if (suggestion?.source) {
        source.value = suggestion.source;
      }
      appendMappingRow(parameters.body, parameter, source, {
        suggested: source.value,
        suggestionLabel: current ? "当前配置，待复测" : "系统建议，待确认",
      });
    }
  };
  const fieldBox = document.createElement("div");
  const renderFieldMappings = () => {
    fieldBox.replaceChildren();
    const selected = candidates.find((item) => item.function_name === functionSelect.value);
    const artifactType = proposal.input_artifact_types?.[0];
    if (!artifactType || !Object.keys(selected.field_reads || {}).length) return;
    const nested = createMappingTable("脚本内部字段映射", "脚本读取", "上游已有字段");
    fieldBox.append(nested.section);
    for (const [parameter, paths] of Object.entries(selected.field_reads || {})) {
      const targets = paths.filter((path) =>
        path.includes("*") || paths.some((other) => other.startsWith(path + "/*/"))
      );
      for (const target of targets) {
        const collection = !target.includes("*");
        const available = (collection
          ? proposal.collection_source_fields || []
          : proposal.nested_source_fields || []
        ).filter((item) =>
          item.artifact_type === artifactType
          && (collection || item.source_pointer.split("/").length === target.split("/").length)
        );
        if (available.some((item) => item.source_pointer === target)) continue;
        const select = document.createElement("select");
        select.dataset.parameter = parameter;
        select.dataset.target = target;
        select.dataset.artifactType = artifactType;
        select.dataset.required = (selected.required_reads?.[parameter] || []).includes(target) ? "true" : "false";
        select.append(new Option(select.dataset.required === "true"
          ? "必需字段：请选择来源"
          : "可选字段：不映射", ""));
        for (const item of available) select.append(new Option(item.source_pointer, item.source_pointer));
        const current = currentAdapter?.function_name === selected.function_name
          ? currentAdapter.field_bindings?.find((item) =>
            item.parameter_name === parameter && item.target_pointer === target
          ) : null;
        const suggested = current || (selected.field_suggestions || []).find((item) =>
          item.parameter_name === parameter && item.target_pointer === target
        );
        if (suggested) select.value = suggested.source_pointer;
        appendMappingRow(nested.body, `${parameter}${target}`, select, {
          required: select.dataset.required === "true",
          suggested: suggested?.source_pointer || "",
          suggestionLabel: current ? "当前配置，待复测" : "系统建议，待确认",
          note: !suggested ? "无可靠建议；上游缺失的数据不能通过映射补造。" : "",
        });
      }
    }
  };
  functionSelect.addEventListener("change", renderFieldMappings);
  renderFieldMappings();
  functionSelect.addEventListener("change", renderParameters);
  renderParameters();
  const confirm = document.createElement("button");
  confirm.textContent = "确认映射并试运行";
  card.append(note, functionSelect, parameterBox, fieldBox, confirm);
  await new Promise((resolve, reject) => {
    let completed = false;
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (!completed) reject(new Error("尚未确认脚本函数参数"));
    }, { once: true });
    confirm.onclick = async () => {
      try {
        const args = [...parameterBox.querySelectorAll("select")].map((select) => {
          if (!select.value) throw new Error(`请确认参数 ${select.dataset.parameter} 需要什么内容`);
          if (select.value === "TASK_PAYLOAD" || select.value === "INPUTS") {
            return { parameter_name: select.dataset.parameter, source: select.value };
          }
          const [artifactType, pointer] = JSON.parse(select.value);
          if (artifactType === "CONTENT") {
            return {
              parameter_name: select.dataset.parameter,
              source: "ARTIFACT_CONTENT",
              artifact_type: pointer,
            };
          }
          return {
            parameter_name: select.dataset.parameter,
            source: "ARTIFACT_FIELD",
            artifact_type: artifactType,
            source_pointer: pointer,
          };
        });
        const missingFields = [...fieldBox.querySelectorAll("select")]
          .filter((select) => select.dataset.required === "true" && !select.value)
          .map((select) => `${select.dataset.parameter}${select.dataset.target}`);
        if (missingFields.length) {
          throw new Error(`脚本需要的上游字段不存在或尚未确认：${missingFields.join("、")}`);
        }
        const fieldBindings = [...fieldBox.querySelectorAll("select")]
          .map((select) => ({
            parameter_name: select.dataset.parameter,
            artifact_type: select.dataset.artifactType,
            source_pointer: select.value,
            target_pointer: select.dataset.target,
          }));
        for (const binding of fieldBindings) {
          if (!args.some((arg) => arg.parameter_name === binding.parameter_name
            && arg.source === "ARTIFACT_CONTENT")) {
            throw new Error("内部字段映射需要选择该上游产物的完整内容");
          }
        }
        await request(`/api/v1/extensions/skills/${skillId}/function-adapter`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ function_name: functionSelect.value, arguments: args, field_bindings: fieldBindings }),
        });
        completed = true;
        dialog.close();
        resolve();
      } catch (error) { report(error.message, true); }
    };
    dialog.showModal();
  });
}

/** 将脚本试运行观测到的普通 JSON 字段映射为当前下游的标准内容字段。 */
async function ensureScriptOutputMapping(skillId, check, force = false) {
  const diagnostics = (check.diagnostics || []).join("；");
  if (!check.observed_output_fields?.length
      || (!force && !/标准 outputs|返回字段|只返回了部分/.test(diagnostics))) return false;
  const proposal = await request(
    `/api/v1/extensions/skills/${skillId}/output-mapping-proposals`,
    { method: "POST" },
  );
  if (!proposal.target_fields?.length) return false;
  const { dialog, card } = createEditDialog("确认脚本输出如何交给下游");
  const note = document.createElement("p");
  note.textContent = "脚本已运行。请选择返回字段与下游标准字段的对应关系；未选择的字段不会进入此产物，平台不会补造缺失数据。";
  const observed = document.createElement("p");
  observed.textContent = `脚本返回：${proposal.observed_fields.map((field) => `${field}（${check.observed_output_shapes?.[field] || "结构未知"}）`).join("；")}`;
  const issue = document.createElement("p");
  issue.textContent = `试运行提示：${diagnostics.replaceAll("未发现第三方依赖。；", "")}`;
  card.append(note, observed, issue);
  if (proposal.suggested_bindings.some((binding) => check.observed_output_shapes?.[binding.source_pointer.slice(1)] === "列表（0 项）")) {
    const warning = document.createElement("p");
    warning.textContent = "脚本返回了空列表。请先确认脚本拿到的是否为正确上游内容；仅做输出字段映射不能补回丢失的数据。";
    card.append(warning);
  }
  const mapping = createMappingTable("输出字段映射", "下游需要", "脚本返回");
  card.append(mapping.section);
  for (const target of proposal.target_fields) {
    const select = document.createElement("select");
    select.dataset.outputTarget = target.target_field;
    select.dataset.artifactType = target.artifact_type;
    const empty = document.createElement("option");
    empty.value = "";
    empty.textContent = "暂不映射";
    select.append(empty);
    const suggested = proposal.suggested_bindings.find((item) => item.target_field === target.target_field);
    for (const field of proposal.observed_fields) {
      const option = document.createElement("option");
      option.value = `/${field.replaceAll("~", "~0").replaceAll("/", "~1")}`;
      option.textContent = `脚本返回的 ${field}`;
      option.selected = suggested?.source_pointer === option.value;
      select.append(option);
    }
    appendMappingRow(mapping.body, target.business_label, select, {
      required: false,
      suggested: suggested?.source_pointer || "",
      note: !suggested ? "无可靠建议；未映射的字段不会交给下游。" : "",
    });
    const detail = document.createElement("div");
    detail.dataset.outputRecordGroup = target.target_field;
    const refreshRecordFields = () => {
      detail.replaceChildren();
      const recordFields = proposal.target_record_fields?.[target.target_field] || [];
      const sourceName = select.value.slice(1);
      const sourceFields = proposal.observed_record_fields?.[sourceName] || [];
      if (!sourceFields.length) return;
      const nested = createMappingTable(`${target.business_label}内部字段`, "下游字段", "脚本记录字段");
      detail.append(nested.section);
      for (const recordField of recordFields) {
        const recordSelect = document.createElement("select");
        recordSelect.dataset.outputRecordTarget = recordField;
        recordSelect.append(new Option("暂不映射", ""));
        for (const sourceField of sourceFields) {
          const option = new Option(`脚本记录的 ${sourceField}`, sourceField);
          option.selected = suggested?.source_pointer === select.value
            && suggested?.record_field_bindings?.[recordField] === sourceField;
          recordSelect.append(option);
        }
        appendMappingRow(nested.body, recordField, recordSelect, {
          required: false,
          suggested: suggested?.source_pointer === select.value
            ? suggested?.record_field_bindings?.[recordField] || "" : "",
        });
      }
    };
    select.addEventListener("change", refreshRecordFields);
    refreshRecordFields();
    card.append(detail);
  }
  const confirm = document.createElement("button");
  confirm.textContent = "确认并重新试运行";
  card.append(confirm);
  await new Promise((resolve, reject) => {
    let completed = false;
    dialog.addEventListener("close", () => {
      dialog.remove();
      if (!completed) reject(new Error("尚未确认脚本输出映射"));
    }, { once: true });
    confirm.onclick = async () => {
      try {
        const bindings = [...card.querySelectorAll("select[data-output-target]")]
          .filter((select) => select.value)
          .map((select) => ({
            artifact_type: select.dataset.artifactType,
            source_pointer: select.value,
            target_field: select.dataset.outputTarget,
            record_field_bindings: Object.fromEntries(
              [...(card.querySelector(`[data-output-record-group="${select.dataset.outputTarget}"]`)?.querySelectorAll("select[data-output-record-target]") || [])]
                .filter((recordSelect) => recordSelect.value)
                .map((recordSelect) => [recordSelect.dataset.outputRecordTarget, recordSelect.value]),
            ),
          }));
        if (!bindings.length) throw new Error("请至少确认一个返回字段的用途");
        await request(`/api/v1/extensions/skills/${skillId}/output-bindings`, {
          method: "PUT",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(bindings),
        });
        completed = true;
        dialog.close();
        resolve();
      } catch (error) { report(error.message, true); }
    };
    dialog.showModal();
  });
  return true;
}

/** 脚本草稿发布前必须在无网络 Runner 中通过依赖扫描和精确契约试运行。 */
async function checkScriptDraftBeforePublish(skillId) {
  const detail = await request(`/api/v1/extensions/skills/${skillId}`);
  if (!detail.resources.some(isPythonResource)) return null;
  if (detail.metadata?.contract_profile_id === "entity_transform") {
    const proposal = await request(`/api/v1/extensions/skills/${skillId}/function-adapter-proposal`, { method: "POST" });
    if (!proposal.standard_entrypoint
        && !proposal.functions.some((item) => item.function_name === "run")
        && proposal.functions.some((item) => item.parameters.length === 1)) {
      await ensureEntityAdapter(skillId, detail);
    } else {
      await ensureFunctionAdapter(skillId, detail);
    }
  } else {
    await ensureFunctionAdapter(skillId, detail);
    if (detail.metadata?.generated_script_entrypoint || (await request(`/api/v1/extensions/skills/${skillId}/function-adapter-proposal`, { method: "POST" })).standard_entrypoint) {
      await ensureScriptInputMapping(skillId, detail);
    }
  }
  report("正在检查脚本依赖、标准输入与输出格式，请稍候…");
  let check = await request(`/api/v1/extensions/skills/${skillId}/contract-checks`, {
    method: "POST",
  });
  if (check.status !== "PASSED" && await ensureScriptOutputMapping(skillId, check)) {
    check = await request(`/api/v1/extensions/skills/${skillId}/contract-checks`, {
      method: "POST",
    });
  }
  if (check.status !== "PASSED") {
    throw new Error(`脚本契约检查未通过：${(check.diagnostics || []).filter((item) => item !== "未发现第三方依赖。").join("；")}`);
  }
  return check;
}

/**
 * 在编辑窗口明确展示当前修订已保存的资源。
 *
 * 这里只展示服务端摘要，不传输或暴露脚本正文；文件名、大小和校验摘要足以让用户
 * 确认脚本确实已经上传并绑定到当前 Skill 修订。
 */
function appendExistingResources(card, detail) {
  const section = document.createElement("section");
  section.className = "existing-resource-panel";
  const heading = document.createElement("div");
  heading.className = "existing-resource-heading";
  const title = document.createElement("strong");
  const resources = Array.isArray(detail.resources) ? detail.resources : [];
  title.textContent = resources.length ? "当前已上传文件" : "无上传文件";
  const scriptResources = resources.filter(isPythonResource);
  heading.append(title);
  if (scriptResources.length) {
    const mode = document.createElement("span");
    mode.className = "resource-mode has-script";
    mode.textContent = "脚本执行";
    heading.append(mode);
  }
  section.append(heading);

  if (scriptResources.length) {
    const explanation = document.createElement("p");
    explanation.className = "edit-note resource-explanation";
    const entrypoint = String(detail.metadata?.generated_script_entrypoint || detail.metadata?.entrypoint || scriptResources[0].relative_path);
    explanation.textContent = detail.metadata?.generated_script_entrypoint
      ? `原脚本保持不变；平台适配入口：${entrypoint}。保存业务说明时会继续保留这些文件。`
      : `该 Skill 已绑定执行脚本，入口文件：${entrypoint}。保存业务说明时会继续保留这些文件。`;
    section.append(explanation);
  }

  // 只有存在资源时才渲染文件清单；空状态已经由标题“无上传文件”表达。
  if (resources.length) {
    const list = document.createElement("ul");
    list.className = "existing-resource-list";
    for (const resource of resources) {
      const item = document.createElement("li");
      const identity = document.createElement("div");
      const filename = document.createElement("strong");
      filename.textContent = resource.relative_path;
      const kind = document.createElement("span");
      kind.className = "resource-kind";
      kind.textContent = resource.relative_path === "_kg_skill_entry.py" ? "平台生成入口" : isPythonResource(resource) ? "Python 脚本" : "捆绑资源";
      identity.append(filename, kind);
      const facts = document.createElement("small");
      const checksum = String(resource.checksum || "").slice(0, 12);
      facts.textContent = `${formatResourceSize(resource.size_bytes)}${checksum ? ` · 校验 ${checksum}` : ""}`;
      item.append(identity, facts);
      list.append(item);
    }
    section.append(list);
  }
  card.append(section);
}

/** 用户选择替换文件后立即显示本地待上传清单，避免必须保存后才能确认。 */
function appendPendingResourcePreview(card, upload) {
  const preview = document.createElement("div");
  preview.className = "pending-resource-preview";
  preview.textContent = "尚未选择新文件；当前已上传文件会继续保留。";
  upload.addEventListener("change", () => {
    const files = [...upload.files];
    if (!files.length) {
      preview.textContent = "尚未选择新文件；当前已上传文件会继续保留。";
      return;
    }
    preview.replaceChildren();
    const label = document.createElement("strong");
    label.textContent = "本次待上传：";
    const names = document.createElement("span");
    names.textContent = files.map((file) => `${file.name}（${formatResourceSize(file.size)}）`).join("、");
    preview.append(label, names);
  });
  card.append(preview);
}

/** 将旧版没有保存业务表单原文的 Skill 以安全的默认值回显，用户可在发布前补充。 */
function skillBusinessValues(detail) {
  const stored = detail.metadata && detail.metadata.business_input;
  if (stored && typeof stored === "object") return stored;
  return {
    name: detail.name,
    solves_problem: detail.description,
    applicable_materials: "已解析的业务文档",
    expected_result: "结构化业务结果，并保留原文依据。",
    example_request: "请根据上传资料执行该能力。",
    additional_requirements: "",
  };
}

/** 从受控 YAML 中读取现有权限，仅用于勾选回显；保存时仍由后端重新校验。 */
function allowedSkillNames(permissionsYaml) {
  const match = String(permissionsYaml || "").match(/allowed_skills:\s*([\s\S]*?)(?:\n\S|$)/);
  return new Set(match ? [...match[1].matchAll(/^\s*-\s*(.+)$/gm)].map((item) => item[1].trim()) : []);
}

/** 编辑 Skill 时创建新草稿，旧发布版本不下线；可选上传同名文件会替换草稿中的原资源。 */
async function editSkill(item) {
  try {
    const [detail, contractOptions] = await Promise.all([
      request(`/api/v1/extensions/skills/${item.skill_id}`),
      request("/api/v1/extensions/business-skills/contract-options"),
    ]);
    const values = skillBusinessValues(detail);
    const { dialog, card } = createEditDialog(`编辑 Skill：${detail.name}`);
    const notice = document.createElement("p");
    notice.className = "edit-note";
    notice.textContent = "保存后生成新草稿；当前已发布版本会继续运行，直到你在管理页发布此草稿。已有脚本和资源默认保留。";
    card.append(notice);
    appendField(card, { name: "name", label: "能力名称", value: values.name });
    appendField(card, { name: "solves_problem", label: "它要解决什么问题", value: values.solves_problem, rows: 3 });
    appendField(card, { name: "applicable_materials", label: "适用什么资料", value: values.applicable_materials, rows: 2 });
    appendField(card, { name: "expected_result", label: "希望得到什么结果", value: values.expected_result, rows: 3 });
    appendField(card, { name: "example_request", label: "典型用户提问", value: values.example_request, rows: 2 });
    appendField(card, { name: "additional_requirements", label: "补充要求（可选）", value: values.additional_requirements, rows: 2, required: false });
    const contractLabel = document.createElement("label");
    const contractCaption = document.createElement("span");
    contractCaption.textContent = "能力使用方式";
    const contractSelect = document.createElement("select");
    contractSelect.name = "contract_profile_id";
    const selectedProfile = detail.metadata?.contract_profile_id || "document_table_analysis";
    for (const option of contractOptions) {
      const element = document.createElement("option");
      element.value = option.profile_id;
      element.textContent = `${option.business_name} · ${option.suggested_position}`;
      element.selected = option.profile_id === selectedProfile;
      contractSelect.append(element);
    }
    contractLabel.append(contractCaption, contractSelect);
    const contractHint = document.createElement("small");
    const refreshContractHint = () => {
      const selected = contractOptions.find((option) => option.profile_id === contractSelect.value);
      contractHint.textContent = selected
        ? `接收：${selected.input_description}；交付：${selected.output_description}。保存后仍会校验上下游兼容性。`
        : "修改后会生成新草稿；系统仍会校验输入输出和上下游兼容性。";
    };
    contractSelect.addEventListener("change", refreshContractHint);
    refreshContractHint();
    contractLabel.append(contractHint);
    card.append(contractLabel);
    appendExistingResources(card, detail);
    if (detail.draft_revision && (detail.resources || []).some(isPythonResource)) {
      const remapInput = document.createElement("button");
      remapInput.type = "button";
      remapInput.className = "secondary";
      remapInput.textContent = "修改输入字段映射";
      remapInput.onclick = async () => {
        try {
          if (detail.metadata?.function_adapter) {
            await ensureFunctionAdapter(detail.skill_id, detail, true);
          } else {
            await ensureScriptInputMapping(detail.skill_id, detail, true);
          }
          dialog.close();
          dialog.remove();
          report("输入映射已保存到草稿；发布前仍需重新试运行。");
          await refresh();
        } catch (error) { report(error.message, true); }
      };
      const remapOutput = document.createElement("button");
      remapOutput.type = "button";
      remapOutput.className = "secondary";
      remapOutput.textContent = "修改输出字段映射";
      remapOutput.onclick = async () => {
        try {
          remapOutput.disabled = true;
          remapOutput.textContent = "正在试运行脚本…";
          const check = await request(`/api/v1/extensions/skills/${detail.skill_id}/contract-checks`, {
            method: "POST",
          });
          if (!await ensureScriptOutputMapping(detail.skill_id, check, true)) {
            throw new Error("试运行尚未观察到脚本返回字段，请先确认输入映射或脚本返回值。");
          }
          dialog.close();
          dialog.remove();
          report("输出映射已保存到草稿；发布前仍需重新试运行。");
          await refresh();
        } catch (error) {
          report(error.message, true);
          remapOutput.disabled = false;
          remapOutput.textContent = "修改输出字段映射";
        }
      };
      card.append(remapInput, remapOutput);
    }
    const path = appendField(card, { name: "resource_path", label: "新资源的包内路径（单文件时可留空）", required: false, hint: "单个 .py 自动成为入口；多个脚本必须包含 main.py。" });
    const upload = document.createElement("input"); upload.type = "file"; upload.multiple = true; upload.name = "resource";
    const uploadLabel = document.createElement("label"); uploadLabel.textContent = "替换或新增脚本 / 捆绑资源"; uploadLabel.append(upload); card.append(uploadLabel);
    appendPendingResourcePreview(card, upload);
    const save = document.createElement("button"); save.type = "submit"; save.textContent = "保存为草稿"; card.append(save);
    card.addEventListener("submit", async (event) => {
      event.preventDefault(); save.disabled = true; save.textContent = "正在保存…";
      try {
        const form = new FormData(card);
        await request(`/api/v1/extensions/business-skills/${detail.skill_id}/draft`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify(Object.fromEntries(["name", "solves_problem", "applicable_materials", "expected_result", "example_request", "additional_requirements", "contract_profile_id"].map((key) => [key, form.get(key) || ""]))),
        });
        const files = form.getAll("resource").filter((value) => value instanceof File && value.size);
        for (const file of files) {
          const resourceForm = new FormData();
          resourceForm.append("relative_path", files.length === 1 ? (form.get("resource_path") || file.name) : file.name);
          resourceForm.append("resource", file);
          await request(`/api/v1/extensions/skills/${detail.skill_id}/resources`, { method: "POST", body: resourceForm });
        }
        dialog.close(); dialog.remove(); report("已保存为新草稿；确认无误后点击“发布”才会替换当前版本。"); await refresh();
      } catch (error) { report(`保存 Skill 草稿失败：${error.message}`, true); save.disabled = false; save.textContent = "保存为草稿"; }
    });
    dialog.showModal();
  } catch (error) { report(`读取 Skill 定义失败：${error.message}`, true); }
}

/** 编辑 Agent 时让用户重新描述职责并多选权限；契约由服务端以所选 Skill 自动重新推导。 */
async function editAgent(item) {
  try {
    const [detail, options] = await Promise.all([request(`/api/v1/extensions/agents/${item.agent_id}`), request("/api/v1/extensions/skill-options")]);
    const { dialog, card } = createEditDialog(`编辑 Agent：${detail.name}`);
    const note = document.createElement("p"); note.className = "edit-note";
    note.textContent = "保存后生成新草稿；当前已发布版本继续可用，直到你发布此草稿。"; card.append(note);
    appendField(card, { name: "name", label: "Agent 名称", value: detail.name });
    appendField(card, { name: "responsibility", label: "它负责什么工作", value: detail.description, rows: 3 });
    const example = detail.routing_examples[0] || {};
    appendField(card, { name: "applicable_scenarios", label: "适合在什么场景使用", value: example.scenario || "", rows: 2 });
    appendField(card, { name: "example_request", label: "典型用户提问", value: example.request || "", rows: 2 });
    const fieldset = document.createElement("fieldset"); const legend = document.createElement("legend"); legend.textContent = "允许调用的 Skill（可多选）"; fieldset.append(legend);
    const selected = allowedSkillNames(detail.permissions_yaml);
    options.forEach((option) => {
      const label = document.createElement("label"); label.className = "skill-choice";
      const checkbox = document.createElement("input"); checkbox.type = "checkbox"; checkbox.name = "allowed_skill_names"; checkbox.value = option.name; checkbox.checked = selected.has(option.name);
      const text = document.createElement("span"); text.textContent = `${option.display_name} · ${option.description}`;
      label.append(checkbox, text); fieldset.append(label);
    });
    card.append(fieldset);
    const save = document.createElement("button"); save.type = "submit"; save.textContent = "保存为草稿"; card.append(save);
    card.addEventListener("submit", async (event) => {
      event.preventDefault(); const form = new FormData(card); const skills = form.getAll("allowed_skill_names").map(String);
      if (!skills.length) return report("请至少选择一个允许调用的 Skill", true);
      save.disabled = true; save.textContent = "正在保存…";
      try {
        await request(`/api/v1/extensions/business-agents/${detail.agent_id}/draft`, {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: form.get("name"), responsibility: form.get("responsibility"), applicable_scenarios: form.get("applicable_scenarios"), example_request: form.get("example_request"), allowed_skill_names: skills }),
        });
        dialog.close(); dialog.remove(); report("已保存为新草稿；确认无误后点击“发布”才会替换当前版本。"); await refresh();
      } catch (error) { report(`保存 Agent 草稿失败：${error.message}`, true); save.disabled = false; save.textContent = "保存为草稿"; }
    });
    dialog.showModal();
  } catch (error) { report(`读取 Agent 定义失败：${error.message}`, true); }
}

/** 绘制一个定义摘要：草稿可发布，活动定义可停用，已停用定义才允许不可逆删除。 */
function renderDefinition(item, kind) {
  const root = document.createElement("div");
  root.className = "definition";
  const id = kind === "skills" ? item.skill_id : item.agent_id;
  // 用户定义的名称和说明必须作为纯文本插入，不能通过管理页面形成 HTML 注入；机器
  // 标识仅供 API/权限使用，不向业务用户展示。
  const summary = document.createElement("div");
  const name = document.createElement("strong"); name.textContent = item.name;
  const description = document.createElement("p"); description.textContent = item.description;
  summary.append(name, description);
  root.append(summary);
  const actions = document.createElement("div");
  actions.className = "actions";
  const status = document.createElement("span");
  const contractCheck = kind === "skills" ? item.latest_contract_check : null;
  const checkLabel = contractCheck
    ? ` · 最近脚本检查${contractCheck.status === "PASSED" ? "已通过" : "未通过"}`
    : "";
  status.textContent = `${item.draft_revision ? "草稿待发布" : item.status}${checkLabel}`;
  status.className = "status";
  actions.append(status);
  const edit = document.createElement("button");
  edit.className = "secondary";
  edit.textContent = "编辑";
  edit.onclick = () => (kind === "skills" ? editSkill(item) : editAgent(item));
  actions.append(edit);
  if (item.draft_revision) {
    const publish = document.createElement("button");
    publish.textContent = "发布";
    publish.onclick = async () => {
      try {
        if (kind === "skills") await checkScriptDraftBeforePublish(id);
        await request(`/api/v1/extensions/${kind}/${id}/publish`, { method: "POST" });
        report("已发布，下一轮任务会自动发现该定义。");
        await refresh();
      }
      catch (error) { report(error.message, true); }
    };
    actions.append(publish);
  }
  if (item.status !== "DISABLED") {
    const disable = document.createElement("button");
    disable.className = "secondary";
    disable.textContent = "停用";
    disable.onclick = async () => {
      try { await request(`/api/v1/extensions/${kind}/${id}/disable`, { method: "POST" }); report("已停用；历史任务不会受影响。"); await refresh(); }
      catch (error) { report(error.message, true); }
    };
    actions.append(disable);
  } else {
    if (!item.draft_revision && item.current_revision) {
      const enable = document.createElement("button");
      enable.textContent = "重新启用";
      enable.onclick = async () => {
        try { await request(`/api/v1/extensions/${kind}/${id}/enable`, { method: "POST" }); report("已恢复启用，后续新任务会再次发现该定义。"); await refresh(); }
        catch (error) { report(error.message, true); }
      };
      actions.append(enable);
    }
    const remove = document.createElement("button");
    remove.className = "danger";
    remove.textContent = "删除";
    remove.onclick = async () => {
      const label = kind === "skills" ? "Skill" : "Agent";
      const confirmed = window.confirm(
        `确定删除“${item.name}”吗？删除后不可恢复；历史任务日志和产物会保留。`
      );
      if (!confirmed) return;
      try {
        await request(`/api/v1/extensions/${kind}/${id}`, { method: "DELETE" });
        report(`已删除该自定义 ${label} 定义；历史任务不会受影响。`);
        await refresh();
      } catch (error) { report(error.message, true); }
    };
    actions.append(remove);
  }
  root.append(actions);
  return root;
}

/** 从服务端重载两个定义目录。 */
async function refresh() {
  const [skills, agents] = await Promise.all([request("/api/v1/extensions/skills"), request("/api/v1/extensions/agents")]);
  skillsList.replaceChildren(...(skills.length ? skills.map((item) => renderDefinition(item, "skills")) : [Object.assign(document.createElement("p"), { textContent: "暂无自定义 Skill。" })]));
  agentsList.replaceChildren(...(agents.length ? agents.map((item) => renderDefinition(item, "agents")) : [Object.assign(document.createElement("p"), { textContent: "暂无自定义 Agent。" })]));
}

applySelectedManagementTab();
refresh().then(() => report("可查看草稿和已发布能力；发布后的定义会在下一轮任务自动生效。")).catch((error) => report(error.message, true));
