const apiKeyStorageKey = "omf_api_key";

function readStoredApiKey() {
  let sessionKey = "";
  try { sessionKey = sessionStorage.getItem(apiKeyStorageKey) || ""; } catch { /* Continue without session storage. */ }
  try {
    const persistentKey = localStorage.getItem(apiKeyStorageKey) || "";
    if (!persistentKey && sessionKey) localStorage.setItem(apiKeyStorageKey, sessionKey);
    sessionStorage.removeItem(apiKeyStorageKey);
    return persistentKey || sessionKey;
  } catch {
    return sessionKey;
  }
}

function persistApiKey(value) {
  try {
    localStorage.setItem(apiKeyStorageKey, value);
    sessionStorage.removeItem(apiKeyStorageKey);
  } catch {
    try { sessionStorage.setItem(apiKeyStorageKey, value); } catch { /* The live connection still works for this page. */ }
  }
}

function clearStoredApiKey() {
  try { localStorage.removeItem(apiKeyStorageKey); } catch { /* Persistent storage may be unavailable. */ }
  try { sessionStorage.removeItem(apiKeyStorageKey); } catch { /* Session storage may be unavailable. */ }
}

const state = { health: null, automations: [], runs: [], tasks: [], apiKey: readStoredApiKey(), pendingAutomationDeleteId: "" };
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const platformNames = { douyin: "抖音", xiaohongshu: "小红书", bilibili: "哔哩哔哩", youtube: "YouTube" };
const statusNames = { draft: "等待策划", planned: "分镜已完成", assets_generating: "生成画面", composing: "合成视频", generated: "视频处理中", review_rejected: "审核拒绝", approved: "审核通过", publishing: "发布中", published: "已完成", partial_failure: "部分失败", automation_failed: "自动化失败", queued: "已排队", waiting_for_media_runtime: "等待媒体运行时", failed: "失败" };
const terminalStatuses = new Set(["published", "review_rejected", "partial_failure", "automation_failed", "failed"]);

function taskProgress(task) {
  if (!task) return 0;
  if (terminalStatuses.has(task.status)) return 100;
  if (task.status === "draft") return 6;
  if (task.status === "planned") return 18;
  if (task.status === "assets_generating") {
    const shots = task.content_plan?.shots || [];
    const complete = shots.filter((shot) => shot.status === "complete").length;
    const assetRatio = shots.length ? complete / shots.length : 0;
    return Math.round(28 + assetRatio * 42);
  }
  if (task.status === "composing") return 74;
  if (task.status === "generated") return Math.round(78 + Math.min(100, Number(task.metadata?.video_progress || 0)) * .14);
  if (task.status === "approved") return 94;
  if (task.status === "publishing") return 97;
  return 3;
}

