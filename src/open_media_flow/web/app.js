const apiKeyStorageKey = "omf_api_key";
const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const platformNames = { douyin: "抖音", xiaohongshu: "小红书", bilibili: "哔哩哔哩", youtube: "YouTube" };
const statusNames = { draft: "等待策划", planned: "分镜已完成", assets_generating: "生成画面", lip_syncing: "同步口型", composing: "合成视频", generated: "视频处理中", review_rejected: "审核拒绝", approved: "审核通过", publishing: "发布中", published: "已完成", partial_failure: "部分失败", automation_failed: "自动化失败", cancelled: "已取消", pending: "等待生成", queued: "已排队", running: "执行中", complete: "已完成", skipped: "已降级", waiting_for_media_runtime: "等待媒体运行时", failed: "失败" };
const terminalStatuses = new Set(["published", "review_rejected", "partial_failure", "automation_failed", "failed", "cancelled"]);
const failureStatuses = new Set(["review_rejected", "partial_failure", "automation_failed", "failed"]);
const timelineStages = ["策划", "素材", "配音", "口型", "合成", "审核", "发布"];
const presentationNames = { narration: "旁白镜头", mixed: "混合讲述", talking_head: "正面讲话" };
const templates = {
  "ai-tools": { name: "每日 AI 工具观察", topic: "面向普通创作者，解读值得关注的本地 AI 工具、自动化实践和真实使用体验。", interval: "1440", platforms: ["douyin", "xiaohongshu", "bilibili", "youtube"] },
  knowledge: { name: "每周知识解读", topic: "把一个复杂知识点讲清楚，强调事实、结构、例子和可执行结论。", interval: "10080", platforms: ["bilibili", "youtube"] },
  story: { name: "轻故事短片", topic: "创作节奏明快、有反转、有明确情绪落点的竖屏轻故事短片。", interval: "1440", platforms: ["douyin", "xiaohongshu"] },
};

function readStoredApiKey() {
  let sessionKey = "";
  try { sessionKey = sessionStorage.getItem(apiKeyStorageKey) || ""; } catch { /* Storage is optional. */ }
  try {
    const persistentKey = localStorage.getItem(apiKeyStorageKey) || "";
    if (!persistentKey && sessionKey) localStorage.setItem(apiKeyStorageKey, sessionKey);
    sessionStorage.removeItem(apiKeyStorageKey);
    return persistentKey || sessionKey;
  } catch { return sessionKey; }
}

function persistApiKey(value) {
  try { localStorage.setItem(apiKeyStorageKey, value); sessionStorage.removeItem(apiKeyStorageKey); }
  catch { try { sessionStorage.setItem(apiKeyStorageKey, value); } catch { /* Live page connection still works. */ } }
}

function clearStoredApiKey() {
  try { localStorage.removeItem(apiKeyStorageKey); } catch { /* Ignore unavailable storage. */ }
  try { sessionStorage.removeItem(apiKeyStorageKey); } catch { /* Ignore unavailable storage. */ }
}

const state = {
  health: null, automations: [], runs: [], tasks: [],
  apiKey: readStoredApiKey(), pendingAutomationDeleteId: "", pendingCancelTaskId: "",
  editingAutomationId: "", wizardStep: 1, ledgerAutomationId: "", activeTab: "runs",
};

