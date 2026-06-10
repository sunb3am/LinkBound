// LinkBound — Frontend Application Logic v3.2
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  uploadId: null,
  operator: null,
  config: null,
  inputMode: "csv",
  templates: [],
  selectedTemplateId: null,
  selectedTemplateBody: "",
  aiOn: false,
  operatorDisplayNames: {}, // local display name overrides (persisted in localStorage)
};

const ACTION_HINTS = {
  auto: "Picks the safest action: connect if not connected, or DM if already connected.",
  connect_note: "Sends a connection request with your note (<= 300 chars).",
  connect: "Sends a connection request with no note.",
  message: "Direct message (1st-degree connections only).",
  inmail: "Sends an InMail (consumes an InMail credit).",
};
const ACTIONS_NEED_MSG = new Set(["connect_note", "message", "inmail"]);
const ACTIONS_NOTE_LIMIT = new Set(["auto", "connect_note"]);

// ─── Load persisted display names ─────────────────────────────────────────
function loadStoredDisplayNames() {
  try {
    const raw = localStorage.getItem("lb_display_names");
    if (raw) state.operatorDisplayNames = JSON.parse(raw);
  } catch (e) {}
}
function saveDisplayName(key, name) {
  state.operatorDisplayNames[key] = name;
  localStorage.setItem("lb_display_names", JSON.stringify(state.operatorDisplayNames));
}
function getDisplayName(op) {
  return state.operatorDisplayNames[op.key] || op.label;
}

loadStoredDisplayNames();

