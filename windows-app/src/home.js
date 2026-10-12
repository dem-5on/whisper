import { invoke } from "@tauri-apps/api/core";
import { emit } from "@tauri-apps/api/event";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import "./home.css";

const SOCKET_URL = "ws://127.0.0.1:47651";
const ui = {
  title: document.querySelector("#page-title"),
  service: document.querySelector("#service-state"),
  feedback: document.querySelector("#feedback"),
  pages: [...document.querySelectorAll(".page")],
  nav: [...document.querySelectorAll("[data-page]")],
  sessionList: document.querySelector("#session-list"),
  sessionDetail: document.querySelector("#session-detail"),
  historySearch: document.querySelector("#history-search"),
  termList: document.querySelector("#term-list"),
  termCount: document.querySelector("#term-count"),
  consent: document.querySelector("#consent-dialog"),
  consentCheck: document.querySelector("#consent-confirm"),
  confirmSession: document.querySelector("#confirm-session"),
  hidePill: document.querySelector("#setting-hide-pill"),
  rewrite: document.querySelector("#setting-rewrite"),
};

let socket;
let sequence = 0;
let retryDelay = 500;
let currentStatus = {};
let historyFilter = "all";
let selectedSessionId = "";
let historyTimer;
let pending = new Map();
let pendingMode = "";
let busy = false;

function message(text = "", error = false) {
  ui.feedback.textContent = text;
  ui.feedback.dataset.error = String(error);
}

function request(command, params = {}, timeoutMs = 15000) {
  if (!socket || socket.readyState !== WebSocket.OPEN) return Promise.reject(new Error("Wicx is reconnecting. Please try again in a moment."));
  const requestId = ++sequence;
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      pending.delete(requestId);
      reject(new Error("Wicx did not respond in time."));
    }, timeoutMs);
    pending.set(requestId, { resolve, reject, timeout });
    socket.send(JSON.stringify({ command, request_id: requestId, ...params }));
  });
}

function updateStatus(status) {
  if (!status || typeof status !== "object") return;
  currentStatus = { ...currentStatus, ...status };
  const connected = socket?.readyState === WebSocket.OPEN;
  ui.service.dataset.connected = String(connected);
  ui.service.innerHTML = `<i></i>${connected ? "Service ready" : "Reconnecting"}`;
  const recording = currentStatus.state === "RECORDING";
  busy = recording || ["PROCESSING", "DELIVERING"].includes(currentStatus.state);
  ui.hidePill.checked = currentStatus.hide_recording_pill === true;
  ui.rewrite.checked = currentStatus.rewrite_enabled === true;
  document.querySelectorAll("[data-session-mode], #start-dictation").forEach((button) => { button.disabled = busy || !connected; });
  if (currentStatus.state === "ERROR" && currentStatus.error) message(currentStatus.error, true);
}

async function connect() {
  try {
    let url = SOCKET_URL;
    try {
      const token = await invoke("websocket_token");
      if (token) url += `?token=${encodeURIComponent(token)}`;
    } catch { /* Development daemon does not require the packaged token. */ }
    socket = new WebSocket(url);
    socket.addEventListener("open", () => {
      retryDelay = 500;
      request("status").then(updateStatus).catch(() => {});
      request("subscribe", { since: 0 }).catch(() => {});
    });
    socket.addEventListener("message", ({ data }) => {
      let result;
      try { result = JSON.parse(data); } catch { return; }
      if (result.request_id != null) {
        const waiting = pending.get(result.request_id);
        if (waiting) {
          pending.delete(result.request_id);
          clearTimeout(waiting.timeout);
          if (result.ok === "false") waiting.reject(new Error(result.error || "Wicx request failed."));
          else waiting.resolve(result);
        }
      }
      if (result.type === "status") updateStatus(result.status);
      else if (result.state) updateStatus(result);
    });
    socket.addEventListener("close", () => {
      for (const waiting of pending.values()) {
        clearTimeout(waiting.timeout);
        waiting.reject(new Error("Wicx disconnected."));
      }
      pending.clear();
      updateStatus({ state: currentStatus.state });
      window.setTimeout(connect, retryDelay);
      retryDelay = Math.min(5000, retryDelay * 1.7);
    });
    socket.addEventListener("error", () => socket.close());
  } catch {
    window.setTimeout(connect, retryDelay);
  }
}

function openPage(page) {
  const names = { home: "Home", history: "History", dictionary: "Dictionary", settings: "Settings" };
  ui.title.textContent = names[page] || "Home";
  ui.pages.forEach((element) => {
    const active = element.id === `page-${page}`;
    element.hidden = !active;
    element.classList.toggle("active", active);
  });
  document.querySelectorAll(".nav-item[data-page]").forEach((button) => button.classList.toggle("active", button.dataset.page === page));
  if (page === "history") void loadSessions();
  if (page === "dictionary") void loadDictionary();
}

