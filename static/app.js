// LinkBound — Frontend Application Logic v3.2
"use strict";

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  uploadId: null,
  operator: null,
  activeView: "campaigns",
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

function formatDateTime(value) {
  return value ? new Date(value).toLocaleString() : "";
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

function gotoView(viewName, { persist = true } = {}) {
  const panel = $(`#view-${viewName}`);
  if (!panel) viewName = "campaigns";

  $$(".nav-item").forEach(i => i.classList.remove("active"));
  $$(".view-panel").forEach(p => p.classList.remove("active"));

  const navItem = document.querySelector(`.nav-item[data-view="${viewName}"]`);
  if (navItem) navItem.classList.add("active");
  const activePanel = $(`#view-${viewName}`);
  if (activePanel) activePanel.classList.add("active");
  state.activeView = viewName;
  if (persist) {
    localStorage.setItem("lb_active_view", viewName);
  }

  const labels = {
    campaigns: "Campaigns",
    scheduled: "Scheduled",
    templates: "Templates",
    run: "Live Run",
    crm: "Audience Manager",
    inbox: "Inbox & Sync",
    analytics: "Analytics",
    batches: "Batch History",
    settings: "Settings",
  };
  $("#breadcrumb").textContent = labels[viewName] || "LinkBound";

  if (viewName === "templates") loadTemplates();
  if (viewName === "crm")       loadHistory();
  if (viewName === "inbox")     loadInbox();
  if (viewName === "batches")   loadBatches();
  if (viewName === "scheduled") loadScheduledCampaigns();
  if (viewName === "analytics") loadAnalytics();
  if (viewName === "settings")  loadOperators();
  if (viewName === "run")       refreshRunStatus();

  // Re-attach tooltips for dynamically added elements
  attachTooltips();
}

function restoreActiveView() {
  const saved = localStorage.getItem("lb_active_view") || "campaigns";
  gotoView(saved, { persist: false });
}

// ─── Config Boot ───────────────────────────────────────────────────────────
async function loadConfig() {
  state.config = await api("/api/config");
  const dryRun = $("#dryRun");
  const sendsDisabled = state.config.live_sends_enabled === false;
  dryRun.checked = sendsDisabled || dryRun.checked;
  dryRun.disabled = sendsDisabled;
  $("#sendModeNotice").hidden = !sendsDisabled;
  $("#queueSendNotice").hidden = !sendsDisabled;
  $("#scheduledLiveNotice").hidden = !sendsDisabled;
  const weeklyCap = Number(state.config.safety?.queue_weekly_cap);
  $("#queueWeeklyCapNotice").textContent = Number.isFinite(weeklyCap) && weeklyCap > 0
    ? `This host allows up to ${weeklyCap} confirmed sends in any rolling 7 days. Larger chunks can defer later sends.`
    : "";
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
  if (state.activeView === "crm") loadHistory();
  if (state.activeView === "inbox") loadInbox();
  if (state.activeView === "run") refreshRunStatus();
  if (state.activeView === "batches") loadBatches();
  if (state.activeView === "scheduled") loadScheduledCampaigns();
  if (state.activeView === "analytics") loadAnalytics();
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

// ─── Scheduled campaigns ──────────────────────────────────────────────────
function setDefaultQueueSchedule() {
  try { $("#queueTimezone").value = Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC"; }
  catch (_) { $("#queueTimezone").value = "UTC"; }
  const start = new Date();
  start.setMinutes(Math.ceil((start.getMinutes() + 1) / 15) * 15, 0, 0);
  const localValue = new Date(start.getTime() - start.getTimezoneOffset() * 60000)
    .toISOString().slice(0, 16);
  $("#queueStartAt").value = localValue;
}
setDefaultQueueSchedule();

$("#btnQueueCampaign").addEventListener("click", async () => {
  const message = $("#queueFormMessage");
  const btn = $("#btnQueueCampaign");
  const name = $("#queueName").value.trim();
  const startAt = $("#queueStartAt").value;
  const timezone = $("#queueTimezone").value.trim();
  const dailyChunk = Number($("#queueDailyChunk").value);
  message.textContent = "";
  message.classList.remove("error");

  if (!state.uploadId || !state.operator) {
    message.textContent = "Preview a contact list before scheduling it.";
    message.classList.add("error");
    return;
  }
  if (!name || !startAt || !timezone || !Number.isInteger(dailyChunk) || dailyChunk < 1 || dailyChunk > 100) {
    message.textContent = "Enter a name, start time, time zone, and a daily amount from 1 to 100.";
    message.classList.add("error");
    return;
  }
  btn.disabled = true;
  btn.textContent = "Queueing…";
  try {
    const result = await api("/api/queue", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        upload_id: state.uploadId,
        operator: state.operator,
        name,
        start_at_local: startAt,
        timezone,
        daily_chunk: dailyChunk,
        send_on_mismatch: $("#sendOnMismatch").checked,
        ai_personalize: $("#aiPersonalize").checked,
        ai_voice: $("#aiVoice").value,
      }),
    });
    state.uploadId = null;
    message.textContent = `${result.queued} queued, ${result.excluded} excluded. First due ${formatQueueDateTime(result.first_due_at_utc, timezone)}.`;
    showToast(result.live_sends_enabled === false
      ? "Campaign queued. Live sends are disabled on this host."
      : "Campaign queued.");
    await loadScheduledCampaigns();
    gotoView("scheduled");
    $("#queueName").value = "";
  } catch (error) {
    message.textContent = error.message;
    message.classList.add("error");
  } finally {
    btn.disabled = false;
    btn.textContent = "Queue campaign";
  }
});