// ─── Utilities & Toasts ────────────────────────────────────────────────────
function esc(str) {
  return (str || "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
}

function badge(status) {
  const label = (status || "").replace(/_/g, " ");
  return `<span class="badge ${status}">${label}</span>`;
}

function showToast(message, type = "success") {
  const container = $("#toastContainer");
  const toast = document.createElement("div");
  toast.className = `toast ${type}`;
  toast.innerHTML = `
    <div style="font-size: 1.1rem;">${type === 'error' ? '⚠️' : '✅'}</div>
    <div>${esc(message)}</div>
  `;
  container.appendChild(toast);
  setTimeout(() => {
    toast.style.opacity = '0';
    setTimeout(() => toast.remove(), 300);
  }, 4500);
}

async function api(path, opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  
  // Inject custom Gemini settings if configured
  try {
    const key = localStorage.getItem("lb_custom_gemini_key");
    const model = localStorage.getItem("lb_custom_gemini_model");
    if (key) opts.headers["x-user-gemini-key"] = key;
    if (model) opts.headers["x-user-gemini-model"] = model;
  } catch (e) {}

  const res = await fetch(path, opts);
  if (!res.ok) {
    let msg = res.statusText;
    try { msg = (await res.json()).detail || msg; } catch (e) {}
    throw new Error(msg);
  }
  return res.json();
}

// ─── Tooltip system ────────────────────────────────────────────────────────
const tooltip = $("#tooltip");

function attachTooltips() {
  $$(".info-bubble[data-tooltip]").forEach(el => {
    if (el._tooltipAttached) return;
    el._tooltipAttached = true;
    el.addEventListener("mouseenter", (e) => {
      tooltip.textContent = e.currentTarget.dataset.tooltip;
      tooltip.style.opacity = "1";
      positionTooltip(e.currentTarget);
    });
    el.addEventListener("mousemove", (e) => positionTooltip(e.currentTarget));
    el.addEventListener("mouseleave", () => { tooltip.style.opacity = "0"; });
  });
}

function positionTooltip(el) {
  const rect = el.getBoundingClientRect();

  let left = rect.left + rect.width / 2 - 140;
  let top  = rect.top - 60;

  if (left < 8) left = 8;
  if (top < 8) top = rect.top + 28;

  tooltip.style.left = left + "px";
  tooltip.style.top  = top  + "px";
}

attachTooltips();

// ─── Navigation ────────────────────────────────────────────────────────────
$$(".nav-item").forEach(item => {
  item.addEventListener("click", () => gotoView(item.dataset.view));
});

function gotoView(viewName) {
  $$(".nav-item").forEach(i => i.classList.remove("active"));
  $$(".view-panel").forEach(p => p.classList.remove("active"));

  const navItem = document.querySelector(`.nav-item[data-view="${viewName}"]`);
  if (navItem) navItem.classList.add("active");
  const panel = $(`#view-${viewName}`);
  if (panel) panel.classList.add("active");

  const labels = {
    campaigns: "Campaigns",
    templates: "Templates",
    run: "Live Run",
    crm: "Audience Manager",
    analytics: "Analytics",
    batches: "Batch History",
    settings: "Settings",
  };
  $("#breadcrumb").textContent = labels[viewName] || "LinkBound";

  if (viewName === "templates") loadTemplates();
  if (viewName === "crm")       loadHistory();
  if (viewName === "batches")   loadBatches();
  if (viewName === "analytics") loadAnalytics();
  if (viewName === "settings")  loadOperators();

  // Re-attach tooltips for dynamically added elements
  attachTooltips();
}

// ─── Config Boot ───────────────────────────────────────────────────────────
async function loadConfig() {
  state.config = await api("/api/config");
  const sel = $("#operator");
  sel.innerHTML = "";

  state.config.operators.forEach((op) => {
    const o = document.createElement("option");
    o.value = op.key;
    o.textContent = op.label;
    sel.appendChild(o);
  });

  updateOperatorWidget();

  // AI pill
  const ai = state.config.ai || {};
  const localKey = localStorage.getItem("lb_custom_gemini_key");
  state.aiOn = !!(ai.enabled && (ai.configured || localKey));
  const pill = $("#aiPill");
  const pillLabel = $("#aiPillLabel");
  
  if (pill) {
    if (state.aiOn) {
      pill.classList.add("on");
      pill.title = `Gemini AI: Enabled (${localKey ? 'Custom Key' : ai.model})`;
      if (pillLabel) pillLabel.textContent = localKey ? "AI On (Custom)" : "AI On";
    } else {
      pill.classList.remove("on");
      pill.title = "Gemini AI: Not configured. Set GEMINI_API_KEY in .env or Settings";
      if (pillLabel) pillLabel.textContent = "AI Off";
    }
  }

  // Load local AI config to inputs
  if ($("#customGeminiKey")) {
    $("#customGeminiKey").value = localKey || "";
    const locModel = localStorage.getItem("lb_custom_gemini_model");
    if (locModel) $("#customGeminiModel").value = locModel;
  }
}

// ─── AI Settings Save ───────────────────────────────────────────────────────
if ($("#btnSaveAiConfig")) {
  $("#btnSaveAiConfig").addEventListener("click", () => {
    const key = $("#customGeminiKey").value.trim();
    const model = $("#customGeminiModel").value;
    if (key) {
      localStorage.setItem("lb_custom_gemini_key", key);
      localStorage.setItem("lb_custom_gemini_model", model);
      showToast("Custom AI Configuration saved.");
    } else {
      localStorage.removeItem("lb_custom_gemini_key");
      localStorage.removeItem("lb_custom_gemini_model");
      showToast("Reverted to system default AI Configuration.");
    }
    loadConfig(); // Refresh pill status
  });
}

function updateOperatorWidget() {
  if (!state.config || !state.config.operators.length) return;
  const sel = $("#operator");
  const key = sel.value;
  const op = state.config.operators.find(o => o.key === key) || state.config.operators[0];
  if (!op) return;

  const displayName = getDisplayName(op);
  const avatar = displayName.substring(0, 2).toUpperCase();

  const avatarEl = $("#opAvatar");
  const nameEl   = $("#opDisplayName");
  if (avatarEl) avatarEl.textContent = avatar;
  if (nameEl)   nameEl.textContent   = displayName;
}

// ─── Operator selector change ───────────────────────────────────────────────
$("#operator").addEventListener("change", () => {
  updateOperatorWidget();
  // Cancel any in-progress name edit
  $("#opNameEditRow").classList.add("hidden");
  $("#opEditBtn").style.display = "";
});

// ─── Operator display name edit ─────────────────────────────────────────────
$("#opEditBtn").addEventListener("click", () => {
  const key = $("#operator").value;
  const current = getDisplayName(state.config?.operators?.find(o => o.key === key) || { key, label: key });
  $("#opDisplayNameInput").value = current;
  $("#opNameEditRow").classList.remove("hidden");
  $("#opEditBtn").style.display = "none";
  $("#opDisplayNameInput").focus();
  $("#opDisplayNameInput").select();
});

$("#opNameSaveBtn").addEventListener("click", () => {
  const key = $("#operator").value;
  const name = $("#opDisplayNameInput").value.trim();
  if (!name) return;
  saveDisplayName(key, name);
  updateOperatorWidget();
  $("#opNameEditRow").classList.add("hidden");
  $("#opEditBtn").style.display = "";
  showToast("Display name updated.");
});

$("#opDisplayNameInput").addEventListener("keydown", (e) => {
  if (e.key === "Enter") $("#opNameSaveBtn").click();
  if (e.key === "Escape") {
    $("#opNameEditRow").classList.add("hidden");
    $("#opEditBtn").style.display = "";
  }
});

// ─── Campaign Wizard ───────────────────────────────────────────────────────
let currentStep = 1;

function goToStep(step) {
  currentStep = step;
  $$(".step-indicator").forEach(el => {
    const s = parseInt(el.dataset.step);
    el.classList.remove("active", "done");
    if (s === step) el.classList.add("active");
    if (s < step)  el.classList.add("done");
  });
  $$(".wizard-step-content").forEach(el => el.classList.remove("active"));
  $(`#step${step}`)?.classList.add("active");
}

$("#btnNext1").addEventListener("click", () => {
  if (state.inputMode === "csv" && !$("#csvFile").files.length) {
    showToast("Please select a CSV or Excel file first.", "error");
    return;
  }
  if (state.inputMode === "urls" && !$("#urlsText").value.trim()) {
    showToast("Please paste at least one LinkedIn URL.", "error");
    return;
  }
  goToStep(2);
});

$("#btnBack2").addEventListener("click", () => goToStep(1));

$("#btnNext2").addEventListener("click", async () => {
  const action   = $("#actionSelect").value;
  const bodyText = ($("#msgTemplate").value || "").trim();

  if (ACTIONS_NEED_MSG.has(action) && !bodyText) {
    showToast("This action requires a message.", "error");
    return;
  }

  const btn = $("#btnNext2");
  const oldText = btn.textContent;
  btn.textContent = "Parsing…";
  btn.disabled = true;

  try {
    let templateId = null, messageTemplate = "";
    if (state.selectedTemplateId && bodyText === (state.selectedTemplateBody || "").trim()) {
      templateId = state.selectedTemplateId;
    } else if (bodyText) {
      messageTemplate = bodyText;
    }

    let data;
    if (state.inputMode === "csv") {
      const fd = new FormData();
      fd.append("operator", $("#operator").value);
      fd.append("action", action);
      if (templateId) fd.append("template_id", String(templateId));
      if (messageTemplate) fd.append("message_template", messageTemplate);
      fd.append("file", $("#csvFile").files[0]);
      data = await api("/api/preview", { method: "POST", body: fd });
    } else {
      data = await api("/api/preview-urls", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          operator: $("#operator").value,
          action,
          urls_text: $("#urlsText").value.trim(),
          template_id: templateId,
          message_template: messageTemplate,
        }),
      });
    }

    state.uploadId = data.upload_id;
    state.operator = data.operator;
    renderPreview(data);
    goToStep(3);
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    btn.textContent = oldText;
    btn.disabled = false;
  }
});