function escapeHtml(value = "") {
  return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.apiKey) headers["X-API-Key"] = state.apiKey;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) {
    clearStoredApiKey();
    state.apiKey = "";
    showConnect("密钥无效，请重新连接。");
    throw new Error("本地 API 密钥无效");
  }
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`);
  return payload;
}

function formatTime(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value));
}

function shortId(value = "") { return value ? `${value.slice(0, 7)}…${value.slice(-4)}` : "—"; }
function badge(status) {
  const tone = ["published", "approved"].includes(status) ? "good" : ["review_rejected", "partial_failure", "automation_failed", "failed"].includes(status) ? "bad" : ["planned", "assets_generating", "composing", "generated", "publishing", "queued", "waiting_for_media_runtime"].includes(status) ? "warn" : "neutral";
  return `<span class="status-badge status-${tone}">${escapeHtml(statusNames[status] || status)}</span>`;
}

function showToast(message) {
  const toast = $("#toast");
  toast.textContent = message;
  toast.classList.add("show");
  clearTimeout(showToast.timer);
  showToast.timer = setTimeout(() => toast.classList.remove("show"), 2800);
}

function showConnect(message = "") {
  $("#connect-error").textContent = message;
  $("#clear-api-key-button").hidden = !state.apiKey;
  const modal = $("#connect-modal");
  if (!modal.open) modal.showModal();
  setTimeout(() => $("#api-key-input").focus(), 80);
}

async function loadData({ quiet = false } = {}) {
  try {
    state.health = await api("/health");
    renderHealth();
    [state.automations, state.runs, state.tasks] = await Promise.all([api("/automations"), api("/automation-runs"), api("/tasks")]);
    renderAutomations(); renderLedger(); renderSignalBoard();
    if (!quiet) showToast("本地数据已同步");
  } catch (error) {
    if (!quiet && state.apiKey) showToast(error.message);
  }
}

function renderHealth() {
  const health = state.health || {};
  $("#health-status").textContent = health.status === "ok" ? "运行正常" : "需要检查";
  $("#health-dot").classList.toggle("online", health.status === "ok");
  $("#top-health-status").textContent = health.status === "ok" ? "LOCAL ONLINE" : "CHECK SYSTEM";
  $("#top-health-dot").classList.toggle("online", health.status === "ok");
  $("#model-status").textContent = health.llm_primary_model || "未配置";
  $("#fallback-status").textContent = health.llm_fallback_enabled ? "CLOUD FALLBACK" : "LOCAL ONLY";
  $("#scheduler-status").textContent = health.scheduler_running ? "调度中" : "已停止";
  $("#store-status").textContent = (health.store_backend || "—").toUpperCase();
  $("#publish-status").textContent = health.publish_mode === "dry-run" ? "模拟发布" : "真实发布";
}

function renderAutomations() {
  const root = $("#automation-list");
  $("#plan-count").textContent = state.automations.length;
  if (!state.automations.length) { root.innerHTML = '<div class="empty-state">还没有计划。创建第一条本地内容生产线。</div>'; return; }
  root.innerHTML = state.automations.map((item) => {
    const latestTask = state.tasks
      .filter((task) => task.automation_id === item.id)
      .sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];
    const isRunning = latestTask && !terminalStatuses.has(latestTask.status);
    const progress = taskProgress(latestTask);
    const stage = latestTask ? (statusNames[latestTask.status] || latestTask.status) : "尚未运行";
    return `
    <article class="automation-card ${item.enabled ? "enabled" : ""} ${isRunning ? "is-running" : ""}">
      <i class="automation-rail"></i>
      <div class="automation-body">
        <div class="automation-kicker"><span class="automation-state">${item.enabled ? "● ACTIVE" : "○ PAUSED"}</span><span>每 ${item.interval_minutes} 分钟</span><span>上次 ${formatTime(item.last_run_at)}</span></div>
        <h3>${escapeHtml(item.name)}</h3><p class="automation-topic">${escapeHtml(item.topic)}</p>
        <div class="tag-row">${item.platforms.map((p) => `<span class="tag">${platformNames[p] || p}</span>`).join("")}<span class="tag">${item.video_materials.length ? `${item.video_materials.length} 份备用素材` : "AI 生成素材"}</span></div>
        ${latestTask ? `<div class="automation-progress ${isRunning ? "is-active" : ""}"><div class="automation-progress-copy"><span>${isRunning ? "正在执行" : "最近一次"} · ${escapeHtml(stage)}</span><b>${progress}%</b></div><div class="automation-progress-track" role="progressbar" aria-label="${escapeHtml(item.name)}运行进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}"><i style="width:${progress}%"></i></div></div>` : ""}
      </div>
      <div class="automation-actions">
        <button class="run-button" data-action="run" data-id="${item.id}" ${isRunning ? 'disabled aria-busy="true"' : ""}>${isRunning ? `${escapeHtml(stage)} · ${progress}%` : "立即运行 →"}</button>
        <div class="automation-secondary-actions"><button class="toggle-button" data-action="toggle" data-id="${item.id}" data-enabled="${item.enabled}">${item.enabled ? "暂停" : "启用"}</button><span></span><button class="delete-button" data-action="delete" data-id="${item.id}" data-name="${escapeHtml(item.name)}" aria-label="删除计划 ${escapeHtml(item.name)}">删除</button></div>
      </div>
    </article>`;
  }).join("");
}

function renderLedger() {
  const automationMap = Object.fromEntries(state.automations.map((item) => [item.id, item]));
  $("#runs-table").innerHTML = state.runs.length ? state.runs.map((run) => `
    <tr><td>${badge(run.status)}</td><td><b>${escapeHtml(automationMap[run.automation_id]?.name || "未知计划")}</b><br><span class="mono muted">${shortId(run.task_id)}</span></td><td>${formatTime(run.created_at)}</td><td class="muted">${escapeHtml(run.detail || "等待编排器接管")}</td><td><button class="table-action" data-task-id="${run.task_id}">检查任务</button></td></tr>`).join("") : '<tr><td colspan="5"><div class="empty-state">还没有运行记录</div></td></tr>';
  $("#tasks-table").innerHTML = state.tasks.length ? state.tasks.map((task) => {
    const progress = taskProgress(task);
    return `<tr><td>${badge(task.status)}</td><td><b>${escapeHtml(task.title || task.topic)}</b><br><span class="mono muted">${shortId(task.id)}</span></td><td>${task.platforms.map((p) => platformNames[p] || p).join(" · ")}</td><td><div class="progress-track" title="${progress}%"><i style="width:${Math.max(0, Math.min(100, progress))}%"></i></div></td><td>${formatTime(task.updated_at)}</td><td><button class="table-action" data-task-id="${task.id}">详情</button></td></tr>`;
  }).join("") : '<tr><td colspan="6"><div class="empty-state">还没有内容任务</div></td></tr>';
}

function renderSignalBoard() {
  const active = state.tasks.filter((task) => !terminalStatuses.has(task.status)).length;
  const complete = state.tasks.filter((task) => task.status === "published").length;
  const failed = state.tasks.filter((task) => ["review_rejected", "partial_failure", "automation_failed"].includes(task.status)).length;
  $("#active-count").textContent = String(active).padStart(2, "0");
  $("#task-count").textContent = state.tasks.length;
  $("#pending-count").textContent = active;
  $("#complete-count").textContent = complete;
  $("#failed-count").textContent = failed;
  $("#activity-meter").style.width = `${Math.min(100, active * 18)}%`;
}

function openTaskDetail(taskId) {
  const task = state.tasks.find((item) => item.id === taskId);
  if (!task) { showToast("未找到该任务"); return; }
  const checks = task.audit?.checks || [];
  $("#detail-content").innerHTML = `<h2>${escapeHtml(task.title || task.topic)}</h2>
    <div class="detail-grid"><div><small>STATUS</small><p>${badge(task.status)}</p></div><div><small>TASK ID</small><p class="mono">${escapeHtml(task.id)}</p></div><div><small>PLATFORMS</small><p>${task.platforms.map((p) => platformNames[p] || p).join(" · ")}</p></div><div><small>VIDEO PROGRESS</small><p>${escapeHtml(task.metadata?.video_progress ?? "—")}%</p></div><div><small>MODEL</small><p>${escapeHtml(task.metadata?.llm_generation?.model || "—")}</p></div><div><small>UPDATED</small><p>${formatTime(task.updated_at)}</p></div></div>
    ${task.automation_error ? `<section class="detail-section"><h3>失败原因</h3><pre>${escapeHtml(task.automation_error)}</pre></section>` : ""}
    ${checks.length ? `<section class="detail-section"><h3>审核检查</h3><pre>${escapeHtml(checks.map((c) => `${c.passed ? "✓" : "×"} ${c.name} · ${c.score} · ${c.detail}`).join("\n"))}</pre></section>` : ""}
    ${task.script ? `<section class="detail-section"><h3>生成脚本</h3><pre>${escapeHtml(task.script)}</pre></section>` : ""}`;
  $("#detail-modal").showModal();
}

async function handleAutomationAction(button) {
  const { action, id, enabled } = button.dataset;
  if (action === "delete") {
    state.pendingAutomationDeleteId = id;
    $("#delete-plan-name").textContent = button.dataset.name || "该计划";
    $("#delete-modal").showModal();
    return;
  }
  const originalText = button.textContent;
  button.disabled = true;
  button.setAttribute("aria-busy", "true");
  button.textContent = action === "run" ? "排队中…" : "更新中…";
  try {
    if (action === "run") { const run = await api(`/automations/${id}/run`, { method: "POST" }); showToast(`任务已开始 · ${shortId(run.task_id)}，进度会显示在计划卡片中`); }
    if (action === "toggle") { await api(`/automations/${id}/${enabled === "true" ? "disable" : "enable"}`, { method: "POST" }); showToast(enabled === "true" ? "计划已暂停" : "计划已启用"); }
    await loadData({ quiet: true });
  } catch (error) { showToast(error.message.includes("active run") ? "该计划正在运行，进度已显示在卡片中" : error.message); }
  finally { button.disabled = false; button.removeAttribute("aria-busy"); button.textContent = originalText; }
}

async function confirmAutomationDelete() {
  const id = state.pendingAutomationDeleteId;
  if (!id) return;
  const button = $("#confirm-delete-button");
  button.disabled = true;
  try {
    await api(`/automations/${id}`, { method: "DELETE" });
    state.pendingAutomationDeleteId = "";
    $("#delete-modal").close();
    await loadData({ quiet: true });
    showToast("计划已删除");
  } catch (error) { showToast(error.message); }
  finally { button.disabled = false; }
}

$("#connect-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  state.apiKey = $("#api-key-input").value.trim();
  try {
    await api("/tasks");
    persistApiKey(state.apiKey);
    $("#api-key-input").value = "";
    $("#connect-modal").close();
    $("#connect-error").textContent = "";
    await loadData({ quiet: true });
    showToast("本地控制面已连接");
  } catch (error) { $("#connect-error").textContent = error.message; }
});

$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const formElement = event.currentTarget;
  const submitButton = formElement.querySelector('button[type="submit"]');
  $("#create-error").textContent = "";
  submitButton.disabled = true;
  const form = new FormData(formElement);
  const platforms = form.getAll("platforms");
  if (!platforms.length) { $("#create-error").textContent = "至少选择一个平台"; submitButton.disabled = false; return; }
  const materials = String(form.get("materials") || "").split(/\n+/).map((v) => v.trim()).filter(Boolean);
  try {
    await api("/automations", { method: "POST", body: JSON.stringify({ name: form.get("name"), topic: form.get("topic"), platforms, video_materials: materials, interval_minutes: Number(form.get("interval_minutes")), enabled: form.get("enabled") === "on" }) });
    formElement.reset(); $("#create-modal").close(); await loadData({ quiet: true }); showToast("新计划已保存");
  } catch (error) { $("#create-error").textContent = error.message; }
  finally { submitButton.disabled = false; }
});

document.addEventListener("click", (event) => {
  const action = event.target.closest("[data-action]"); if (action) handleAutomationAction(action);
  const task = event.target.closest("[data-task-id]"); if (task) openTaskDetail(task.dataset.taskId);
  const tab = event.target.closest("[data-tab]"); if (tab) { $$(".tab").forEach((item) => { const active = item === tab; item.classList.toggle("is-active", active); item.setAttribute("aria-selected", String(active)); }); $("#runs-pane").classList.toggle("is-hidden", tab.dataset.tab !== "runs"); $("#tasks-pane").classList.toggle("is-hidden", tab.dataset.tab !== "tasks"); }
  if (event.target.closest("[data-close-modal]")) $("#create-modal").close();
  if (event.target.closest("[data-close-detail]")) $("#detail-modal").close();
  if (event.target.closest("[data-close-delete]")) { state.pendingAutomationDeleteId = ""; $("#delete-modal").close(); }
});

$("#open-create-button").addEventListener("click", () => { $("#create-error").textContent = ""; $("#create-modal").showModal(); setTimeout(() => $("#create-form [name=name]").focus(), 60); });
$("#connect-settings-button").addEventListener("click", () => showConnect());
$("#clear-api-key-button").addEventListener("click", () => {
  clearStoredApiKey();
  state.apiKey = "";
  $("#clear-api-key-button").hidden = true;
  $("#connect-error").textContent = "已清除保存的密钥，请输入密钥重新连接。";
  $("#api-key-input").focus();
});
$("#confirm-delete-button").addEventListener("click", confirmAutomationDelete);
$("#refresh-button").addEventListener("click", () => loadData());
$$('.modal').forEach((modal) => modal.addEventListener("click", (event) => { if (event.target === modal) modal.close(); }));
$("#delete-modal").addEventListener("close", () => { state.pendingAutomationDeleteId = ""; });
setInterval(() => { $("#local-clock").textContent = `${new Date().toLocaleTimeString("zh-CN", { hour12: false })} CST`; }, 1000);
setInterval(() => { if (state.apiKey && !document.hidden) loadData({ quiet: true }); }, 5000);
loadData({ quiet: true });