function escapeHtml(value = "") { return String(value).replace(/[&<>'"]/g, (char) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]); }
function shortId(value = "") { return value ? `${value.slice(0, 7)}…${value.slice(-4)}` : "—"; }
function formatTime(value) { return value ? new Intl.DateTimeFormat("zh-CN", { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", hour12: false }).format(new Date(value)) : "—"; }
function formatFullTime(value) { return value ? new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false }).format(new Date(value)) : "—"; }
function formatElapsed(value) { const seconds = Math.max(0, Math.floor((Date.now() - new Date(value).getTime()) / 1000)); const hours = Math.floor(seconds / 3600); const minutes = Math.floor((seconds % 3600) / 60); const rest = seconds % 60; return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}` : `${String(minutes).padStart(2, "0")}:${String(rest).padStart(2, "0")}`; }
function formatDuration(start, end = new Date()) { if (!start) return "—"; const seconds = Math.max(0, Math.floor((new Date(end).getTime() - new Date(start).getTime()) / 1000)); if (seconds < 60) return `${seconds} 秒`; const minutes = Math.floor(seconds / 60); if (minutes < 60) return `${minutes} 分 ${seconds % 60} 秒`; return `${Math.floor(minutes / 60)} 小时 ${minutes % 60} 分`; }
function formatElapsedSeconds(value) { const seconds = Math.max(0, Math.floor(Number(value) || 0)); if (seconds < 60) return `${seconds} 秒`; const minutes = Math.floor(seconds / 60); return `${minutes} 分 ${seconds % 60} 秒`; }

function badge(status) {
  const tone = ["published", "approved"].includes(status) ? "good" : failureStatuses.has(status) ? "bad" : status === "cancelled" ? "neutral" : "warn";
  return `<span class="status-badge status-${tone}">${escapeHtml(statusNames[status] || status)}</span>`;
}

function mediaProviderName(provider) {
  if (!provider) return "等待分配引擎";
  if (["comfyui", "rife-mlx", "qwen3-tts"].includes(provider)) return "OpenMediaFlow 本地引擎";
  return provider;
}

function taskProgress(task) {
  if (!task) return { value: 0, kind: "阶段估算" };
  if (task.status === "generated" && task.metadata?.video_progress != null) return { value: Math.round(78 + Math.min(100, Number(task.metadata.video_progress)) * .14), kind: "引擎实时" };
  if (["published", "partial_failure"].includes(task.status)) return { value: 100, kind: "最终状态" };
  if (task.status === "review_rejected") return { value: 94, kind: "审核终止" };
  if (["automation_failed", "failed", "cancelled"].includes(task.status)) {
    if (task.audit) return { value: 94, kind: "终止位置" };
    if (task.media_path || task.generation_job_id) return { value: 78, kind: "终止位置" };
    if (task.content_plan) return { value: 28, kind: "终止位置" };
    return { value: 8, kind: "终止位置" };
  }
  if (task.status === "draft") return { value: 6, kind: "阶段估算" };
  if (task.status === "planned") return { value: 18, kind: "阶段估算" };
  if (task.status === "assets_generating") {
    const shots = task.content_plan?.shots || [];
    const complete = shots.filter((shot) => shot.status === "complete").length;
    return { value: Math.round(28 + (shots.length ? complete / shots.length : 0) * 35), kind: shots.length ? "素材实况" : "阶段估算" };
  }
  if (task.status === "lip_syncing") return { value: Math.round(64 + Math.min(100, Number(task.metadata?.lip_sync_progress || 0)) * .12), kind: "口型实况" };
  if (task.status === "composing") return { value: 78, kind: "阶段估算" };
  if (task.status === "approved") return { value: 94, kind: "阶段估算" };
  if (task.status === "publishing") return { value: 97, kind: "阶段估算" };
  return { value: 3, kind: "阶段估算" };
}

function taskStageIndex(status) {
  if (status === "draft") return 0;
  if (["planned", "assets_generating", "waiting_for_media_runtime"].includes(status)) return 1;
  if (status === "lip_syncing") return 3;
  if (["composing", "generated"].includes(status)) return 4;
  if (["approved", "review_rejected"].includes(status)) return 5;
  return 6;
}

function retryState(task) {
  const retry = task?.metadata?.automation_retry || {};
  const max = Number(retry.max_attempts || state.health?.automation_max_attempts || 0);
  const attempt = Number(retry.attempt || task?.automation_attempts || 0);
  return { attempt, max, stage: retry.stage || task?.status || "", willRetry: Boolean(retry.will_retry) && !terminalStatuses.has(task?.status) };
}

function retryTitle(retry) {
  if (retry.stage === "draft") return "内容方案未通过，正在自动修复";
  if (retry.stage === "assets_generating") return "素材生成遇到问题，正在自动恢复";
  if (retry.stage === "lip_syncing") return "口型同步遇到问题，正在自动恢复";
  if (["composing", "generated"].includes(retry.stage)) return "视频合成遇到问题，正在自动恢复";
  return "自动编排遇到问题，正在重试";
}

function taskDisplayStage(task) {
  const retry = retryState(task);
  if (retry.willRetry) return `自动修复重试 ${retry.attempt}/${retry.max}`;
  return statusNames[task?.status] || task?.status || "尚未运行";
}

async function api(path, options = {}) {
  const headers = { ...(options.headers || {}) };
  if (state.apiKey) headers["X-API-Key"] = state.apiKey;
  if (options.body) headers["Content-Type"] = "application/json";
  const response = await fetch(path, { ...options, headers });
  if (response.status === 401) { clearStoredApiKey(); state.apiKey = ""; showConnect("自动连接失效，请输入备用密钥。"); throw new Error("本地 API 密钥无效"); }
  if (response.status === 204) return {};
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.detail || `请求失败 (${response.status})`);
  return payload;
}

function showToast(message, { persistent = false } = {}) {
  const toast = $("#toast"); toast.textContent = message; toast.classList.add("show"); clearTimeout(showToast.timer);
  if (!persistent) showToast.timer = setTimeout(() => toast.classList.remove("show"), 4200);
}

function showConnect(message = "") { $("#connect-error").textContent = message; $("#clear-api-key-button").hidden = !state.apiKey; renderReadiness(); const modal = $("#connect-modal"); if (!modal.open) modal.showModal(); setTimeout(() => $("#api-key-input").focus(), 80); }

async function loadData({ quiet = false } = {}) {
  try {
    state.health = await api("/health");
    [state.automations, state.runs, state.tasks] = await Promise.all([api("/automations"), api("/automation-runs"), api("/tasks")]);
    renderAll();
    $("#last-sync").textContent = `刚刚同步 · ${new Date().toLocaleTimeString("zh-CN", { hour12: false, hour: "2-digit", minute: "2-digit" })}`;
    if (!quiet) showToast("本地数据已同步");
  } catch (error) { if (!quiet) showToast(error.message); }
}

function renderAll() {
  renderHealth(); renderAutomations(); renderLedger(); renderSignalBoard(); renderRunDock(); renderAttention(); renderRuntimeBanner();
  const detailModal = $("#detail-modal");
  if (detailModal.open && detailModal.dataset.taskId) {
    const task = state.tasks.find((item) => item.id === detailModal.dataset.taskId);
    const version = task ? `${task.status}:${task.updated_at}` : "missing";
    if (detailModal.dataset.taskVersion !== version) openTaskDetail(detailModal.dataset.taskId);
  }
}

function renderHealth() {
  const health = state.health || {};
  const components = health.components || {};
  $("#health-status").textContent = health.status === "ok" ? "运行正常" : "需要启动"; $("#health-dot").classList.toggle("online", health.status === "ok");
  $("#top-health-status").textContent = health.status === "ok" ? "LOCAL ONLINE" : "START RUNTIME"; $("#top-health-dot").classList.toggle("online", health.status === "ok");
  $("#model-status").textContent = health.llm_primary_model || "未配置"; $("#fallback-status").textContent = health.llm_fallback_enabled ? "CLOUD FALLBACK" : "LOCAL ONLY";
  $("#generation-status").textContent = health.generation_ready ? "画面与配音就绪" : components.generation_engine_ready ? "等待配音增强" : "尚未启动";
  $("#publish-status").textContent = health.publish_mode === "dry-run" ? "模拟发布" : "真实发布";
  renderReadiness();
}

function renderReadiness() {
  const health = state.health || {};
  const components = health.components || {};
  const rows = [["控制与调度", Boolean(health.scheduler_running)], ["内容模型", Boolean(components.content_model_ready)], ["画面生成引擎", Boolean(components.generation_engine_ready)], ["配音引擎", Boolean(components.voice_engine_ready)], ["唇形同步（可选）", Boolean(components.lip_sync_engine_ready)], ["动态增强", Boolean(components.motion_engine_ready)], ["成片合成器", Boolean(components.video_compositor_ready)], ["发布安全门禁", health.publish_mode === "dry-run"]];
  $("#readiness-list").innerHTML = rows.map(([label, ready]) => `<div><i class="${ready ? "ready" : ""}"></i><span>${label}</span><b>${ready ? "READY" : "OFFLINE"}</b></div>`).join("");
}

function renderRuntimeBanner() {
  const health = state.health || {}; const components = health.components || {}; const banner = $("#runtime-banner");
  if (!health.media_generation_enabled || health.generation_ready) { banner.hidden = true; return; }
  const missing = [["内容模型", components.content_model_ready], ["画面生成", components.generation_engine_ready], ["配音", components.voice_engine_ready], ["动态增强", components.motion_engine_ready], ["成片合成", components.video_compositor_ready]].filter(([, ready]) => !ready).map(([label]) => label);
  $("#runtime-copy").textContent = `完整生产线未就绪${missing.length ? ` · 待启动：${missing.join("、")}` : ""}；运行中的任务会安全等待。`;
  banner.hidden = false;
}

function automationForTask(task) { return state.automations.find((item) => item.id === task?.automation_id); }
function latestTaskForAutomation(id) { return state.tasks.filter((task) => task.automation_id === id).sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0]; }

function renderRunDock() {
  const task = state.tasks.filter((item) => !terminalStatuses.has(item.status)).sort((a, b) => new Date(b.created_at) - new Date(a.created_at))[0];
  const dock = $("#run-dock");
  if (!task) { dock.hidden = true; dock.dataset.taskId = ""; return; }
  const progress = taskProgress(task); const index = taskStageIndex(task.status); const plan = automationForTask(task);
  dock.hidden = false; dock.dataset.taskId = task.id; $("#run-dock-plan").textContent = plan?.name || task.title || task.topic; $("#run-dock-stage").textContent = taskDisplayStage(task);
  $("#run-dock-kind").textContent = progress.kind; $("#run-dock-percent").textContent = `${progress.value}%`; $("#run-dock-elapsed").textContent = formatElapsed(task.created_at); $("#run-dock-meter").style.width = `${progress.value}%`;
  $("#run-timeline").innerHTML = timelineStages.map((label, stageIndex) => `<li class="${stageIndex < index ? "is-done" : stageIndex === index ? "is-current" : ""}"><i>${stageIndex < index ? "✓" : String(stageIndex + 1).padStart(2, "0")}</i><span>${label}</span></li>`).join("");
}

function renderAutomations() {
  const root = $("#automation-list"); const query = $("#plan-search").value.trim().toLowerCase();
  const items = state.automations.filter((item) => `${item.name} ${item.topic}`.toLowerCase().includes(query)); $("#plan-count").textContent = state.automations.length;
  if (!state.automations.length) {
    root.innerHTML = `<div class="onboarding"><span class="onboarding-index">01</span><div><p class="section-number">START HERE</p><h3>创建第一条内容生产线</h3><p>选择一个模板，修改主题，然后先在模拟发布模式下跑通全流程。</p></div><div class="onboarding-templates"><button data-template="ai-tools"><b>AI 工具观察</b><span>每日 · 四平台</span></button><button data-template="knowledge"><b>知识解读</b><span>每周 · 长内容</span></button><button data-template="story"><b>轻故事短片</b><span>每日 · 竖屏</span></button></div><button class="primary-button" data-open-blank>从空白创建 <span>→</span></button></div>`;
    return;
  }
  if (!items.length) { root.innerHTML = '<div class="empty-state">没有匹配的计划。清除搜索词后再试。</div>'; return; }
  root.innerHTML = items.map((item) => {
    const latestTask = latestTaskForAutomation(item.id); const isRunning = latestTask && !terminalStatuses.has(latestTask.status); const progress = taskProgress(latestTask); const stage = latestTask ? taskDisplayStage(latestTask) : "尚未运行"; const canRetry = latestTask && failureStatuses.has(latestTask.status);
    return `<article class="automation-card ${item.enabled ? "enabled" : ""} ${isRunning ? "is-running" : ""}"><i class="automation-rail"></i><div class="automation-body"><div class="automation-kicker"><span class="automation-state">${item.enabled ? "● ACTIVE" : "○ PAUSED"}</span><span>每 ${item.interval_minutes} 分钟</span><span>上次 ${formatTime(item.last_run_at)}</span></div><h3>${escapeHtml(item.name)}</h3><p class="automation-topic">${escapeHtml(item.topic)}</p><div class="tag-row">${item.platforms.map((p) => `<span class="tag">${platformNames[p] || p}</span>`).join("")}<span class="tag">${item.video_materials.length ? `${item.video_materials.length} 份兜底素材` : "AI 生成素材"}</span></div>${latestTask ? `<div class="automation-progress ${isRunning ? "is-active" : ""}"><div class="automation-progress-copy"><span>${isRunning ? "正在执行" : "最近一次"} · ${escapeHtml(stage)} <em>${progress.kind}</em></span><b>${progress.value}%</b></div><div class="automation-progress-track" role="progressbar" aria-label="${escapeHtml(item.name)}运行进度" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress.value}"><i style="width:${progress.value}%"></i></div></div>` : ""}</div><div class="automation-actions"><button class="run-button" data-action="${canRetry ? "retry" : "run"}" data-id="${item.id}" ${isRunning ? 'disabled aria-busy="true"' : ""}>${isRunning ? `${escapeHtml(stage)} · ${progress.value}%` : canRetry ? "重新运行 →" : "立即运行 →"}</button><div class="card-menu"><button data-action="edit" data-id="${item.id}">编辑</button><button data-action="duplicate" data-id="${item.id}">复制</button><button data-action="history" data-id="${item.id}">历史</button><button data-action="toggle" data-id="${item.id}" data-enabled="${item.enabled}">${item.enabled ? "暂停" : "启用"}</button><button class="danger-link" data-action="delete" data-id="${item.id}" data-name="${escapeHtml(item.name)}" aria-label="删除计划 ${escapeHtml(item.name)}">删除</button></div></div></article>`;
  }).join("");
}

function statusMatches(status, filter) { if (filter === "all") return true; if (filter === "active") return !terminalStatuses.has(status); if (filter === "failed") return failureStatuses.has(status); return status === filter; }

function renderLedger() {
  const automationMap = Object.fromEntries(state.automations.map((item) => [item.id, item])); const query = $("#ledger-search").value.trim().toLowerCase(); const filter = $("#ledger-status-filter").value;
  const tasks = state.tasks.filter((task) => (!state.ledgerAutomationId || task.automation_id === state.ledgerAutomationId) && statusMatches(task.status, filter) && `${task.title} ${task.topic} ${task.id}`.toLowerCase().includes(query));
  const taskMap = Object.fromEntries(state.tasks.map((task) => [task.id, task]));
  const runs = state.runs.filter((run) => { const task = taskMap[run.task_id]; const name = automationMap[run.automation_id]?.name || ""; return (!state.ledgerAutomationId || run.automation_id === state.ledgerAutomationId) && statusMatches(task?.status || run.status, filter) && `${name} ${run.task_id} ${run.detail}`.toLowerCase().includes(query); });
  if (state.ledgerAutomationId) { const name = automationMap[state.ledgerAutomationId]?.name || "指定计划"; $("#ledger-context").innerHTML = `仅显示「${escapeHtml(name)}」的历史 · <button class="inline-clear" data-clear-history>查看全部</button>`; } else { $("#ledger-context").textContent = "追踪每次运行与内容产物。"; }
  $("#runs-table").innerHTML = runs.length ? runs.map((run) => `<tr><td>${badge(run.status)}</td><td><b>${escapeHtml(automationMap[run.automation_id]?.name || "已删除计划")}</b><br><span class="mono muted">${shortId(run.task_id)}</span></td><td>${formatTime(run.created_at)}</td><td class="muted">${escapeHtml(run.detail || "等待编排器接管")}</td><td><button class="table-action" data-task-id="${run.task_id}">检查任务</button></td></tr>`).join("") : '<tr><td colspan="5"><div class="empty-state compact-empty">没有匹配的运行记录</div></td></tr>';
  $("#tasks-table").innerHTML = tasks.length ? tasks.map((task) => { const progress = taskProgress(task); return `<tr><td>${badge(task.status)}</td><td><b>${escapeHtml(task.title || task.topic)}</b><br><span class="mono muted">${shortId(task.id)}</span></td><td>${task.platforms.map((p) => platformNames[p] || p).join(" · ")}</td><td><div class="table-progress"><div class="progress-track" title="${progress.value}%"><i style="width:${progress.value}%"></i></div><small>${progress.value}% · ${progress.kind}</small></div></td><td>${formatTime(task.updated_at)}</td><td><button class="table-action" data-task-id="${task.id}">详情</button></td></tr>`; }).join("") : '<tr><td colspan="6"><div class="empty-state compact-empty">没有匹配的内容任务</div></td></tr>';
}

function renderSignalBoard() {
  const active = state.tasks.filter((task) => !terminalStatuses.has(task.status)).length; const complete = state.tasks.filter((task) => task.status === "published").length; const failed = state.tasks.filter((task) => failureStatuses.has(task.status)).length;
  $("#active-count").textContent = String(active).padStart(2, "0"); $("#task-count").textContent = state.tasks.length; $("#pending-count").textContent = active; $("#complete-count").textContent = complete; $("#failed-count").textContent = failed; $("#activity-meter").style.width = `${Math.min(100, active * 18)}%`;
  $("#signal-panel").classList.toggle("is-empty", state.tasks.length === 0); $("#signal-summary").textContent = state.tasks.length ? "当前生产线的即时负载。" : "暂无任务，环境已待命。";
}

function renderAttention() { const failed = state.tasks.filter((task) => failureStatuses.has(task.status)); $("#attention-banner").hidden = !failed.length; if (failed.length) $("#attention-copy").textContent = `${failed.length} 个任务失败或未通过审核`; }

function detailStageState(task, index) {
  const current = detailTaskStageIndex(task);
  if (task.status === "cancelled" && index === current) return "is-stopped";
  if (failureStatuses.has(task.status) && index === current) return "is-failed";
  if (task.status === "published" || index < current) return "is-done";
  if (index === current) return "is-current";
  return "";
}

function detailTaskStageIndex(task) {
  if (!["automation_failed", "failed", "cancelled"].includes(task.status)) return taskStageIndex(task.status);
  const eventStage = [...(task.events || [])].reverse().find((event) => timelineStages.includes(event.stage))?.stage;
  if (eventStage) return timelineStages.indexOf(eventStage);
  if (task.audit) return 5;
  if (task.media_path || task.generation_job_id) return 4;
  if (task.audio_path) return 2;
  if (task.content_plan) return 1;
  return 0;
}

function taskTimeline(task) {
  const events = task.events || [];
  return `<ol class="detail-timeline">${timelineStages.map((label, index) => { const related = events.filter((event) => event.stage === label); const latest = related.at(-1); const stateName = detailStageState(task, index); const stateCopy = stateName === "is-done" ? "已完成" : stateName === "is-current" ? "进行中" : stateName === "is-failed" ? "失败" : stateName === "is-stopped" ? "已停止" : "等待"; return `<li class="${stateName}"><i>${stateName === "is-done" ? "✓" : stateName === "is-failed" ? "!" : stateName === "is-stopped" ? "×" : index + 1}</i><span>${label}</span><small>${latest ? formatTime(latest.created_at) : stateCopy}</small></li>`; }).join("")}</ol>`;
}

function currentOperation(task) {
  const retry = retryState(task);
  if (retry.willRetry) return `第 ${retry.attempt}/${retry.max} 次尝试未通过，系统将在下一调度周期携带校验反馈自动修复。`;
  if (task.metadata?.waiting_for_runtime?.detail) return task.metadata.waiting_for_runtime.detail;
  const shots = task.content_plan?.shots || [];
  const complete = shots.filter((shot) => shot.status === "complete").length;
  const lipStageNames = { queued: "等待本机推理资源", preparing: "准备音视频", inference: "MuseTalk 正在推理", quality_check: "检查口型与正脸质量", complete: "口型同步完成", failed: "口型同步失败" };
  const lipStage = lipStageNames[task.metadata?.lip_sync_stage] || "准备口型同步";
  const lipElapsed = Math.max(0, Number(task.metadata?.lip_sync_elapsed_seconds || 0));
  const messages = {
    draft: "正在调用内容模型生成标题、脚本和分镜方案。",
    planned: "内容方案已经就绪，正在检查配音与本地媒体运行时。",
    assets_generating: `正在生成分镜素材，当前已完成 ${complete}/${shots.length || 0} 个。`,
    lip_syncing: `${lipStage}${lipElapsed ? `，已运行 ${formatElapsedSeconds(lipElapsed)}` : ""}。MuseTalk 不提供逐帧进度，完成后将自动进入质量门禁。`,
    composing: "全部分镜已就绪，正在创建成片合成任务。",
    generated: task.media_path ? "成片已经生成，正在执行规则与模型审核。" : `本地视频引擎正在合成，实时进度 ${Math.round(Number(task.metadata?.video_progress || 0))}%。`,
    approved: "审核已经通过，准备进入平台发布门禁。",
    publishing: "正在处理各目标平台的发布任务。",
    published: "流程已经完成。当前为模拟发布，不会操作真实平台账号。",
    review_rejected: "自动审核未通过，请查看审核项后调整内容。",
    partial_failure: "部分平台处理失败，请检查平台结果。",
    automation_failed: "自动编排已达到最大重试次数，请查看错误上下文。",
    cancelled: "任务已停止，不会继续进入后续阶段。",
  };
  return messages[task.status] || "等待编排器更新任务状态。";
}

function pathRow(label, value) {
  return `<div class="artifact-row"><span>${label}</span>${value ? `<code title="${escapeHtml(value)}">${escapeHtml(value)}</code><button type="button" data-copy-value="${escapeHtml(value)}">复制路径</button>` : '<em>尚未生成</em>'}</div>`;
}

function renderMediaPreview(task) {
  if (!task.media_path && !task.cover_path) return '<div class="detail-empty media-preview-empty">成片生成后会在这里直接预览。</div>';
  const coverUrl = task.cover_path ? `/tasks/${encodeURIComponent(task.id)}/cover` : "";
  if (!task.media_path) return `<div class="media-preview is-pending"><div class="media-preview-stage"><img src="${coverUrl}" alt="${escapeHtml(task.title || task.topic)} 封面" loading="lazy" /><span class="media-corner-label">COVER / 9:16</span></div><div class="media-preview-copy"><div class="media-preview-status"><i></i><span>COMPOSING</span></div><h4>封面已生成，成片合成中</h4><p>完成后这里会自动切换为本地播放器，不需要刷新页面。</p></div></div>`;
  const videoUrl = `/tasks/${encodeURIComponent(task.id)}/preview`;
  const shots = task.content_plan?.shots || [];
  const totalDuration = shots.reduce((total, shot) => total + Number(shot.duration_seconds || 0), 0);
  let elapsed = 0;
  const shotNavigation = shots.map((shot) => { const start = elapsed; elapsed += Number(shot.duration_seconds || 0); return `<button type="button" data-preview-seek="${start}" onclick="seekPreviewShot(this)" aria-label="从第 ${shot.order} 个分镜开始播放"><b>${String(shot.order).padStart(2, "0")}</b><span>${escapeHtml(shot.narration)}</span><i style="--shot-weight:${Math.max(1, Number(shot.duration_seconds || 1))}"></i></button>`; }).join("");
  const mediaCheck = task.audit?.checks?.find((check) => check.name === "media");
  return `<div class="media-preview"><div class="media-preview-stage"><video controls preload="metadata" playsinline ${coverUrl ? `poster="${coverUrl}"` : ""}><source src="${videoUrl}" type="video/mp4" />当前浏览器不支持视频预览。</video><span class="media-corner-label">FINAL / 9:16</span><span class="media-local-label"><i></i> LOCAL FILE</span></div><div class="media-preview-copy"><div class="media-preview-status"><i></i><span>FINAL CUT READY</span><em>${state.health?.publish_mode === "dry-run" ? "SAFE PREVIEW" : "LIVE MODE"}</em></div><div><p class="media-preview-kicker">LOCAL REVIEW MONITOR</p><h4>${escapeHtml(task.title || task.topic)}</h4><p>${escapeHtml(task.description || "成片已保存在本机，可在发布前完成最后检查。")}</p></div><dl class="media-preview-metrics"><div><dt>时长</dt><dd data-preview-duration>${totalDuration ? `${totalDuration}S` : "—"}</dd></div><div><dt>分镜</dt><dd>${shots.length || "—"}</dd></div><div><dt>审核</dt><dd>${task.audit?.score ?? "—"}</dd></div><div><dt>平台</dt><dd>${task.platforms.length}</dd></div></dl>${mediaCheck ? `<p class="media-check-line"><i>✓</i>${escapeHtml(mediaCheck.detail)}</p>` : ""}<div class="media-preview-actions"><button type="button" data-preview-play onclick="togglePreviewPlayback(this)"><i>▶</i><span>从头播放</span></button><a href="${videoUrl}" target="_blank" rel="noopener">打开原片 ↗</a></div>${shots.length ? `<div class="media-shot-navigation"><div><span>SHOT TIMELINE</span><small>点击跳转分镜</small></div><div>${shotNavigation}</div></div>` : ""}</div></div>`;
}

function togglePreviewPlayback(button) {
  const video = button.closest(".media-preview")?.querySelector("video"); if (!video) return;
  if (video.paused) { if (video.ended) video.currentTime = 0; video.play().catch(() => showToast("浏览器阻止了自动播放，请使用视频内播放按钮")); } else video.pause();
}

function seekPreviewShot(button) {
  const video = button.closest(".media-preview")?.querySelector("video"); if (!video) return;
  video.currentTime = Number(button.dataset.previewSeek || 0); video.play().catch(() => {});
}

function initializeMediaPreview() {
  const root = $("#detail-assets .media-preview"); const video = root?.querySelector("video");
  if (!root || !video) return;
  const playButton = root.querySelector("[data-preview-play]"); const duration = root.querySelector("[data-preview-duration]"); const shotButtons = [...root.querySelectorAll("[data-preview-seek]")];
  const updatePlayButton = () => { if (!playButton) return; playButton.classList.toggle("is-playing", !video.paused); playButton.querySelector("i").textContent = video.paused ? "▶" : "Ⅱ"; playButton.querySelector("span").textContent = video.paused ? (video.currentTime > .2 ? "继续播放" : "从头播放") : "暂停播放"; };
  const updateShot = () => { const time = video.currentTime; shotButtons.forEach((button, index) => { const start = Number(button.dataset.previewSeek); const next = Number(shotButtons[index + 1]?.dataset.previewSeek ?? Infinity); button.classList.toggle("is-current", time >= start && time < next); }); };
  video.addEventListener("loadedmetadata", () => { if (duration && Number.isFinite(video.duration)) duration.textContent = `${Math.round(video.duration)}S`; });
  video.addEventListener("play", updatePlayButton); video.addEventListener("pause", updatePlayButton); video.addEventListener("timeupdate", updateShot);
  updatePlayButton(); updateShot();
}

function renderEventLog(task) {
  const events = task.events?.length ? task.events : [{ stage: "状态重建", status: task.status, detail: "该任务创建于事件记录功能启用前，以下状态根据现有任务数据重建。", created_at: task.updated_at, inferred: true }];
  return `<div class="event-list">${[...events].reverse().map((event, index) => `<article class="event-row ${event.inferred ? "is-inferred" : ""}"><div class="event-marker"><i></i><span>${String(events.length - index).padStart(2, "0")}</span></div><div><div class="event-meta"><b>${escapeHtml(event.stage)}</b><span>${escapeHtml(statusNames[event.status] || event.status)}</span><time>${formatFullTime(event.created_at)}</time></div><p>${escapeHtml(event.detail)}</p></div></article>`).join("")}</div>`;
}

function renderShotList(task) {
  const shots = task.content_plan?.shots || [];
  if (!shots.length) return '<div class="detail-empty">分镜方案尚未生成。</div>';
  return `<div class="shot-list">${shots.map((shot) => { const requested = shot.presentation_mode || "narration"; const effective = shot.effective_presentation_mode || requested; const fallback = shot.lip_sync_fallback_reason; const quality = shot.lip_sync_score != null ? `同步 ${Math.round(shot.lip_sync_score * 100)} · 正脸 ${Math.round((shot.face_coverage || 0) * 100)}` : ""; return `<article class="shot-card ${requested === "talking_head" ? "is-talking" : ""}"><div class="shot-index"><span>SHOT</span><b>${String(shot.order).padStart(2, "0")}</b></div><div class="shot-body"><div class="shot-head">${badge(shot.status)}<span>${shot.duration_seconds} 秒 · ${shot.kind === "image" ? "图片" : "视频"}</span><span>${escapeHtml(mediaProviderName(shot.provider))}</span></div><div class="shot-mode-row"><b>${escapeHtml(presentationNames[requested] || requested)}</b><span>${effective !== requested ? `实际：${escapeHtml(presentationNames[effective] || effective)}` : quality || "按计划执行"}</span></div><h4>${escapeHtml(shot.narration)}</h4><p>${escapeHtml(shot.visual_prompt)}</p>${fallback ? `<div class="lip-fallback"><b>已安全降级</b><span>${escapeHtml(fallback)}</span></div>` : ""}${shot.audio_path ? `<div class="shot-audio">独立驱动音频 · <code>${escapeHtml(shot.audio_path)}</code></div>` : ""}${shot.negative_prompt ? `<details><summary>负面提示词</summary><p>${escapeHtml(shot.negative_prompt)}</p></details>` : ""}${shot.error || shot.lip_sync_error ? `<div class="inline-error">${escapeHtml(shot.error || shot.lip_sync_error)}</div>` : ""}${shot.media_path ? `<details class="shot-preview"><summary>预览分镜素材</summary>${shot.kind === "image" ? `<img src="/tasks/${encodeURIComponent(task.id)}/shots/${encodeURIComponent(shot.id)}/preview" alt="分镜 ${shot.order}" loading="lazy" />` : `<video controls preload="none" playsinline src="/tasks/${encodeURIComponent(task.id)}/shots/${encodeURIComponent(shot.id)}/preview"></video>`}</details><div class="shot-path"><code>${escapeHtml(shot.media_path)}</code><button type="button" data-copy-value="${escapeHtml(shot.media_path)}">复制</button></div>` : ""}</div></article>`; }).join("")}</div>`;
}

function renderAudit(task) {
  if (!task.audit) return '<div class="detail-empty">尚未进入自动审核阶段。</div>';
  return `<div class="audit-score"><div><span>AUDIT SCORE</span><b>${task.audit.score}</b><small>/ 100</small></div><p>${task.audit.approved ? "所有发布门禁已通过" : "存在未通过的审核项"}<br><small>${formatFullTime(task.audit.reviewed_at)}</small></p></div><div class="check-list">${task.audit.checks.map((check) => `<article class="check-row ${check.passed ? "is-pass" : "is-fail"}"><i>${check.passed ? "✓" : "×"}</i><div><b>${escapeHtml(check.name)}</b><p>${escapeHtml(check.detail)}</p></div><strong>${check.score}</strong></article>`).join("")}</div>`;
}

function renderPublishResults(task) {
  if (!task.publish_results?.length) return `<div class="detail-empty">尚未进入平台处理阶段。发布门禁当前为 <b>${state.health?.publish_mode === "dry-run" ? "模拟发布" : "真实发布"}</b>。</div>`;
  return `<div class="publish-grid">${task.publish_results.map((result) => `<article class="publish-card ${result.success ? "is-success" : "is-failure"}"><div><span>${platformNames[result.platform] || result.platform}</span><b>${result.success ? "处理成功" : "处理失败"}</b></div><p>${escapeHtml(result.detail)}</p><dl><dt>远端标识</dt><dd>${escapeHtml(result.remote_id || "模拟发布，无远端 ID")}</dd><dt>完成时间</dt><dd>${formatFullTime(result.published_at)}</dd></dl></article>`).join("")}</div>`;
}

function openTaskDetail(taskId) {
  const task = state.tasks.find((item) => item.id === taskId); if (!task) { showToast("未找到该任务"); return; }
  const progress = taskProgress(task); const active = !terminalStatuses.has(task.status); const failed = failureStatuses.has(task.status); const retry = retryState(task); const plan = automationForTask(task); const run = state.runs.find((item) => item.task_id === task.id); const shots = task.content_plan?.shots || []; const completedShots = shots.filter((shot) => shot.status === "complete").length;
  $("#detail-modal").dataset.taskId = task.id;
  $("#detail-modal").dataset.taskVersion = `${task.status}:${task.updated_at}`;
  $("#detail-content").innerHTML = `<header class="detail-title"><div><p class="detail-breadcrumb">${escapeHtml(plan?.name || "独立内容任务")} / ${shortId(task.id)}</p><h2>${escapeHtml(task.title || task.topic)}</h2><p>${escapeHtml(task.topic)}</p></div>${badge(task.status)}</header><div class="detail-progress"><div><span>${escapeHtml(taskDisplayStage(task))}</span><b>${progress.value}%</b><em>${progress.kind}</em></div><div class="progress-track" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress.value}"><i style="width:${progress.value}%"></i></div><p>${escapeHtml(currentOperation(task))}</p></div>${taskTimeline(task)}<nav class="detail-jump" aria-label="详情章节"><a href="#detail-events">运行记录</a><a href="#detail-content-package">内容方案</a><a href="#detail-assets">素材与成片</a><a href="#detail-audit">审核</a><a href="#detail-publish">平台结果</a><a href="#detail-debug">调试信息</a></nav><section class="detail-kpis"><article><span>运行时长</span><b>${formatDuration(task.created_at, terminalStatuses.has(task.status) ? task.updated_at : new Date())}</b><small>${formatFullTime(task.created_at)} 开始</small></article><article><span>自动尝试</span><b>${retry.attempt}/${retry.max || "—"}</b><small>${retry.willRetry ? "下一轮将自动重试当前阶段" : "当前阶段运行正常"}</small></article><article><span>分镜素材</span><b>${completedShots}/${shots.length}</b><small>${task.metadata?.cover_status === "complete" ? "封面已就绪" : "封面未完成"}</small></article><article><span>目标平台</span><b>${task.platforms.length}</b><small>${task.platforms.map((p) => platformNames[p] || p).join(" · ")}</small></article></section>${task.automation_error ? `<section class="failure-panel ${retry.willRetry ? "is-retrying" : ""}"><span>${retry.willRetry ? "RETRY SCHEDULED" : "LAST ERROR"}</span><h3>${retry.willRetry ? retryTitle(retry) : "自动编排遇到问题"}</h3><pre>${escapeHtml(task.automation_error)}</pre><small>第 ${retry.attempt}/${retry.max || "—"} 次尝试 · ${retry.willRetry ? "无需手动操作" : "可以点击重新运行"} · 最后更新 ${formatFullTime(task.updated_at)}</small></section>` : ""}<section class="detail-block" id="detail-events"><div class="detail-block-head"><div><span>01 / EVENT STREAM</span><h3>运行记录</h3></div><p>${task.events?.length ? "后端真实记录" : "历史状态重建"}</p></div>${renderEventLog(task)}</section><section class="detail-block" id="detail-content-package"><div class="detail-block-head"><div><span>02 / CONTENT PACKAGE</span><h3>内容方案</h3></div><p>${task.metadata?.llm_generation ? `${escapeHtml(task.metadata.llm_generation.endpoint)} · ${escapeHtml(task.metadata.llm_generation.model)}` : "等待模型调用"}</p></div>${task.content_plan ? `<div class="brief-grid"><article><span>目标受众</span><p>${escapeHtml(task.content_plan.audience)}</p></article><article><span>开场钩子</span><p>${escapeHtml(task.content_plan.hook)}</p></article><article class="span-2"><span>创意方向</span><p>${escapeHtml(task.content_plan.creative_direction)}</p></article><article class="span-2"><span>封面提示词</span><p>${escapeHtml(task.content_plan.cover_prompt)}</p></article></div>` : '<div class="detail-empty">内容模型尚未生成方案。</div>'}${task.description ? `<details class="content-fold" open><summary>发布简介</summary><p>${escapeHtml(task.description)}</p></details>` : ""}${task.script ? `<details class="content-fold"><summary>完整脚本 <small>${task.script.length} 字符</small></summary><pre>${escapeHtml(task.script)}</pre></details>` : ""}${task.tags?.length ? `<div class="detail-tags">${task.tags.map((tag) => `<span>#${escapeHtml(tag)}</span>`).join("")}</div>` : ""}</section><section class="detail-block" id="detail-assets"><div class="detail-block-head"><div><span>03 / MEDIA ASSETS</span><h3>素材与成片</h3></div><p>${completedShots}/${shots.length} 个分镜完成</p></div><div class="artifact-list">${pathRow("配音文件", task.audio_path)}${pathRow("封面文件", task.cover_path)}${pathRow("最终视频", task.media_path)}</div>${renderShotList(task)}</section><section class="detail-block" id="detail-audit"><div class="detail-block-head"><div><span>04 / QUALITY GATE</span><h3>自动审核</h3></div><p>${task.audit ? (task.audit.approved ? "APPROVED" : "REJECTED") : "PENDING"}</p></div>${renderAudit(task)}</section><section class="detail-block" id="detail-publish"><div class="detail-block-head"><div><span>05 / DISTRIBUTION</span><h3>平台处理结果</h3></div><p>${state.health?.publish_mode === "dry-run" ? "DRY-RUN SAFE MODE" : "LIVE MODE"}</p></div>${renderPublishResults(task)}</section><section class="detail-block" id="detail-debug"><div class="detail-block-head"><div><span>06 / DEBUG CONTEXT</span><h3>调试信息</h3></div><p>用于定位任务与底层作业</p></div><dl class="debug-grid"><div><dt>任务 ID</dt><dd>${escapeHtml(task.id)}</dd></div><div><dt>运行 ID</dt><dd>${escapeHtml(task.automation_run_id || run?.id || "—")}</dd></div><div><dt>计划 ID</dt><dd>${escapeHtml(task.automation_id || "—")}</dd></div><div><dt>视频引擎作业</dt><dd>${escapeHtml(task.generation_job_id || "—")}</dd></div><div><dt>模型端点</dt><dd>${escapeHtml(task.metadata?.llm_generation?.endpoint || "—")}</dd></div><div><dt>模型名称</dt><dd>${escapeHtml(task.metadata?.llm_generation?.model || "—")}</dd></div><div><dt>创建时间</dt><dd>${formatFullTime(task.created_at)}</dd></div><div><dt>更新时间</dt><dd>${formatFullTime(task.updated_at)}</dd></div><div class="span-2"><dt>运行说明</dt><dd>${escapeHtml(run?.detail || "等待运行记录更新")}</dd></div></dl></section><div class="detail-actions">${active ? `<button class="danger-outline-button" data-cancel-task="${task.id}">停止流程</button>` : ""}${failed && task.automation_id ? `<button class="primary-button" data-retry-automation="${task.automation_id}">重新运行 <span>→</span></button>` : ""}</div>`;
  const artifactList = $("#detail-assets .artifact-list");
  if (artifactList) artifactList.insertAdjacentHTML("beforebegin", renderMediaPreview(task));
  initializeMediaPreview();
  if (!$("#detail-modal").open) $("#detail-modal").showModal();
}

function setWizardStep(step) {
  state.wizardStep = Math.max(1, Math.min(3, step)); $$(".wizard-step").forEach((panel) => panel.classList.toggle("is-hidden", Number(panel.dataset.step) !== state.wizardStep));
  $$('[data-wizard-to]').forEach((button) => { const value = Number(button.dataset.wizardTo); button.classList.toggle("is-active", value === state.wizardStep); button.classList.toggle("is-done", value < state.wizardStep); });
  $("#wizard-back").hidden = state.wizardStep === 1; $("#wizard-next").hidden = state.wizardStep === 3; $("#save-only-button").hidden = state.wizardStep !== 3; $("#save-run-button").hidden = state.wizardStep !== 3 || Boolean(state.editingAutomationId);
  if (state.wizardStep === 3) renderPlanReview();
}

function validateWizardStep(step) { const fields = [...$(`.wizard-step[data-step="${step}"]`).querySelectorAll("input, textarea, select")]; for (const field of fields) if (!field.reportValidity()) return false; return true; }

function applyTemplate(key) {
  const template = templates[key]; if (!template) return; const form = $("#create-form"); form.elements.name.value = template.name; form.elements.topic.value = template.topic; form.elements.interval_minutes.value = template.interval; form.querySelectorAll('[name="platforms"]').forEach((input) => { input.checked = template.platforms.includes(input.value); });
}

function renderPlanReview() { const form = new FormData($("#create-form")); const platforms = form.getAll("platforms").map((item) => platformNames[item] || item); $("#plan-review").innerHTML = `<span>确认计划</span><h3>${escapeHtml(form.get("name") || "未命名计划")}</h3><p>${escapeHtml(form.get("topic") || "尚未填写内容主题")}</p><div>${platforms.map((item) => `<b>${escapeHtml(item)}</b>`).join("")}<b>${escapeHtml(presentationNames[form.get("presentation_mode")] || "旁白镜头")}</b><b>每 ${escapeHtml(form.get("interval_minutes"))} 分钟</b><b>${form.get("enabled") === "on" ? "定时开启" : "仅手动运行"}</b></div>`; }

function openPlanForm({ automation = null, template = "" } = {}) {
  const form = $("#create-form"); form.reset(); state.editingAutomationId = automation?.id || ""; $("#create-error").textContent = ""; $("#form-kicker").textContent = automation ? "EDIT AUTOMATION" : "NEW AUTOMATION"; $("#form-title").textContent = automation ? "编辑内容计划" : "创建内容计划"; $("#save-only-button").textContent = automation ? "保存修改" : "仅保存";
  if (automation) { form.elements.name.value = automation.name; form.elements.topic.value = automation.topic; form.elements.interval_minutes.value = String(automation.interval_minutes); form.elements.materials.value = automation.video_materials.join("\n"); form.elements.presentation_mode.value = automation.presentation_mode || "narration"; form.elements.enabled.checked = automation.enabled; form.querySelectorAll('[name="platforms"]').forEach((input) => { input.checked = automation.platforms.includes(input.value); }); }
  if (template) applyTemplate(template); setWizardStep(1); $("#create-modal").showModal(); setTimeout(() => form.elements.name.focus(), 60);
}

async function startAutomation(id, button = null) {
  const originalText = button?.textContent; if (button) { button.disabled = true; button.setAttribute("aria-busy", "true"); button.textContent = "正在排队…"; }
  try { const run = await api(`/automations/${id}/run`, { method: "POST" }); showToast(`任务已开始 · ${shortId(run.task_id)}，进度会持续显示`, { persistent: true }); await loadData({ quiet: true }); }
  catch (error) { showToast(error.message.includes("active run") ? "该计划正在运行，已定位到进度面板" : error.message); renderRunDock(); }
  finally { if (button?.isConnected) { button.disabled = false; button.removeAttribute("aria-busy"); button.textContent = originalText; } }
}

async function handleAutomationAction(button) {
  const { action, id, enabled } = button.dataset; const automation = state.automations.find((item) => item.id === id);
  if (action === "delete") { state.pendingAutomationDeleteId = id; $("#delete-plan-name").textContent = button.dataset.name || "该计划"; $("#delete-modal").showModal(); return; }
  if (action === "edit") { openPlanForm({ automation }); return; }
  if (action === "duplicate") { try { await api("/automations", { method: "POST", body: JSON.stringify({ name: `${automation.name}（副本）`, topic: automation.topic, platforms: automation.platforms, video_materials: automation.video_materials, presentation_mode: automation.presentation_mode || "narration", interval_minutes: automation.interval_minutes, enabled: false }) }); await loadData({ quiet: true }); showToast("计划已复制，默认保持暂停"); } catch (error) { showToast(error.message); } return; }
  if (action === "history") { state.ledgerAutomationId = id; state.activeTab = "runs"; $("#ledger-search").value = ""; $("#ledger-status-filter").value = "all"; activateTab("runs"); renderLedger(); $("#ledger").scrollIntoView({ behavior: "smooth", block: "start" }); return; }
  if (["run", "retry"].includes(action)) { await startAutomation(id, button); return; }
  const originalText = button.textContent; button.disabled = true; button.textContent = "更新中…";
  try { if (action === "toggle") { await api(`/automations/${id}/${enabled === "true" ? "disable" : "enable"}`, { method: "POST" }); showToast(enabled === "true" ? "计划已暂停" : "计划已启用"); } await loadData({ quiet: true }); }
  catch (error) { showToast(error.message); } finally { if (button.isConnected) { button.disabled = false; button.textContent = originalText; } }
}

async function confirmAutomationDelete() { const id = state.pendingAutomationDeleteId; if (!id) return; const button = $("#confirm-delete-button"); button.disabled = true; try { await api(`/automations/${id}`, { method: "DELETE" }); state.pendingAutomationDeleteId = ""; $("#delete-modal").close(); await loadData({ quiet: true }); showToast("计划已删除"); } catch (error) { showToast(error.message); } finally { button.disabled = false; } }
async function confirmTaskCancel() { const id = state.pendingCancelTaskId; if (!id) return; const button = $("#confirm-cancel-button"); button.disabled = true; try { await api(`/tasks/${id}/cancel`, { method: "POST" }); state.pendingCancelTaskId = ""; $("#cancel-modal").close(); if ($("#detail-modal").open) $("#detail-modal").close(); await loadData({ quiet: true }); showToast("已停止后续自动编排"); } catch (error) { showToast(error.message); } finally { button.disabled = false; } }
function requestTaskCancel(taskId) { state.pendingCancelTaskId = taskId; $("#cancel-modal").showModal(); }

function activateTab(name) { state.activeTab = name; $$(".tab").forEach((item) => { const active = item.dataset.tab === name; item.classList.toggle("is-active", active); item.setAttribute("aria-selected", String(active)); }); $("#runs-pane").classList.toggle("is-hidden", name !== "runs"); $("#tasks-pane").classList.toggle("is-hidden", name !== "tasks"); }

$("#connect-form").addEventListener("submit", async (event) => { event.preventDefault(); const value = $("#api-key-input").value.trim(); if (!value) { $("#connect-modal").close(); return; } state.apiKey = value; try { await api("/tasks"); persistApiKey(value); $("#api-key-input").value = ""; $("#connect-modal").close(); $("#connect-error").textContent = ""; await loadData({ quiet: true }); showToast("备用连接已保存"); } catch (error) { $("#connect-error").textContent = error.message; } });

$("#create-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (!validateWizardStep(3)) return; const formElement = event.currentTarget; const submitButton = event.submitter; const form = new FormData(formElement); const platforms = form.getAll("platforms"); if (!platforms.length) { $("#create-error").textContent = "至少选择一个平台"; return; }
  const materials = String(form.get("materials") || "").split(/\n+/).map((value) => value.trim()).filter(Boolean); const body = { name: form.get("name"), topic: form.get("topic"), platforms, video_materials: materials, presentation_mode: form.get("presentation_mode") || "narration", interval_minutes: Number(form.get("interval_minutes")), enabled: form.get("enabled") === "on" };
  submitButton.disabled = true; $("#create-error").textContent = "";
  try { const automation = state.editingAutomationId ? await api(`/automations/${state.editingAutomationId}`, { method: "PUT", body: JSON.stringify(body) }) : await api("/automations", { method: "POST", body: JSON.stringify(body) }); const shouldRun = submitButton.dataset.submitMode === "run" && !state.editingAutomationId; formElement.reset(); $("#create-modal").close(); state.editingAutomationId = ""; await loadData({ quiet: true }); showToast(shouldRun ? "计划已保存，正在启动首次运行" : "计划已保存"); if (shouldRun) await startAutomation(automation.id); }
  catch (error) { $("#create-error").textContent = error.message; } finally { submitButton.disabled = false; }
});

document.addEventListener("click", async (event) => {
  const action = event.target.closest("[data-action]"); if (action) handleAutomationAction(action);
  const task = event.target.closest("[data-task-id]"); if (task) openTaskDetail(task.dataset.taskId);
  const retry = event.target.closest("[data-retry-automation]"); if (retry) { const taskId = state.selectedTaskId; retry.disabled = true; try { await api(`/tasks/${taskId}/retry`, { method: "POST" }); $("#detail-modal").close(); await loadData({ quiet: true }); showToast("已从失败阶段恢复，编排器将自动续跑"); } catch (error) { showToast(error.message); } finally { retry.disabled = false; } }
  const cancel = event.target.closest("[data-cancel-task]"); if (cancel) requestTaskCancel(cancel.dataset.cancelTask);
  const copy = event.target.closest("[data-copy-value]"); if (copy) navigator.clipboard.writeText(copy.dataset.copyValue).then(() => showToast("已复制")).catch(() => showToast("复制失败，请手动选择"));
  const tab = event.target.closest("[data-tab]"); if (tab) activateTab(tab.dataset.tab);
  const template = event.target.closest("[data-template]"); if (template && !$("#create-modal").open) openPlanForm({ template: template.dataset.template }); else if (template) applyTemplate(template.dataset.template);
  if (event.target.closest("[data-open-blank]")) openPlanForm();
  if (event.target.closest("[data-close-modal]")) $("#create-modal").close(); if (event.target.closest("[data-close-detail]")) $("#detail-modal").close(); if (event.target.closest("[data-close-connect]")) $("#connect-modal").close();
  if (event.target.closest("[data-close-delete]")) { state.pendingAutomationDeleteId = ""; $("#delete-modal").close(); }
  if (event.target.closest("[data-close-cancel]")) { state.pendingCancelTaskId = ""; $("#cancel-modal").close(); }
  if (event.target.closest("[data-clear-history]")) { state.ledgerAutomationId = ""; renderLedger(); }
});

$("#wizard-next").addEventListener("click", () => { if (validateWizardStep(state.wizardStep)) setWizardStep(state.wizardStep + 1); }); $("#wizard-back").addEventListener("click", () => setWizardStep(state.wizardStep - 1));
$$('[data-wizard-to]').forEach((button) => button.addEventListener("click", () => { const target = Number(button.dataset.wizardTo); if (target < state.wizardStep || validateWizardStep(state.wizardStep)) setWizardStep(target); }));
$("#open-create-button").addEventListener("click", () => openPlanForm()); $("#hero-create-button").addEventListener("click", () => openPlanForm());
$("#connect-settings-button").addEventListener("click", () => showConnect()); $("#confirm-delete-button").addEventListener("click", confirmAutomationDelete); $("#confirm-cancel-button").addEventListener("click", confirmTaskCancel);
$("#open-runtime-button").addEventListener("click", () => showConnect());
$("#cancel-active-button").addEventListener("click", () => requestTaskCancel($("#run-dock").dataset.taskId)); $("#run-dock-detail").addEventListener("click", () => openTaskDetail($("#run-dock").dataset.taskId));
$("#refresh-button").addEventListener("click", () => loadData()); $("#view-attention-button").addEventListener("click", () => { $("#ledger-status-filter").value = "failed"; activateTab("tasks"); renderLedger(); $("#ledger").scrollIntoView({ behavior: "smooth" }); });
$("#plan-search").addEventListener("input", renderAutomations); $("#ledger-search").addEventListener("input", renderLedger); $("#ledger-status-filter").addEventListener("change", renderLedger); $("#clear-ledger-filter").addEventListener("click", () => { $("#ledger-search").value = ""; $("#ledger-status-filter").value = "all"; state.ledgerAutomationId = ""; renderLedger(); });
$("#clear-api-key-button").addEventListener("click", () => { clearStoredApiKey(); state.apiKey = ""; $("#clear-api-key-button").hidden = true; $("#connect-error").textContent = "已清除备用密钥，页面仍会尝试自动连接。"; });
$$('.modal').forEach((modal) => modal.addEventListener("click", (event) => { if (event.target === modal) modal.close(); })); $("#delete-modal").addEventListener("close", () => { state.pendingAutomationDeleteId = ""; }); $("#cancel-modal").addEventListener("close", () => { state.pendingCancelTaskId = ""; });

function updateClock() { $("#local-clock").textContent = `${new Date().toLocaleTimeString("zh-CN", { hour12: false })} CST`; const dock = $("#run-dock"); if (!dock.hidden) { const task = state.tasks.find((item) => item.id === dock.dataset.taskId); if (task) $("#run-dock-elapsed").textContent = formatElapsed(task.created_at); } }
updateClock(); setInterval(updateClock, 1000); setInterval(() => { if (!document.hidden) loadData({ quiet: true }); }, 5000); loadData({ quiet: true });