$("#btnBack3").addEventListener("click", () => goToStep(2));

// ─── Input Mode Toggle ─────────────────────────────────────────────────────
$$(".mode-btn").forEach(btn => {
  btn.addEventListener("click", () => {
    $$(".mode-btn").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    state.inputMode = btn.dataset.mode;
    $("#csvSection").classList.toggle("hidden", state.inputMode !== "csv");
    $("#urlsSection").classList.toggle("hidden", state.inputMode !== "urls");
  });
});

$("#csvFile").addEventListener("change", (e) => {
  const f = e.target.files[0];
  if (f) {
    $("#csvFileName").textContent = `📎 ${f.name}`;
  }
});

// ─── Template Builder (Step 2) ─────────────────────────────────────────────
function updateCharCount(ta, displayEl, isNote) {
  const len = (ta.value || "").length;
  displayEl.textContent = `${len} chars`;
  if (isNote && len > 300) displayEl.style.color = "var(--danger)";
  else if (isNote && len > 250) displayEl.style.color = "var(--warning)";
  else displayEl.style.color = "var(--text-faint)";
}

$("#msgTemplate").addEventListener("input", () => {
  updateCharCount($("#msgTemplate"), $("#charCount"), ACTIONS_NOTE_LIMIT.has($("#actionSelect").value));
});

$("#actionSelect").addEventListener("change", (e) => {
  const a = e.target.value;
  $("#msgTemplate").placeholder = a === 'connect' ? "No note needed for connect only." : "Hi {first_name}, …";
  updateCharCount($("#msgTemplate"), $("#charCount"), ACTIONS_NOTE_LIMIT.has(a));
});

$$(".var-chip[data-var]").forEach(chip => {
  chip.addEventListener("click", () => {
    const ta = $("#msgTemplate");
    const s = ta.selectionStart, e = ta.selectionEnd;
    const text = chip.dataset.var;
    ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
    ta.selectionStart = ta.selectionEnd = s + text.length;
    ta.focus();
    updateCharCount($("#msgTemplate"), $("#charCount"), ACTIONS_NOTE_LIMIT.has($("#actionSelect").value));
  });
});