let scheduledLoadSeq = 0;
async function loadScheduledCampaigns() {
  loadOutreachReviews();
  const seq = ++scheduledLoadSeq;
  const operator = $("#operator").value;
  const root = $("#scheduledCampaigns");
  if (!operator) {
    root.innerHTML = `<div class="queue-empty"><strong>No session selected</strong>Select a LinkedIn session to view its scheduled campaigns.</div>`;
    return;
  }
  root.innerHTML = `<div class="queue-empty">Loading scheduled campaigns…</div>`;
  try {
    const data = await api(`/api/queue?operator=${encodeURIComponent(operator)}`);
    if (seq !== scheduledLoadSeq || operator !== $("#operator").value) return;
    const campaigns = data.campaigns || [];
    if (!campaigns.length) {
      root.innerHTML = `<div class="queue-empty"><strong>No scheduled campaigns</strong>Preview a contact list, then schedule it from the campaign review step.</div>`;
      return;
    }
    root.innerHTML = `<div class="queue-ledger">${campaigns.map(renderScheduledCampaign).join("")}</div>`;
  } catch (error) {
    if (seq !== scheduledLoadSeq) return;
    root.innerHTML = `<div class="queue-empty"><strong>Could not load scheduled campaigns</strong>${esc(error.message)}</div>`;
  }
}

let outreachReviewSeq = 0;
async function loadOutreachReviews() {
  const seq = ++outreachReviewSeq;
  const operator = $("#operator").value;
  const root = $("#outreachReviewItems");
  if (!operator) {
    root.innerHTML = `<div class="queue-empty">Select a session to review uncertain outreach.</div>`;
    return;
  }
  root.innerHTML = `<div class="queue-target-state">Loading review items…</div>`;
  try {
    const data = await api(`/api/outreach/review?operator=${encodeURIComponent(operator)}`);
    if (seq !== outreachReviewSeq || operator !== $("#operator").value) return;
    const items = data.items || [];
    if (!items.length) {
      root.innerHTML = `<div class="queue-target-state">No uncertain outreach for this session.</div>`;
      return;
    }
    root.innerHTML = items.map(item => `<div class="outreach-review-row" data-review-id="${Number(item.id)}">
      <div class="outreach-review-person"><strong>${esc(item.linkedin_url || "Unknown profile")}</strong>
        <span>${esc(item.campaign_name || "Immediate run")} · ${esc(item.detail || "Browser action interrupted")}</span></div>
      <label class="outreach-review-note">What did you verify on LinkedIn?
        <input type="text" maxlength="500" placeholder="e.g. Invitation visible in Sent" aria-label="Review note for ${esc(item.linkedin_url || "profile")}"></label>
      <div class="outreach-review-actions">
        <button type="button" class="btn small" data-verdict="sent">Confirmed sent</button>
        <button type="button" class="btn small" data-verdict="not_sent">Confirmed not sent</button>
      </div>
      <div class="queue-target-state error" role="alert" hidden></div>
    </div>`).join("");
  } catch (error) {
    if (seq !== outreachReviewSeq) return;
    root.innerHTML = `<div class="queue-target-state error">Could not load review items: ${esc(error.message)}</div>`;
  }
}

$("#outreachReviewItems").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-verdict]");
  if (!button) return;
  const row = button.closest("[data-review-id]");
  const note = row.querySelector("input").value.trim();
  const error = row.querySelector("[role='alert']");
  if (!note) {
    error.textContent = "Add a note describing what you checked in LinkedIn.";
    error.hidden = false;
    return;
  }
  const verdict = button.dataset.verdict;
  if (!window.confirm(`Mark this LinkedIn action as ${verdict === "sent" ? "sent" : "not sent"}?`)) return;
  row.querySelectorAll("button").forEach(control => { control.disabled = true; });
  try {
    await api(`/api/outreach/review/${Number(row.dataset.reviewId)}`, {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({operator: $("#operator").value, verdict, note}),
    });
    showToast("Review recorded. Paused campaigns stay paused until resumed.");
    await loadScheduledCampaigns();
  } catch (failure) {
    error.textContent = failure.message;
    error.hidden = false;
    row.querySelectorAll("button").forEach(control => { control.disabled = false; });
  }
});