async function loadSessions() {
  ui.sessionList.innerHTML = '<div class="empty-state">Loading your recordings…</div>';
  try {
    const response = await request("sessions", {
      limit: 100,
      offset: 0,
      mode: historyFilter === "all" ? undefined : historyFilter,
      search: ui.historySearch.value.trim() || undefined,
    });
    const sessions = Array.isArray(response.sessions) ? response.sessions : [];
    ui.sessionList.replaceChildren();
    if (!sessions.length) {
      ui.sessionList.innerHTML = '<div class="empty-state">No recordings here yet.<br />Your sessions will appear after you finish recording.</div>';
      showSessionPlaceholder();
      return;
    }
    for (const session of sessions) {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `session-row${session.id === selectedSessionId ? " selected" : ""}`;
      row.dataset.sessionId = session.id;
      row.addEventListener("click", () => void loadSession(session.id));
      const title = document.createElement("strong");
      title.textContent = `${session.mode[0].toUpperCase()}${session.mode.slice(1)} · ${new Date(session.created_at).toLocaleString()}`;
      const meta = document.createElement("small");
      meta.textContent = `${session.state} · ${session.audio_available ? "Audio saved" : "No audio"}`;
      const preview = document.createElement("p");
      preview.textContent = session.source_transcript || "Transcript not available yet.";
      row.append(title, meta, preview);
      ui.sessionList.append(row);
    }
    if (selectedSessionId && sessions.some((session) => session.id === selectedSessionId)) await loadSession(selectedSessionId);
    else showSessionPlaceholder();
  } catch (error) {
    ui.sessionList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function showSessionPlaceholder() {
  ui.sessionDetail.innerHTML = '<div class="detail-placeholder"><span>◷</span><h3>Select a recording</h3><p>Choose an item to see its transcript and session details.</p></div>';
}

async function loadSession(id) {
  try {
    selectedSessionId = id;
    const { session } = await request("get_session", { session_id: id });
    ui.sessionDetail.replaceChildren();
    const header = document.createElement("header");
    header.className = "detail-head";
    const info = document.createElement("div");
    const heading = document.createElement("h3");
    heading.textContent = `${session.mode[0].toUpperCase()}${session.mode.slice(1)} session`;
    const date = document.createElement("p");
    date.textContent = `${new Date(session.created_at).toLocaleString()} · ${session.state}`;
    info.append(heading, date);
    const actions = document.createElement("div");
    actions.className = "detail-actions";
    const copy = document.createElement("button");
    copy.className = "small-button";
    copy.textContent = "Copy transcript";
    copy.disabled = !session.source_transcript;
    copy.addEventListener("click", async () => {
      try { await writeText(session.source_transcript || ""); message("Transcript copied."); }
      catch { message("Could not copy the transcript.", true); }
    });
    const remove = document.createElement("button");
    remove.className = "small-button danger";
    remove.textContent = "Delete";
    remove.addEventListener("click", async () => {
      if (!window.confirm("Delete this recording, transcript, and saved audio from this device? This cannot be undone.")) return;
      try {
        await request("delete_session", { session_id: id });
        selectedSessionId = "";
        message("Recording deleted from this device.");
        await loadSessions();
      } catch (error) { message(error.message, true); }
    });
    actions.append(copy, remove);
    header.append(info, actions);
    const transcript = document.createElement("div");
    transcript.className = "transcript-body";
    transcript.textContent = session.source_transcript || "No transcript is available for this recording.";
    ui.sessionDetail.append(header, transcript);
    document.querySelectorAll(".session-row").forEach((row) => row.classList.toggle("selected", row.dataset.sessionId === id));
  } catch (error) { message(error.message, true); }
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = value;
  return element.innerHTML;
}

async function loadDictionary() {
  ui.termList.innerHTML = '<div class="empty-state">Loading your dictionary…</div>';
  try { renderTerms((await request("dictionary", { action: "list" })).terms || []); }
  catch (error) { ui.termList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`; }
}

function renderTerms(terms) {
  ui.termCount.textContent = `${terms.length} ${terms.length === 1 ? "term" : "terms"}`;
  ui.termList.replaceChildren();
  if (!terms.length) {
    ui.termList.innerHTML = '<div class="empty-state">Your dictionary is empty. Add a name or technical term above.</div>';
    return;
  }
  terms.forEach((term) => {
    const row = document.createElement("div");
    row.className = "term-row";
    const label = document.createElement("span");
    label.textContent = term;
    const remove = document.createElement("button");
    remove.type = "button";
    remove.textContent = "Remove";
    remove.addEventListener("click", async () => {
      try { renderTerms((await request("dictionary", { action: "remove", term })).terms || []); message(`Removed “${term}”.`); }
      catch (error) { message(error.message, true); }
    });
    row.append(label, remove);
    ui.termList.append(row);
  });
}

async function startDictation() {
  try { await emit("recording-hotkey"); }
  catch (error) { message(error.message, true); }
}

async function beginSession(mode) {
  pendingMode = mode;
  ui.consentCheck.checked = false;
  ui.confirmSession.disabled = true;
  document.querySelector("#consent-title").textContent = mode === "lecture" ? "Start a lecture" : "Start a meeting";
  document.querySelector("#consent-copy").textContent = mode === "lecture"
    ? "Please let everyone in the room know that the lecture will be recorded."
    : "Please let everyone know that the conversation will be recorded.";
  ui.consent.showModal();
}

document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => openPage(button.dataset.page)));
document.querySelector("#start-dictation").addEventListener("click", () => void startDictation());
document.querySelectorAll("[data-session-mode]").forEach((button) => button.addEventListener("click", () => beginSession(button.dataset.sessionMode)));
ui.consentCheck.addEventListener("change", () => { ui.confirmSession.disabled = !ui.consentCheck.checked; });
ui.confirmSession.addEventListener("click", async (event) => {
  event.preventDefault();
  if (!ui.consentCheck.checked || !pendingMode) return;
  ui.confirmSession.disabled = true;
  try {
    updateStatus(await request("start_session", { mode: pendingMode, consent_acknowledged: true }));
    ui.consent.close();
    message(`${pendingMode === "lecture" ? "Lecture" : "Meeting"} recording started. Use the check button in the status pill to finish.`);
  } catch (error) { message(error.message, true); }
  finally { ui.confirmSession.disabled = !ui.consentCheck.checked; }
});
document.querySelector("#add-term-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#new-term");
  try {
    const response = await request("dictionary", { action: "add", term: input.value });
    renderTerms(response.terms || []);
    message(`Added “${input.value.trim()}” to your dictionary.`);
    input.value = "";
  } catch (error) { message(error.message, true); }
});
document.querySelectorAll("[data-history-filter]").forEach((button) => button.addEventListener("click", () => {
  historyFilter = button.dataset.historyFilter;
  document.querySelectorAll("[data-history-filter]").forEach((chip) => chip.classList.toggle("selected", chip === button));
  void loadSessions();
}));
ui.historySearch.addEventListener("input", () => {
  window.clearTimeout(historyTimer);
  historyTimer = window.setTimeout(() => void loadSessions(), 250);
});
document.querySelectorAll("[data-settings-section]").forEach((button) => button.addEventListener("click", () => {
  document.querySelectorAll("[data-settings-section]").forEach((item) => item.classList.toggle("selected", item === button));
  document.querySelectorAll("[data-settings-panel]").forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== button.dataset.settingsSection; });
}));
ui.hidePill.addEventListener("change", async () => {
  ui.hidePill.disabled = true;
  try { updateStatus(await request("set_recording_pill_hidden", { hidden: ui.hidePill.checked })); message(ui.hidePill.checked ? "The recording pill will be hidden." : "The recording pill will be shown during active work."); }
  catch (error) { ui.hidePill.checked = currentStatus.hide_recording_pill === true; message(error.message, true); }
  finally { ui.hidePill.disabled = false; }
});
ui.rewrite.addEventListener("change", async () => {
  ui.rewrite.disabled = true;
  try { updateStatus(await request("set_rewrite", { enabled: ui.rewrite.checked })); message(ui.rewrite.checked ? "Rewrite enabled." : "Rewrite disabled."); }
  catch (error) { ui.rewrite.checked = currentStatus.rewrite_enabled === true; message(error.message, true); }
  finally { ui.rewrite.disabled = false; }
});

async function openAccountSettings() {
  try { await invoke("show_key_settings"); }
  catch (error) { message(error.message, true); }
}
document.querySelector("#account-shortcut").addEventListener("click", openAccountSettings);
document.querySelector("#show-key-settings").addEventListener("click", openAccountSettings);
document.querySelector("#account-settings-button").addEventListener("click", openAccountSettings);
document.querySelector("#open-tray-panel").addEventListener("click", async () => {
  try { await invoke("show_compact_panel"); }
  catch (error) { message(error.message, true); }
});

connect();