// Template Picker
async function loadTemplatesData() {
  const data = await api("/api/templates");
  state.templates = data.templates || [];
  const picker = $("#templatePicker");
  picker.innerHTML = '<option value="">Custom Message (Inline)</option>';
  state.templates.forEach(t => {
    const o = document.createElement("option");
    o.value = String(t.id);
    o.textContent = t.name;
    picker.appendChild(o);
  });
}

$("#templatePicker").addEventListener("change", (e) => {
  const id = e.target.value;
  if (!id) {
    state.selectedTemplateId = null;
    state.selectedTemplateBody = "";
    return;
  }
  const t = state.templates.find(x => String(x.id) === id);
  if (!t) return;
  state.selectedTemplateId = t.id;
  state.selectedTemplateBody = t.body;
  $("#msgTemplate").value = t.body;
  if (["connect_note", "message", "inmail"].includes(t.action)) {
    $("#actionSelect").value = t.action;
  }
  updateCharCount($("#msgTemplate"), $("#charCount"), ACTIONS_NOTE_LIMIT.has($("#actionSelect").value));
});

// AI Generation
async function callAIGenerate(improve = false) {
  if (!state.aiOn) {
    showToast("AI is not configured. Add GEMINI_API_KEY to your .env file.", "error");
    return;
  }
  const btn = improve ? $("#aiImprove") : $("#aiGenerate");
  const oldTxt = btn.textContent;
  btn.textContent = "Thinking…";
  btn.disabled = true;

  try {
    const limit = ACTIONS_NOTE_LIMIT.has($("#actionSelect").value) ? 300 : null;
    const data = await api("/api/ai/generate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        goal: $("#aiGoal").value.trim() || "a warm, high-reply-rate outbound opener",
        operator: $("#operator").value,
        max_chars: limit,
        existing: improve ? $("#msgTemplate").value : "",
        voice: $("#aiVoice").value,
      }),
    });
    $("#msgTemplate").value = data.text || "";
    $("#templatePicker").value = "";
    state.selectedTemplateId = null;
    updateCharCount($("#msgTemplate"), $("#charCount"), !!limit);
    showToast("AI generation complete.");
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    btn.textContent = oldTxt;
    btn.disabled = false;
  }
}
$("#aiGenerate").addEventListener("click", () => callAIGenerate(false));
$("#aiImprove").addEventListener("click", () => callAIGenerate(true));

// ─── Preview (Step 3) ──────────────────────────────────────────────────────
function renderPreview(data) {
  const stats = $("#previewStats");
  stats.innerHTML = `
    <div class="stat-card">
      <div class="stat-val">${data.total}</div>
      <div class="stat-lbl">Total Uploaded</div>
    </div>
    <div class="stat-card">
      <div class="stat-val" style="color: var(--success);">${data.sendable}</div>
      <div class="stat-lbl">Ready to Send</div>
    </div>
    <div class="stat-card">
      <div class="stat-val" style="color: var(--text-faint);">${data.already_contacted}</div>
      <div class="stat-lbl">Skipped (Already Contacted)</div>
    </div>
  `;

  const tbody = $("#previewTable tbody");
  tbody.innerHTML = "";
  data.rows.forEach(r => {
    let status = "queued";
    if (!r.template_ok || !r.linkedin_url) status = "needs_attention";
    else if (r.already_contacted) status = "skipped_dedup";

    tbody.insertAdjacentHTML("beforeend", `
      <tr>
        <td>${r.row_index + 1}</td>
        <td class="cell-name">${esc(r.first_name)} ${esc(r.last_name)}</td>
        <td>${esc(r.company)}</td>
        <td>${badge(r.action)}</td>
        <td class="msg-preview" title="${esc(r.rendered_message)}">${esc(r.rendered_message)}</td>
        <td>${badge(status)}</td>
      </tr>
    `);
  });
}

$("#btnLaunch").addEventListener("click", async () => {
  if (!state.uploadId) return;
  const btn = $("#btnLaunch");
  btn.disabled = true;
  btn.textContent = "Starting…";

  try {
    await api("/api/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upload_id: state.uploadId,
        operator: state.operator,
        action: $("#actionSelect").value,
        dry_run: $("#dryRun").checked,
        batch_name: "",
        send_on_mismatch: $("#sendOnMismatch").checked,
        ai_personalize: $("#aiPersonalize").checked,
        ai_voice: $("#aiVoice").value,
      }),
    });

    showToast("Campaign started successfully! 🚀");
    gotoView("run");
    goToStep(1);
    $("#csvFile").value = "";
    $("#csvFileName").textContent = "";
    $("#urlsText").value = "";
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.innerHTML = `Launch Campaign <i data-lucide="rocket" style="width:16px;"></i>`;
    lucide.createIcons();
  }
});