function renderScheduledCampaign(campaign) {
  const status = String(campaign.status || "unknown").replace(/_/g, " ");
  const canPause = campaign.status === "queued";
  const canResume = campaign.status === "paused";
  const canCancel = ["queued", "paused"].includes(campaign.status);
  const due = campaign.next_due_at_utc
    ? formatQueueDateTime(campaign.next_due_at_utc, campaign.timezone)
    : "None scheduled";
  return `<article class="queue-row" data-campaign-id="${Number(campaign.id)}">
    <div>
      <div class="queue-campaign-name">${esc(campaign.name || "Untitled campaign")}</div>
      <div class="queue-source-name">Source: ${esc(campaign.source_name || "Not available")}</div>
    </div>
    <div>
      <div class="queue-status-line"><span class="queue-status-text">${esc(status)}</span><span class="hint">${esc(campaign.timezone || "")}</span></div>
      ${campaign.pause_reason ? `<div class="queue-pause-reason">${esc(campaign.pause_reason)}</div>` : ""}
      <div class="queue-counts" aria-label="Campaign contact counts">
        <span>${Number(campaign.queued || 0)} queued</span><span>${Number(campaign.sending || 0)} sending</span>
        <span>${Number(campaign.sent || 0)} sent</span><span>${Number(campaign.skipped || 0)} skipped</span>
        <span>${Number(campaign.failed || 0)} failed</span><span>${Number(campaign.uncertain || 0)} uncertain</span>
        <span>${Number(campaign.cancelled || 0)} cancelled</span><span>${Number(campaign.total || 0)} total</span>
      </div>
    </div>
    <div class="queue-meta"><div><strong>Daily chunk:</strong> ${Number(campaign.daily_chunk || 0)}</div><div><strong>Next due:</strong> ${esc(due)}</div></div>
    <div class="queue-controls">
      <button type="button" class="btn small" data-queue-action="targets" aria-expanded="false" aria-controls="queue-targets-${Number(campaign.id)}">View targets</button>
      ${canPause ? `<button type="button" class="btn small" data-queue-action="pause" aria-label="Pause ${esc(campaign.name || "campaign")}">Pause</button>` : ""}
      ${canResume ? `<button type="button" class="btn small" data-queue-action="resume" aria-label="Resume ${esc(campaign.name || "campaign")}">Resume</button>` : ""}
      ${canCancel ? `<button type="button" class="btn small danger" data-queue-action="cancel" aria-label="Cancel ${esc(campaign.name || "campaign")}">Cancel</button>` : ""}
      <button type="button" class="btn small" data-queue-action="source" aria-label="Download source for ${esc(campaign.name || "campaign")}" data-source-name="${esc(campaign.source_name || "source.csv")}">Download source</button>
    </div>
    <div class="queue-targets hidden" id="queue-targets-${Number(campaign.id)}" data-targets-for="${Number(campaign.id)}" aria-live="polite"></div>
  </article>`;
}

function formatQueueDateTime(value, timezone) {
  try {
    return new Intl.DateTimeFormat(undefined, {
      year: "numeric", month: "short", day: "numeric", hour: "numeric",
      minute: "2-digit", timeZone: timezone, timeZoneName: "short",
    }).format(new Date(value));
  } catch (_) {
    return `${formatDateTime(value)} UTC`;
  }
}

$("#scheduledRefresh").addEventListener("click", loadScheduledCampaigns);
$("#scheduledCampaigns").addEventListener("click", async (event) => {
  const button = event.target.closest("button[data-queue-action]");
  if (!button) return;
  const row = button.closest("[data-campaign-id]");
  const campaignId = Number(row?.dataset.campaignId);
  const operation = button.dataset.queueAction;
  const operator = $("#operator").value;
  if (!campaignId || !operator) return;
  if (operation === "targets") {
    const targetPanel = row.querySelector("[data-targets-for]");
    const expanded = button.getAttribute("aria-expanded") === "true";
    if (expanded) {
      button.setAttribute("aria-expanded", "false");
      button.textContent = "View targets";
      targetPanel.classList.add("hidden");
      return;
    }
    button.setAttribute("aria-expanded", "true");
    button.textContent = "Hide targets";
    targetPanel.classList.remove("hidden");
    if (targetPanel.dataset.loaded === "true") return;
    targetPanel.innerHTML = `<div class="queue-target-state">Loading targets…</div>`;
    button.disabled = true;
    try {
      const data = await api(`/api/queue/${campaignId}?operator=${encodeURIComponent(operator)}`);
      if (operator !== $("#operator").value) return;
      renderQueueTargets(targetPanel, data.campaign || {}, data.targets || []);
      targetPanel.dataset.loaded = "true";
    } catch (error) {
      targetPanel.innerHTML = `<div class="queue-target-state error">Could not load targets: ${esc(error.message)}</div>`;
      button.setAttribute("aria-expanded", "true");
      button.textContent = "Hide targets";
    } finally {
      button.disabled = false;
    }
    return;
  }
  if (operation === "cancel" && !window.confirm("Cancel this scheduled campaign? Contacts not yet sent will stay unsent.")) return;
  button.disabled = true;
  try {
    if (operation === "source") {
      const response = await fetch(`/api/queue/${campaignId}/source?operator=${encodeURIComponent(operator)}`);
      if (!response.ok) {
        let detail = response.statusText;
        try { detail = (await response.json()).detail || detail; } catch (_) {}
        throw new Error(detail);
      }
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = button.dataset.sourceName || "source.csv";
      document.body.appendChild(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
    } else {
      await api(`/api/queue/${campaignId}/${operation}?operator=${encodeURIComponent(operator)}`, { method: "POST" });
      showToast(`Campaign ${operation === "cancel" ? "cancelled" : operation === "pause" ? "paused" : "resumed"}.`);
    }
    await loadScheduledCampaigns();
  } catch (error) {
    showToast(error.message, "error");
    button.disabled = false;
  }
});

function renderQueueTargets(panel, campaign, targets) {
  if (!targets.length) {
    panel.innerHTML = `<div class="queue-target-state">No targets are available for this campaign.</div>`;
    return;
  }
  const visible = targets.slice(0, 100);
  const rows = visible.map((target) => {
    const job = target.job || {};
    const name = job.full_name || [job.first_name, job.last_name].filter(Boolean).join(" ") || job.first_name || "Name unavailable";
    const linkedinUrl = String(target.linkedin_url || job.linkedin_url || "");
    const href = safeLinkedInHref(linkedinUrl);
    const person = href
      ? `<a href="${esc(href)}" target="_blank" rel="noopener noreferrer">${esc(name)}</a>`
      : `<span>${esc(name)}</span>`;
    const urlText = linkedinUrl ? esc(linkedinUrl) : "LinkedIn URL unavailable";
    const due = target.available_at_utc
      ? formatQueueDateTime(target.available_at_utc, campaign.timezone)
      : "Not scheduled";
    return `<div class="queue-target-row">
      <div class="queue-target-person">${person}<span class="queue-target-url">${urlText}</span></div>
      <div class="queue-target-state-text">${esc(String(target.state || "unknown").replace(/_/g, " "))}</div>
      <div class="queue-target-due">${esc(due)}</div>
      <div class="queue-target-detail">${esc(target.detail || "") || "No additional detail"}</div>
    </div>`;
  }).join("");
  const remainder = targets.length > visible.length
    ? `<div class="queue-target-limit">Showing the first ${visible.length} of ${targets.length} targets.</div>`
    : "";
  panel.innerHTML = `<div class="queue-target-summary">${targets.length} target${targets.length === 1 ? "" : "s"}</div><div class="queue-target-list">${rows}</div>${remainder}`;
}

function safeLinkedInHref(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" && (url.hostname === "linkedin.com" || url.hostname.endsWith(".linkedin.com"))
      ? url.href
      : "";
  } catch (_) {
    return "";
  }
}

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
  ws.onopen = () => refreshRunStatus();
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
  if (!s) return;
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