// ─── Templates View ────────────────────────────────────────────────────────
async function loadTemplates() {
  await loadTemplatesData();
  const list = $("#templateList");
  list.innerHTML = "";
  if (!state.templates.length) {
    list.innerHTML = `<div class="empty-state">
      <div class="empty-icon">📝</div>
      <div class="empty-title">No templates yet</div>
      <div class="empty-desc">Create your first template to reuse it across campaigns.</div>
    </div>`;
    return;
  }

  state.templates.forEach(t => {
    const el = document.createElement("div");
    el.className = "card";
    el.style.padding = "20px";
    el.innerHTML = `
      <div style="display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; gap: 12px;">
        <h3 style="font-size: 1rem; margin: 0;">${esc(t.name)}</h3>
        ${badge(t.action)}
      </div>
      <div style="font-family: var(--font-mono); font-size: 0.8rem; color: var(--text-faint); margin-bottom: 14px; white-space: pre-wrap; line-height: 1.5;">${esc(t.body.substring(0, 200))}${t.body.length > 200 ? '…' : ''}</div>
      <button class="btn small" data-edit="${t.id}">Edit</button>
    `;
    el.querySelector("button").addEventListener("click", () => {
      $("#tplEditId").value = t.id;
      $("#tplName").value = t.name;
      $("#tplBody").value = t.body;
      $("#tplAction").value = t.action;
      $("#tplEditorTitle").textContent = "Edit Template";
      $("#tplDelete").classList.remove("hidden");
    });
    list.appendChild(el);
  });
}

$("#tplNew").addEventListener("click", () => {
  $("#tplEditId").value = "";
  $("#tplName").value = "";
  $("#tplBody").value = "";
  $("#tplEditorTitle").textContent = "New Template";
  $("#tplDelete").classList.add("hidden");
});

$$(".var-chip[data-tpl-var]").forEach(chip => {
  chip.addEventListener("click", () => {
    const ta = $("#tplBody");
    const s = ta.selectionStart, e = ta.selectionEnd;
    const text = chip.dataset.tplVar;
    ta.value = ta.value.slice(0, s) + text + ta.value.slice(e);
    ta.selectionStart = ta.selectionEnd = s + text.length;
    ta.focus();
  });
});

$("#tplSave").addEventListener("click", async () => {
  const id = $("#tplEditId").value;
  const payload = {
    name: $("#tplName").value.trim(),
    body: $("#tplBody").value,
    action: $("#tplAction").value,
    tags: "",
  };
  if (!payload.name || !payload.body.trim()) {
    showToast("Name and body are required.", "error");
    return;
  }
  try {
    if (id) {
      await api(`/api/templates/${id}`, { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    } else {
      await api("/api/templates", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
    }
    showToast("Template saved.");
    $("#tplNew").click();
    loadTemplates();
  } catch (e) { showToast(e.message, "error"); }
});

$("#tplDelete").addEventListener("click", async () => {
  const id = $("#tplEditId").value;
  if (!id || !confirm("Delete this template? This cannot be undone.")) return;
  try {
    await api(`/api/templates/${id}`, { method: "DELETE" });
    showToast("Template deleted.");
    $("#tplNew").click();
    loadTemplates();
  } catch (e) { showToast(e.message, "error"); }
});

// ─── Global Controls ───────────────────────────────────────────────────────
$("#globalPauseBtn").addEventListener("click", async () => {
  const lbl = $("#runStateLabel").textContent.toLowerCase();
  if (lbl === "running")  { await api("/api/pause",  { method: "POST" }); showToast("Run paused"); }
  else if (lbl === "paused") { await api("/api/resume", { method: "POST" }); showToast("Run resumed"); }
});

$("#globalStopBtn").addEventListener("click", async () => {
  if (confirm("Hard stop the current run?")) {
    await api("/api/hard-stop", { method: "POST" });
    showToast("Run stopped");
  }
});

// ─── Live Run WebSocket & State ────────────────────────────────────────────
function connectWS() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (msg) => {
    const ev = JSON.parse(msg.data);
    switch (ev.type) {
      case "snapshot":
      case "state":
        applyState(ev.state);
        applyTotals(ev.totals);
        if (ev.message) $("#runMessage").textContent = ev.message;
        break;
      case "current":
        $("#runMessage").textContent = `Processing ${ev.full_name || ev.linkedin_url}…`;
        break;
      case "waiting":
        $("#runMessage").textContent = `Waiting ${ev.seconds}s before next…`;
        break;
      case "item":
        applyTotals(ev.totals);
        addFeedItem(ev);
        break;
    }
  };
  ws.onclose = () => setTimeout(connectWS, 2500);
}

function applyState(s) {
  const label = s.replace(/_/g, " ");
  const el = $("#runStateLabel");
  el.textContent = label.charAt(0).toUpperCase() + label.slice(1);
  el.style.color = (s === 'running') ? 'var(--success)' : (s === 'error' || s === 'stopped') ? 'var(--danger)' : 'var(--warning)';

  const running = s === "running", paused = s === "paused";
  const pBtn = $("#globalPauseBtn");
  pBtn.disabled = !(running || paused);
  pBtn.textContent = paused ? "Resume" : "Pause";
  $("#globalStopBtn").disabled = !(running || paused || s === "waiting_login");
}

function applyTotals(t) {
  if (!t) return;
  $("#numSent").textContent    = t.sent    ?? 0;
  $("#numSkipped").textContent = t.skipped ?? 0;
  $("#numFailed").textContent  = t.failed  ?? 0;
  $("#numTotal").textContent   = t.total   ?? 0;
}

function addFeedItem(ev) {
  const act = ev.action_executed
    ? `<span class="feed-act">${esc(ev.action_executed.replace(/_/g, " "))}</span>`
    : "";
  const item = document.createElement("div");
  item.className = "feed-item";
  item.innerHTML = `
    <div class="feed-head">
      <span class="feed-who">${esc(ev.full_name) || esc(ev.linkedin_url)}</span>
      <span class="feed-co">${esc(ev.company)}</span>
      ${act}
      ${badge(ev.status)}
    </div>
    <div style="font-family: var(--font-mono); font-size: 0.75rem; color: var(--text-faint); margin-top: 4px;">${esc(ev.detail || "")}</div>
  `;
  $("#feed").prepend(item);
}

// ─── CRM (Audience Manager) ────────────────────────────────────────────────
let crmGridApi = null;

async function loadHistory() {
  const search = $("#historySearch").value.trim();
  const data = await api("/api/contacts?limit=500&search=" + encodeURIComponent(search));

  if (!crmGridApi) {
    const gridOptions = {
      rowData: data.contacts,
      columnDefs: [
        { field: "full_name",     headerName: "Name",        filter: "agTextColumnFilter", flex: 2, cellClass: "cell-name" },
        { field: "company_csv",   headerName: "Company",     filter: "agTextColumnFilter", flex: 2 },
        { field: "last_status",   headerName: "Status",      filter: "agSetColumnFilter",  flex: 1, cellRenderer: p => badge(p.value) },
        { field: "linkedin_url",  headerName: "LinkedIn URL", flex: 2,
          cellRenderer: p => p.value ? `<a href="${esc(p.value)}" target="_blank" rel="noopener noreferrer" style="color: var(--accent-green); font-family: var(--font-mono); font-size: 0.78rem;">${esc(p.value.replace("https://www.linkedin.com/in/",""))}</a>` : "" },
        { field: "last_action_type", headerName: "Action",   flex: 1, cellRenderer: p => badge(p.value) },
        { field: "degree",        headerName: "Degree",      flex: 1 },
        { field: "operator",      headerName: "Session",     filter: "agSetColumnFilter", flex: 1 },
        { field: "last_action_at", headerName: "Last Contacted",
          valueFormatter: p => p.value ? new Date(p.value).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" }) : "",
          flex: 1.5 },
      ],
      defaultColDef: { sortable: true, resizable: true },
      pagination: true,
      paginationPageSize: 25,
    };
    crmGridApi = agGrid.createGrid($("#crmGrid"), gridOptions);
  } else {
    crmGridApi.setGridOption("rowData", data.contacts);
  }
}

$("#historyRefresh").addEventListener("click", loadHistory);
$("#historySearch").addEventListener("keydown", (e) => { if (e.key === "Enter") loadHistory(); });
$("#btnExportCsv")?.addEventListener("click", () => {
  if (crmGridApi) crmGridApi.exportDataAsCsv({ fileName: `linkbound-contacts-${new Date().toISOString().slice(0,10)}.csv` });
});

// ─── Batches ───────────────────────────────────────────────────────────────
let batchesGridApi = null;

async function loadBatches() {
  const data = await api("/api/batches?limit=50");

  if (!batchesGridApi) {
    const gridOptions = {
      rowData: data.batches || [],
      columnDefs: [
        { field: "public_id",  headerName: "ID",       width: 90 },
        { field: "name",       headerName: "Name",     flex: 2, cellClass: "cell-name" },
        { field: "action",     headerName: "Action",   flex: 1, cellRenderer: p => badge(p.value) },
        { field: "operator",   headerName: "Session",  flex: 1 },
        { field: "sent",       headerName: "Sent",     width: 80 },
        { field: "skipped",    headerName: "Skipped",  width: 90 },
        { field: "failed",     headerName: "Failed",   width: 80 },
        { field: "status",     headerName: "Status",   flex: 1, cellRenderer: p => badge(p.value) },
        { field: "started_at", headerName: "Started",
          valueFormatter: p => p.value ? new Date(p.value).toLocaleString() : "",
          flex: 1.5 },
      ],
      defaultColDef: { sortable: true, resizable: true },
      pagination: true,
      paginationPageSize: 15,
    };
    batchesGridApi = agGrid.createGrid($("#batchesGrid"), gridOptions);
  } else {
    batchesGridApi.setGridOption("rowData", data.batches || []);
  }
}

$("#batchesRefresh").addEventListener("click", loadBatches);

// ─── Analytics ─────────────────────────────────────────────────────────────
let chartInstance = null;
let templateChartInstance = null;

async function loadAnalytics() {
  const data = await api("/api/analytics/dashboard");
  $("#kpiTotal").textContent  = data.total_contacted;
  $("#kpiActive").textContent = data.active_campaigns;
  $("#kpiToday").textContent  = data.sent_today;

  const ctx = $("#analyticsChartCanvas").getContext('2d');
  if (chartInstance) chartInstance.destroy();
  chartInstance = new Chart(ctx, {
    type: 'line',
    data: {
      labels: data.sends_over_time.map(d => d.date),
      datasets: [{
        label: 'Sends',
        data: data.sends_over_time.map(d => d.count),
        borderColor: '#38613A',
        backgroundColor: 'rgba(56, 97, 58, 0.08)',
        fill: true,
        tension: 0.4,
        pointBackgroundColor: '#38613A',
        pointRadius: 4,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: { legend: { display: false } },
      scales: {
        y: { beginAtZero: true, grid: { color: 'rgba(0,0,0,0.04)' } },
        x: { grid: { display: false } },
      }
    }
  });

  const ctx2 = $("#analyticsTemplatesCanvas").getContext('2d');
  if (templateChartInstance) templateChartInstance.destroy();
  const hasData = data.template_performance && data.template_performance.length > 0;
  templateChartInstance = new Chart(ctx2, {
    type: 'doughnut',
    data: {
      labels: hasData ? data.template_performance.map(t => t.template_name || "Inline") : ["No data yet"],
      datasets: [{
        data: hasData ? data.template_performance.map(t => t.count) : [1],
        backgroundColor: hasData
          ? ['#38613A', '#D4A574', '#E07856', '#8B9B8E', '#2B2B2B']
          : ['rgba(0,0,0,0.05)'],
        borderWidth: 0,
      }]
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      plugins: {
        legend: { position: 'bottom', labels: { font: { family: 'JetBrains Mono' }, boxWidth: 12 } }
      }
    }
  });
}

$("#analyticsRefresh").addEventListener("click", loadAnalytics);

// ─── Voice Training ────────────────────────────────────────────────────────
$("#btnTrainVoice").addEventListener("click", async () => {
  const name = $("#voiceName").value.trim();
  const examples = $("#voiceExamples").value.trim();
  if (!name || !examples) {
    showToast("Please provide a name and writing examples.", "error");
    return;
  }
  const btn = $("#btnTrainVoice");
  btn.disabled = true;
  btn.textContent = "Analyzing…";

  try {
    const res = await api("/api/ai/train-voice", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name, examples })
    });
    $("#voiceSystemPrompt").value = res.system_prompt;
    $("#voiceResult").style.display = "block";
    showToast("Voice profile created!");
    const opt = document.createElement("option");
    opt.value = name;
    opt.textContent = name;
    $("#aiVoice").appendChild(opt);
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Train AI Voice";
  }
});

// ─── Sessions (Operators) ──────────────────────────────────────────────────
async function loadOperators() {
  const data = await api("/api/operators");
  const tbody = $("#opsTable tbody");
  tbody.innerHTML = "";

  if (!data.operators || data.operators.length === 0) {
    tbody.innerHTML = `<tr><td colspan="5" style="text-align: center; color: var(--text-faint); padding: 32px;">
      No sessions yet. Add one above to get started.
    </td></tr>`;
    return;
  }

  data.operators.forEach(op => {
    const displayName = state.operatorDisplayNames[op.key] || op.label;
    const createdDate = op.created_at
      ? new Date(op.created_at).toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" })
      : "—";
    tbody.insertAdjacentHTML("beforeend", `
      <tr>
        <td style="font-family: var(--font-mono); font-size: 0.8rem; color: var(--text-faint);">${esc(op.key)}</td>
        <td class="cell-name">${esc(displayName)}</td>
        <td style="font-family: var(--font-mono); font-size: 0.78rem; color: var(--text-faint);">profiles/${esc(op.key)}/</td>
        <td style="font-size: 0.85rem; color: var(--text-faint);">${createdDate}</td>
        <td>
          <button class="btn danger small" onclick="deleteOperator('${esc(op.key)}')">Delete</button>
        </td>
      </tr>
    `);
  });
}

$("#btnCreateOp").addEventListener("click", async () => {
  const name = $("#newOpName").value.trim();
  if (!name) return showToast("Full name is required.", "error");

  const btn = $("#btnCreateOp");
  btn.disabled = true;
  try {
    const result = await api("/api/operators", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name })
    });
    $("#newOpName").value = "";
    showToast(`Session "${result.label}" created! Go to Campaigns to run it.`);
    await loadConfig();
    await loadOperators();
    lucide.createIcons();
  } catch (e) {
    showToast(e.message, "error");
  } finally {
    btn.disabled = false;
  }
});