async function refreshRunStatus() {
  try {
    const snapshot = await api("/api/status");
    applyState(snapshot.state || "idle");
    applyTotals(snapshot.totals || {});
    if (snapshot.message) {
      $("#runMessage").textContent = snapshot.message;
    } else if (snapshot.current?.linkedin_url) {
      $("#runMessage").textContent = `Processing ${snapshot.current.full_name || snapshot.current.linkedin_url}...`;
    } else {
      $("#runMessage").textContent = snapshot.state ? snapshot.state.replace(/_/g, " ") : "Idle";
    }
  } catch (e) {
    // WebSocket events will retry independently; keep the UI responsive.
  }
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
let crmLoadSeq = 0;

async function loadHistory() {
  const seq = ++crmLoadSeq;
  const operator = $("#operator").value;
  if (!operator) return;
  const search = $("#historySearch").value.trim();
  let data;
  try {
    data = await api("/api/contacts?operator=" + encodeURIComponent(operator)
      + "&limit=500&search=" + encodeURIComponent(search));
  } catch (error) {
    if (seq === crmLoadSeq) showToast("Could not load contacts: " + error.message, "error");
    return;
  }
  if (seq !== crmLoadSeq) return;

  if (!crmGridApi) {
    const gridOptions = {
      rowData: data.contacts,
      columnDefs: [
        { field: "full_name",     headerName: "Name",        filter: "agTextColumnFilter", flex: 2, cellClass: "cell-name" },
        { field: "company_csv",   headerName: "Company",     filter: "agTextColumnFilter", flex: 2 },
        { field: "last_observed_status", headerName: "Status", filter: "agSetColumnFilter", flex: 1, cellRenderer: p => badge(p.value || (p.data?.has_successful_send ? "sent" : "")) },
        { field: "invited_at", headerName: "Invited", flex: 1,
          valueFormatter: p => p.value ? formatDateTime(p.value) : "" },
        { field: "accepted_at", headerName: "Accepted", flex: 1,
          valueFormatter: p => p.value ? formatDateTime(p.value) : "" },
        { field: "replied_at", headerName: "Replied", flex: 1,
          valueFormatter: p => p.value ? formatDateTime(p.value) : "" },
        { field: "file_received_at", headerName: "File received", flex: 1,
          valueFormatter: p => p.value ? formatDateTime(p.value) : "" },
        { field: "linkedin_url",  headerName: "LinkedIn URL", flex: 2,
          cellRenderer: p => p.value ? `<a href="${esc(p.value)}" target="_blank" rel="noopener noreferrer" style="color: var(--accent-green); font-family: var(--font-mono); font-size: 0.78rem;">${esc(p.value.replace("https://www.linkedin.com/in/",""))}</a>` : "" },
        { field: "last_action_type", headerName: "Action",   flex: 1, cellRenderer: p => badge(p.value) },
        { field: "degree",        headerName: "Degree",      flex: 1 },
        { field: "last_observed_at", headerName: "Last Observed",
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

// ─── Inbox & Sync ─────────────────────────────────────────────────────────
let inboxLoadSeq = 0;
let inboxDetailSeq = 0;
let inboxSelectedConversationId = null;
let inboxConversations = [];
let inboxTotal = 0;

function renderInboxList() {
  $("#inboxConversationCount").textContent = `${inboxConversations.length} of ${inboxTotal}`;
  $("#inboxConversationList").innerHTML = inboxConversations.map(renderInboxConversation).join("");
  $("#inboxLoadMore").hidden = inboxConversations.length >= inboxTotal;
}

function inboxQuery(operator) {
  return `operator=${encodeURIComponent(operator)}`;
}

function inboxSafeLinkedInUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value);
    return url.protocol === "https:" && /(^|\.)linkedin\.com$/i.test(url.hostname) ? url.href : "";
  } catch (e) {
    return "";
  }
}

function inboxCoverageHtml(coverage) {
  if (!coverage || typeof coverage !== "object" || !Object.keys(coverage).length) {
    return `<span class="inbox-coverage-empty">No section coverage recorded.</span>`;
  }
  return Object.entries(coverage).map(([section, item]) => {
    item = item || {};
    const counts = [`${Number(item.observed) || 0} observed`, `${Number(item.stored) || 0} saved`];
    if (Number(item.unresolved) > 0) counts.push(`${Number(item.unresolved)} unresolved`);
    const error = item.error ? `<span class="inbox-coverage-error">${esc(item.error)}</span>` : "";
    return `<div class="inbox-coverage-item">
      <span class="inbox-coverage-name">${esc(section.replace(/_/g, " "))}</span>
      <span>${counts.map(esc).join(" · ")}</span>
      <span class="inbox-status-text">${esc(item.status || "unknown")}</span>${error}
    </div>`;
  }).join("");
}

function renderInboxSync(runs) {
  const summary = $("#inboxSyncHealth").querySelector(".inbox-sync-summary");
  const coverage = $("#inboxSyncCoverage");
  const schedule = state.config?.inbound_schedule;
  const operator = $("#operator").value;
  $("#inboxSyncNote").textContent = schedule?.enabled && schedule.operator === operator
    ? `A no-send browser scan runs daily at ${schedule.time_local} (${schedule.timezone}). It inspects changed visible conversations in bounded batches. Coverage remains partial until the backfill is complete.`
    : "Daily browser scanning is off for this session. Saved observations remain available here.";
  if (!runs.length) {
    summary.innerHTML = `<strong>No inbox sync recorded yet.</strong><span>Run history will appear here after the first scan.</span>`;
    coverage.innerHTML = "";
    return;
  }
  const latest = [...runs].sort((a, b) => new Date(b.started_at || 0) - new Date(a.started_at || 0))[0];
  summary.innerHTML = `<strong>Latest sync: ${esc(latest.status || "unknown")}</strong>
    <span>${latest.started_at ? `Started ${esc(formatDateTime(latest.started_at))}` : "Start time unavailable"}</span>
    ${latest.finished_at ? `<span>Finished ${esc(formatDateTime(latest.finished_at))}</span>` : ""}`;
  coverage.innerHTML = inboxCoverageHtml(latest.coverage);
}

function renderInboxConversation(conversation) {
  const selected = String(conversation.id) === String(inboxSelectedConversationId);
  const unread = !!conversation.linkedin_unread;
  const readLabel = conversation.linkedin_unread == null ? "LinkedIn read state unknown" :
    unread ? "LinkedIn unread" : "Read on LinkedIn";
  const unmatched = conversation.match_state === "unmatched";
  const ambiguous = conversation.match_state === "ambiguous";
  const reviewed = !!conversation.reviewed_at;
  const scanLabel = conversation.last_scan_unread_before_open === 1
    ? conversation.last_scan_restore_status === "restored"
      ? "Unread before scan; restored"
      : conversation.last_scan_restore_status === "pending"
        ? "Unread before scan; restore pending"
        : "Unread before scan; restore unverified"
    : "";
  const label = conversation.participant_name || "Unknown participant";
  return `<button class="inbox-conversation${selected ? " selected" : ""}" type="button"
      data-conversation-id="${esc(String(conversation.id))}" aria-pressed="${selected}">
    <span class="inbox-conversation-top">
      <span class="inbox-person">${esc(label)}</span>
      ${unread ? `<span class="inbox-unread-dot" aria-label="Unread on LinkedIn" title="Unread on LinkedIn"></span>` : ""}
    </span>
    <span class="inbox-preview">${esc(conversation.preview_text || "No message preview")}</span>
    <span class="inbox-row-meta">
      <span class="inbox-label${unread ? " unread" : ""}">${readLabel}</span>
      ${scanLabel ? `<span class="inbox-label${conversation.last_scan_restore_status === "restored" ? "" : " unmatched"}">${scanLabel}</span>` : ""}
      ${reviewed ? `<span class="inbox-label reviewed">Reviewed</span>` : `<span class="inbox-label pending-review">Needs review</span>`}
      ${unmatched ? `<span class="inbox-label unmatched">Unmatched</span>` : ""}
      ${ambiguous ? `<span class="inbox-label unmatched">Identity conflict</span>` : ""}
      <span>${Number(conversation.message_count) || 0} messages</span>
      ${Number(conversation.file_count) > 0 ? `<span>${Number(conversation.file_count)} files</span>` : ""}
    </span>
  </button>`;
}

async function loadInbox() {
  const seq = ++inboxLoadSeq;
  const operator = $("#operator").value;
  if (!operator) return;
  const list = $("#inboxConversationList");
  list.innerHTML = `<div class="inbox-empty">Loading conversations…</div>`;
  try {
    const [runData, conversationData] = await Promise.all([
      api(`/api/inbound/runs?${inboxQuery(operator)}`),
      api(`/api/inbound/conversations?${inboxQuery(operator)}&limit=100&offset=0`),
    ]);
    if (seq !== inboxLoadSeq || operator !== $("#operator").value) return;
    const runs = runData.runs || [];
    const conversations = conversationData.conversations || [];
    renderInboxSync(runs);
    inboxConversations = conversations;
    inboxTotal = Number(conversationData.total) || conversations.length;
    renderInboxList();
    if (!conversations.length) {
      list.innerHTML = `<div class="inbox-empty">No conversations have been saved for this session.</div>`;
      inboxSelectedConversationId = null;
      $("#inboxConversationDetail").innerHTML = `<div class="inbox-detail-empty">Messages and files will appear here after an inbox scan.</div>`;
      return;
    }
    const selectedStillExists = conversations.some(item => String(item.id) === String(inboxSelectedConversationId));
    if (!selectedStillExists) inboxSelectedConversationId = conversations[0].id;
    renderInboxList();
    loadInboxDetail(inboxSelectedConversationId);
  } catch (error) {
    if (seq !== inboxLoadSeq) return;
    list.innerHTML = `<div class="inbox-empty error" role="alert">Could not load inbox data: ${esc(error.message)}</div>`;
    $("#inboxSyncHealth").querySelector(".inbox-sync-summary").textContent = "Sync status unavailable.";
    $("#inboxSyncCoverage").innerHTML = "";
    $("#inboxConversationDetail").innerHTML = `<div class="inbox-detail-empty">Select Refresh to try again.</div>`;
  }
}

async function loadMoreInbox() {
  const operator = $("#operator").value;
  const button = $("#inboxLoadMore");
  const seq = inboxLoadSeq;
  button.disabled = true;
  try {
    const data = await api(`/api/inbound/conversations?${inboxQuery(operator)}&limit=100&offset=${inboxConversations.length}`);
    if (seq !== inboxLoadSeq || operator !== $("#operator").value) return;
    const known = new Set(inboxConversations.map(item => item.id));
    inboxConversations.push(...(data.conversations || []).filter(item => !known.has(item.id)));
    inboxTotal = Number(data.total) || inboxConversations.length;
    renderInboxList();
  } catch (error) {
    showToast("Could not load more conversations: " + error.message, "error");
  } finally {
    button.disabled = false;
  }
}

async function loadInboxDetail(conversationId) {
  const seq = ++inboxDetailSeq;
  const operator = $("#operator").value;
  const detail = $("#inboxConversationDetail");
  if (!operator || !conversationId) return;
  detail.innerHTML = `<div class="inbox-detail-empty">Loading conversation…</div>`;
  try {
    const data = await api(`/api/inbound/conversations/${encodeURIComponent(conversationId)}?${inboxQuery(operator)}`);
    if (seq !== inboxDetailSeq || operator !== $("#operator").value) return;
    renderInboxDetail(data.conversation);
  } catch (error) {
    if (seq !== inboxDetailSeq) return;
    detail.innerHTML = `<div class="inbox-detail-empty error" role="alert">Could not load this conversation: ${esc(error.message)}</div>`;
  }
}

function renderInboxDetail(conversation) {
  if (!conversation) return;
  const operator = $("#operator").value;
  const id = String(conversation.id);
  const participant = conversation.participant_name || "Unknown participant";
  const safeUrl = inboxSafeLinkedInUrl(conversation.contact_url);
  const messages = Array.isArray(conversation.messages) ? conversation.messages : [];
  const reviewed = !!conversation.reviewed_at;
  const unmatched = conversation.match_state === "unmatched";
  const ambiguous = conversation.match_state === "ambiguous";
  const readLabel = conversation.linkedin_unread == null ? "LinkedIn read state unknown" :
    conversation.linkedin_unread ? "LinkedIn unread" : "Read on LinkedIn";
  const scanLabel = conversation.last_scan_unread_before_open === 1
    ? conversation.last_scan_restore_status === "restored"
      ? "Unread before last scan; restored on LinkedIn"
      : "Unread before last scan; restoration needs attention"
    : "";
  const messageHtml = messages.length ? messages.map(message => {
    const direction = String(message.direction || "unknown").toLowerCase();
    const attachments = Array.isArray(message.attachments) ? message.attachments : [];
    return `<article class="inbox-message">
      <div class="inbox-message-head">
        <span>${esc(direction === "inbound" ? participant : direction === "outbound" ? "You" : direction)}</span>
        ${message.source_at ? `<time datetime="${esc(message.source_at)}">${esc(formatDateTime(message.source_at))}</time>` : ""}
      </div>
      <div class="inbox-message-body">${esc(message.body || "[No text]")}</div>
      ${attachments.length ? `<div class="inbox-attachments"><span>Files</span>${attachments.map(file => {
        const fileUrl = `/api/inbound/files/${encodeURIComponent(file.id)}?${inboxQuery(operator)}`;
        return file.status === "saved"
          ? `<a href="${esc(fileUrl)}" download>${esc(file.filename || "Download file")}</a>`
          : `<span class="inbox-file-status">${esc(file.filename || "File")} · ${esc(file.status || "not saved")}</span>`;
      }).join("")}</div>` : ""}
    </article>`;
  }).join("") : `<div class="inbox-detail-empty">No saved messages are available for this conversation.</div>`;
  $("#inboxConversationDetail").innerHTML = `
    <header class="inbox-detail-header">
      <div>
        <h3>${esc(participant)}</h3>
        <div class="inbox-detail-badges">
          <span class="inbox-label${conversation.linkedin_unread ? " unread" : ""}">${readLabel}</span>
          ${scanLabel ? `<span class="inbox-label${conversation.last_scan_restore_status === "restored" ? "" : " unmatched"}">${scanLabel}</span>` : ""}
          <span class="inbox-label${reviewed ? " reviewed" : " pending-review"}">${reviewed ? "Reviewed in LinkBound" : "Not reviewed in LinkBound"}</span>
          ${unmatched ? `<span class="inbox-label unmatched">Unmatched contact</span>` : ambiguous ? `<span class="inbox-label unmatched">Identity conflict</span>` : `<span class="inbox-label">${esc(conversation.match_state || "Contact status unknown")}</span>`}
        </div>
        <div class="inbox-detail-meta">${conversation.last_observed_at ? `Last observed ${esc(formatDateTime(conversation.last_observed_at))}` : "Observation time unavailable"}</div>
      </div>
      ${safeUrl ? `<a class="btn small" href="${esc(safeUrl)}" target="_blank" rel="noopener noreferrer">Open LinkedIn</a>` : ""}
    </header>
    <div class="inbox-detail-actions">
      <button class="btn small primary" id="inboxMarkReviewed" type="button" ${reviewed ? "disabled" : ""}>${reviewed ? "Reviewed" : "Mark reviewed"}</button>
      ${unmatched || ambiguous ? `<form class="inbox-link-form" id="inboxLinkForm">
        <label for="inboxContactUrl">Link to contact</label>
        <input id="inboxContactUrl" type="url" inputmode="url" placeholder="https://www.linkedin.com/in/..." value="${esc(safeUrl)}" required>
        <button class="btn small" type="submit">Link contact</button>
      </form>` : ""}
    </div>
    <div class="inbox-message-list">${messageHtml}</div>
  `;
  $("#inboxMarkReviewed")?.addEventListener("click", () => markInboxReviewed(id));
  $("#inboxLinkForm")?.addEventListener("submit", event => linkInboxContact(event, id));
}

async function markInboxReviewed(conversationId) {
  const operator = $("#operator").value;
  try {
    await api(`/api/inbound/conversations/${encodeURIComponent(conversationId)}/review?${inboxQuery(operator)}`, { method: "POST" });
    showToast("Conversation marked reviewed.");
    await loadInbox();
  } catch (error) {
    showToast("Could not mark reviewed: " + error.message, "error");
  }
}

async function linkInboxContact(event, conversationId) {
  event.preventDefault();
  const operator = $("#operator").value;
  const contactUrl = inboxSafeLinkedInUrl($("#inboxContactUrl").value.trim());
  if (!contactUrl) return showToast("Enter a valid LinkedIn profile URL.", "error");
  const button = event.currentTarget.querySelector("button[type=submit]");
  button.disabled = true;
  try {
    await api(`/api/inbound/conversations/${encodeURIComponent(conversationId)}/link?${inboxQuery(operator)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ contact_url: contactUrl }),
    });
    showToast("Conversation linked to contact.");
    await loadInbox();
  } catch (error) {
    showToast("Could not link contact: " + error.message, "error");
    button.disabled = false;
  }
}

$("#inboxRefresh")?.addEventListener("click", loadInbox);
$("#inboxLoadMore")?.addEventListener("click", loadMoreInbox);
$("#inboxExport")?.addEventListener("click", event => {
  const operator = $("#operator").value;
  if (!operator) {
    event.preventDefault();
    showToast("Select a LinkedIn session first.", "error");
    return;
  }
  event.currentTarget.href = `/api/inbound/export?${inboxQuery(operator)}`;
});
$("#inboxConversationList")?.addEventListener("click", event => {
  if (event.target.closest("a")) return;
  const button = event.target.closest("button[data-conversation-id]");
  if (!button) return;
  inboxSelectedConversationId = button.dataset.conversationId;
  $$("#inboxConversationList .inbox-conversation").forEach(item => {
    const active = item.dataset.conversationId === inboxSelectedConversationId;
    item.classList.toggle("selected", active);
    item.setAttribute("aria-pressed", String(active));
  });
  loadInboxDetail(inboxSelectedConversationId);
});

// ─── Batches ───────────────────────────────────────────────────────────────
let batchesGridApi = null;
let selectedBatchId = null;
let batchesLoadSeq = 0;

async function loadBatches() {
  const seq = ++batchesLoadSeq;
  const operator = $("#operator").value;
  if (!operator) return;
  let data;
  try {
    data = await api("/api/batches?operator=" + encodeURIComponent(operator) + "&limit=50");
  } catch (error) {
    if (seq === batchesLoadSeq) showToast("Could not load batches: " + error.message, "error");
    return;
  }
  if (seq !== batchesLoadSeq) return;

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
          valueFormatter: p => formatDateTime(p.value),
          flex: 1.5 },
        { headerName: "Details", width: 110, sortable: false, filter: false,
          cellRenderer: () => `<button class="btn tiny">View</button>` },
      ],
      defaultColDef: { sortable: true, resizable: true },
      pagination: true,
      paginationPageSize: 15,
      rowSelection: "single",
      onRowClicked: event => selectBatch(event.data),
    };
    batchesGridApi = agGrid.createGrid($("#batchesGrid"), gridOptions);
  } else {
    batchesGridApi.setGridOption("rowData", data.batches || []);
  }

  if (selectedBatchId) {
    const stillVisible = (data.batches || []).some(b => b.id === selectedBatchId);
    if (stillVisible) loadBatchDetail(selectedBatchId);
    else {
      selectedBatchId = null;
      $("#batchDetailCard").innerHTML = `<div class="batch-detail-empty">Select a batch to view its history.</div>`;
    }
  }
}

$("#batchesRefresh").addEventListener("click", loadBatches);

function selectBatch(batch) {
  if (!batch || !batch.id) return;
  selectedBatchId = batch.id;
  loadBatchDetail(batch.id);
}

async function loadBatchDetail(batchId) {
  const card = $("#batchDetailCard");
  const operator = $("#operator").value;
  card.innerHTML = `<div class="batch-detail-empty">Loading batch details...</div>`;
  try {
    const data = await api(`/api/batches/${batchId}?operator=${encodeURIComponent(operator)}`);
    if (operator !== $("#operator").value) return;
    renderBatchDetail(data.batch, data.requests || []);
  } catch (err) {
    card.innerHTML = `<div class="batch-detail-empty error">Could not load batch details: ${esc(err.message)}</div>`;
  }
}

function renderBatchDetail(batch, requests) {
  const card = $("#batchDetailCard");
  const failed = requests.filter(r => (r.status || "").startsWith("failed")).length;
  const flagged = requests.filter(r => r.status === "mismatch_flagged" || r.status === "needs_attention").length;

  card.innerHTML = `
    <div class="batch-detail-header">
      <div>
        <div class="card-title" style="margin-bottom: 4px;">${esc(batch.name || batch.public_id || "Batch Details")}</div>
        <div class="batch-detail-meta">
          ${esc(batch.public_id || "")}
          ${batch.operator ? ` · ${esc(batch.operator)}` : ""}
          ${batch.started_at ? ` · ${esc(formatDateTime(batch.started_at))}` : ""}
        </div>
      </div>
      <div class="batch-detail-summary">
        <span>${requests.length} profiles</span>
        <span>${failed} failed</span>
        <span>${flagged} flagged</span>
      </div>
    </div>
    <div class="batch-request-list">
      ${requests.length ? requests.map(renderBatchRequest).join("") : `
        <div class="batch-detail-empty">No per-profile records were saved for this batch.</div>
      `}
    </div>
  `;
}

function renderBatchRequest(req) {
  const trace = Array.isArray(req.decision_trace) ? req.decision_trace : [];
  const traceHtml = trace.length
    ? `<details class="request-trace">
         <summary>Decision trace (${trace.length})</summary>
         <ol>${trace.map(step => `<li>${esc(String(step))}</li>`).join("")}</ol>
       </details>`
    : "";
  const screenshot = req.screenshot_path
    ? `<a class="request-link" href="/api/screenshot?path=${encodeURIComponent(req.screenshot_path)}" target="_blank" rel="noopener noreferrer">Screenshot</a>`
    : "";
  const name = req.full_name || req.linkedin_url || req.public_id;
  const url = req.linkedin_url
    ? `<a class="request-link" href="${esc(req.linkedin_url)}" target="_blank" rel="noopener noreferrer">Open profile</a>`
    : "";

  return `
    <div class="batch-request">
      <div class="batch-request-main">
        <div class="feed-head">
          <span class="feed-who">${esc(name)}</span>
          ${req.company_csv ? `<span class="feed-co">${esc(req.company_csv)}</span>` : ""}
          ${req.action_executed ? `<span class="feed-act">${esc(req.action_executed.replace(/_/g, " "))}</span>` : ""}
          ${badge(req.status)}
        </div>
        <div class="request-detail">${esc(req.detail || "No detail recorded.")}</div>
        <div class="request-links">
          ${url}
          ${screenshot}
          ${req.created_at ? `<span>${esc(formatDateTime(req.created_at))}</span>` : ""}
        </div>
        ${traceHtml}
      </div>
    </div>
  `;
}

// ─── Analytics ─────────────────────────────────────────────────────────────
let chartInstance = null;
let templateChartInstance = null;
let analyticsLoadSeq = 0;

async function loadAnalytics() {
  const seq = ++analyticsLoadSeq;
  const operator = $("#operator").value;
  if (!operator) return;
  let data;
  try {
    data = await api("/api/analytics/dashboard?operator=" + encodeURIComponent(operator));
  } catch (error) {
    if (seq === analyticsLoadSeq) showToast("Could not load analytics: " + error.message, "error");
    return;
  }
  if (seq !== analyticsLoadSeq) return;
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
    tbody.innerHTML = `<tr><td colspan="6" style="text-align: center; color: var(--text-faint); padding: 32px;">
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
        <td style="font-family: var(--font-mono); font-size: 0.78rem; color: var(--text-faint);">${esc(op.profile_dir)}</td>
        <td>
          <div class="op-identity-control">
            <input type="url" aria-label="LinkedIn profile URL for ${esc(displayName)}" value="${esc(op.linkedin_self_url || "")}" placeholder="linkedin.com/in/username" spellcheck="false">
            <button class="btn small" type="button" data-save-identity="${esc(op.key)}">Save URL</button>
          </div>
        </td>
        <td style="font-size: 0.85rem; color: var(--text-faint);">${createdDate}</td>
        <td>
          <button class="btn danger small" onclick="deleteOperator('${esc(op.key)}')">Delete</button>
        </td>
      </tr>
    `);
  });
}

$("#opsTable tbody").addEventListener("click", async (event) => {
  const button = event.target.closest("[data-save-identity]");
  if (!button) return;
  const key = button.dataset.saveIdentity;
  const input = button.closest(".op-identity-control").querySelector("input");
  const url = input.value.trim();
  if (!url) return showToast("Enter the LinkedIn profile URL shown under Me.", "error");
  button.disabled = true;
  try {
    await api(`/api/operators/${encodeURIComponent(key)}/linkedin-identity`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ linkedin_url: url }),
    });
    showToast("LinkedIn profile URL saved.");
    await loadOperators();
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    button.disabled = false;
  }
});

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
async function boot() {
  try {
    await loadConfig();
  } catch (e) {
    showToast("Failed to load config: " + e.message, "error");
  }
  loadTemplatesData().catch(() => {});
  restoreActiveView();
  connectWS();
  refreshRunStatus();
}

boot();