window.deleteOperator = async function(key) {
  if (!confirm(`Delete session "${key}"?\n\nThis removes it from the dropdown but preserves the browser profile and all associated data.`)) return;
  try {
    await api(`/api/operators/${key}`, { method: "DELETE" });
    showToast("Session removed.");
    await loadConfig();
    await loadOperators();
  } catch (e) {
    showToast(e.message, "error");
  }
};

// Settings save (no-op UI for now — actual settings live in config.yaml)
$("#saveSettingsBtn")?.addEventListener("click", () => {
  showToast("Note: safety limits are currently read from config.yaml. Editing config.yaml directly takes effect on next restart.", "error");
});

// ─── Help Modal ─────────────────────────────────────────────────────────────
const helpModal   = $("#helpModal");
const helpBtn     = $("#helpBtn");
const closeHelp   = $("#closeHelp");

helpBtn?.addEventListener("click", () => {
  helpModal.style.display = "flex";
  lucide.createIcons();
});
closeHelp?.addEventListener("click", () => { helpModal.style.display = "none"; });
helpModal?.addEventListener("click", (e) => { if (e.target === helpModal) helpModal.style.display = "none"; });
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && helpModal.style.display !== "none") helpModal.style.display = "none";
});

// ─── Onboarding ─────────────────────────────────────────────────────────────
(function initOnboarding() {
  const overlay  = $("#onboardingOverlay");
  const obNext   = $("#obNext");
  const obSkip   = $("#obSkip");
  const dots     = $$(".ob-dot-ind");
  let currentOb  = 1;
  const totalOb  = 3;

  // Only show if first visit
  const seen = localStorage.getItem("lb_onboarding_done");
  if (seen) {
    overlay.style.display = "none";
    return;
  }

  function setObStep(step) {
    currentOb = step;
    $$(".ob-step").forEach(s => s.classList.remove("active"));
    $(`#ob-${step}`)?.classList.add("active");
    dots.forEach(d => d.classList.toggle("active", parseInt(d.dataset.step) === step));
    if (obNext) obNext.textContent = step === totalOb ? "Get Started →" : "Next →";
  }

  dots.forEach(d => d.addEventListener("click", () => setObStep(parseInt(d.dataset.step))));

  obNext?.addEventListener("click", () => {
    if (currentOb < totalOb) {
      setObStep(currentOb + 1);
    } else {
      overlay.style.display = "none";
      localStorage.setItem("lb_onboarding_done", "1");
    }
  });

  obSkip?.addEventListener("click", () => {
    overlay.style.display = "none";
    localStorage.setItem("lb_onboarding_done", "1");
  });

  setObStep(1);
})();

// ─── Boot ──────────────────────────────────────────────────────────────────
loadConfig().catch(e => showToast("Failed to load config: " + e.message, "error"));
loadTemplatesData().catch(() => {});
connectWS();
