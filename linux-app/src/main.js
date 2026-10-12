import { invoke } from "@tauri-apps/api/core";
import { getCurrentWindow } from "@tauri-apps/api/window";
import "./style.css";

const preview = !window.__TAURI_INTERNALS__;
const ui = {
  connection: document.querySelector("#service-status"),
  feedback: document.querySelector("#feedback"),
  pages: [...document.querySelectorAll(".page")],
  nav: [...document.querySelectorAll("[data-page]")],
  sessionList: document.querySelector("#session-list"),
  sessionDetail: document.querySelector("#session-detail"),
  historySearch: document.querySelector("#history-search"),
  termList: document.querySelector("#term-list"),
  termCount: document.querySelector("#term-count"),
  recentSessions: document.querySelector("#recent-sessions"),
  consent: document.querySelector("#consent-dialog"),
  consentCheck: document.querySelector("#consent-confirm"),
  confirmSession: document.querySelector("#confirm-session"),
  hidePill: document.querySelector("#setting-hide-pill"),
  rewrite: document.querySelector("#setting-rewrite"),
  activeBanner: document.querySelector("#active-session-banner"),
  micButton: document.querySelector("#choose-microphone"),
  micDialog: document.querySelector("#mic-dialog"),
  micOptions: document.querySelector("#microphone-options"),
};

const mockSessions = [
  { id: "preview-lecture", mode: "lecture", state: "complete", created_at: new Date(Date.now() - 86400000).toISOString(), source_transcript: "Today we discussed the foundations of distributed systems, including consistency, availability, and how services communicate across a network.", audio_available: true },
  { id: "preview-dictation", mode: "dictation", state: "complete", created_at: new Date(Date.now() - 172800000).toISOString(), source_transcript: "Remember to send the project notes to the team before the end of the day.", audio_available: false },
];
let mockTerms = ["TypeScript", "Twi", "Wicx"];
let mockStatus = { state: "IDLE", backend: "local", model: "base", mic: "default", rewrite_enabled: false, hide_recording_pill: false };
let historyFilter = "all";
let selectedSessionId = "";
let historyTimer;
let pendingMode = "";
let availableMics = [];

function notify(text = "", error = false) {
  ui.feedback.textContent = text;
  ui.feedback.dataset.error = String(error);
}

async function request(command, params = {}) {
  if (!preview) return invoke("daemon_request", { command, params });
  await new Promise((resolve) => window.setTimeout(resolve, 90));
  if (command === "status") return { ...mockStatus, ok: "true" };
  if (command === "sessions") {
    const mode = params.mode;
    const query = String(params.search || "").toLowerCase();
    const sessions = mockSessions.filter((session) => (!mode || session.mode === mode) && (!query || session.source_transcript.toLowerCase().includes(query)));
    return { sessions, ok: "true" };
  }
  if (command === "get_session") {
    const session = mockSessions.find((item) => item.id === params.session_id);
    if (!session) throw new Error("Recording not found.");
    return { session, ok: "true" };
  }
  if (command === "delete_session") {
    mockSessions.splice(mockSessions.findIndex((item) => item.id === params.session_id), 1);
    return { ok: "true" };
  }
  if (command === "dictionary") {
    if (params.action === "add" && !mockTerms.includes(params.term)) mockTerms.push(params.term);
    if (params.action === "remove") mockTerms = mockTerms.filter((term) => term !== params.term);
    return { terms: [...mockTerms], ok: "true" };
  }
  if (command === "mics") return { mics: [{ id: "default", name: "System default", default: true }], ok: "true" };
  if (command === "set_mic") {
    mockStatus.mic = params.device || "default";
    return { ...mockStatus, ok: "true" };
  }
  if (command === "set_rewrite") mockStatus.rewrite_enabled = params.enabled;
  if (command === "set_recording_pill_hidden") mockStatus.hide_recording_pill = params.hidden;
  if (command === "toggle" && mockStatus.state === "RECORDING") {
    mockStatus.state = "IDLE";
    return { ...mockStatus, ok: "true" };
  }
  if (command === "toggle" || command === "start_session") {
    mockStatus.state = "RECORDING";
    mockStatus.session_mode = command === "toggle" ? "dictation" : params.mode;
    return { ...mockStatus, ok: "true" };
  }
  if (command === "stop_session" || command === "cancel") {
    mockStatus.state = "IDLE";
    return { ...mockStatus, ok: "true" };
  }
  return { ...mockStatus, ok: "true" };
}

function updateStatus(status) {
  if (!status || typeof status !== "object") return;
  mockStatus = { ...mockStatus, ...status };
  const ready = status.ok !== "false";
  ui.connection.dataset.ready = String(ready);
  ui.connection.querySelector("span").textContent = preview ? "Preview" : ready ? "Ready" : "Offline";
  const busy = ["RECORDING", "PROCESSING", "DELIVERING"].includes(status.state);
  const activeMode = status.session_mode || mockStatus.session_mode || "dictation";
  ui.activeBanner.hidden = !busy;
  if (busy) {
    const processing = status.state !== "RECORDING";
    const modeName = activeMode[0].toUpperCase() + activeMode.slice(1);
    document.querySelector("#active-session-title").textContent = processing ? `Finishing your ${activeMode}` : `${modeName} in progress`;
    document.querySelector("#active-session-description").textContent = processing ? (status.rewrite_state ? "Rewriting your transcript…" : "Transcribing your audio…") : "Recording is saved on this device.";
    document.querySelector("#cancel-active-session").hidden = processing;
    document.querySelector("#finish-active-session").hidden = processing;
  } else {
    mockStatus.session_mode = "";
  }
  ui.hidePill.checked = status.hide_recording_pill === true;
  ui.rewrite.checked = status.rewrite_enabled === true;
  document.querySelector("#model-name").textContent = `${status.backend === "local" ? "Local" : status.backend || "Local"} · ${status.effective_model || status.model || "base"}`;
  const activeMic = availableMics.find((mic) => mic.id === status.mic);
  document.querySelector("#mic-name").textContent = activeMic?.name || (status.mic && status.mic !== "default" ? status.mic : "System default");
  ui.micButton.disabled = busy;
  document.querySelectorAll("[data-session-mode], #start-dictation, #mode-dictation").forEach((button) => { button.disabled = busy || (!ready && !preview); });
  if (status.state === "ERROR" && status.error) notify(status.error, true);
}

async function refreshStatus() {
  try { updateStatus(await request("status")); }
  catch (error) {
    ui.connection.dataset.ready = "false";
    ui.connection.querySelector("span").textContent = preview ? "Preview" : "Offline";
    if (!preview) notify(error.message, true);
  }
}

function openPage(page) {
  ui.pages.forEach((element) => {
    const active = element.id === `page-${page}`;
    element.hidden = !active;
    element.classList.toggle("active", active);
  });
  document.querySelectorAll(".nav-item[data-page]").forEach((button) => button.classList.toggle("selected", button.dataset.page === page));
  if (page === "history") void loadSessions();
  if (page === "dictionary") void loadDictionary();
}

async function refreshMicrophones() {
  const response = await request("mics");
  if (response.ok === "false") throw new Error(response.error || "Could not list microphones.");
  availableMics = Array.isArray(response.mics) ? response.mics : [];
  const activeMic = availableMics.find((mic) => mic.id === mockStatus.mic);
  document.querySelector("#mic-name").textContent = activeMic?.name || (mockStatus.mic && mockStatus.mic !== "default" ? mockStatus.mic : "System default");
  return availableMics;
}

async function openMicrophonePicker() {
  ui.micDialog.showModal();
  ui.micOptions.innerHTML = '<div class="empty-state">Loading microphones…</div>';
  try {
    await refreshMicrophones();
    ui.micOptions.replaceChildren();
    const currentDevice = mockStatus.mic && mockStatus.mic !== "default" ? mockStatus.mic : "default";
    const choices = [
      { id: "default", name: "System default", default: currentDevice === "default" },
      ...availableMics.filter((mic) => mic.id !== "default"),
    ];
    if (choices.length === 1) {
      const note = document.createElement("p");
      note.className = "mic-empty-note";
      note.textContent = "No other input devices were found.";
      ui.micOptions.append(note);
    }
    choices.forEach((mic) => {
      const choice = document.createElement("button");
      choice.type = "button";
      choice.className = "mic-option";
      choice.setAttribute("aria-pressed", String(mic.id === currentDevice));
      const copy = document.createElement("span");
      const name = document.createElement("strong");
      const detail = document.createElement("small");
      name.textContent = mic.name;
      detail.textContent = mic.id === "default" ? "Let the system choose" : mic.default ? "System default input" : "Input device";
      copy.append(name, detail);
      const selected = document.createElement("span");
      selected.className = "mic-selected";
      selected.setAttribute("aria-hidden", "true");
      selected.textContent = mic.id === currentDevice ? "✓" : "";
      choice.append(copy, selected);
      choice.addEventListener("click", async () => {
        choice.disabled = true;
        try {
          const response = await request("set_mic", { device: mic.id });
          if (response.ok === "false") throw new Error(response.error || "Could not change microphone.");
          updateStatus(response);
          ui.micDialog.close();
          notify(`Microphone set to ${mic.name}.`);
        } catch (error) {
          notify(error.message, true);
          choice.disabled = false;
        }
      });
      ui.micOptions.append(choice);
    });
  } catch (error) {
    ui.micOptions.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function escapeHtml(value) {
  const element = document.createElement("span");
  element.textContent = value;
  return element.innerHTML;
}

function showSessionPlaceholder() {
  ui.sessionDetail.innerHTML = '<div class="detail-placeholder"><span>◷</span><h3>Choose a recording</h3><p>Select a session to read its transcript and details.</p></div>';
}

async function loadSessions() {
  ui.sessionList.innerHTML = '<div class="empty-state">Loading recordings…</div>';
  try {
    const response = await request("sessions", { limit: 100, offset: 0, mode: historyFilter === "all" ? undefined : historyFilter, search: ui.historySearch.value.trim() || undefined });
    const sessions = Array.isArray(response.sessions) ? response.sessions : [];
    ui.sessionList.replaceChildren();
    if (!sessions.length) {
      ui.sessionList.innerHTML = '<div class="empty-state">No recordings yet. Finished sessions will appear here.</div>';
      showSessionPlaceholder();
      return;
    }
    sessions.forEach((session) => {
      const row = document.createElement("button");
      row.type = "button";
      row.className = `session-row${session.id === selectedSessionId ? " selected" : ""}`;
      row.dataset.sessionId = session.id;
      row.addEventListener("click", () => void loadSession(session.id));
      const title = document.createElement("strong");
      title.textContent = `${session.mode[0].toUpperCase()}${session.mode.slice(1)} · ${new Date(session.created_at).toLocaleString()}`;
      const meta = document.createElement("small");
      meta.textContent = `${session.state} · ${session.audio_available ? "Audio saved" : "No audio"}`;
      const previewText = document.createElement("p");
      previewText.textContent = session.source_transcript || "Transcript not available yet.";
      row.append(title, meta, previewText);
      ui.sessionList.append(row);
    });
    renderRecent(sessions.slice(0, 3));
    if (selectedSessionId && sessions.some((session) => session.id === selectedSessionId)) await loadSession(selectedSessionId);
    else showSessionPlaceholder();
  } catch (error) {
    ui.sessionList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function renderRecent(sessions) {
  ui.recentSessions.replaceChildren();
  if (!sessions.length) {
    ui.recentSessions.innerHTML = '<div class="recent-empty">Your recordings will show up here.</div>';
    return;
  }
  sessions.forEach((session) => {
    const row = document.createElement("button");
    row.className = "recent-row";
    row.type = "button";
    row.innerHTML = `<span class="recent-type">${session.mode === "lecture" ? "▤" : session.mode === "meeting" ? "◉" : "♪"}</span><span><strong>${session.mode[0].toUpperCase()}${session.mode.slice(1)}</strong><small>${new Date(session.created_at).toLocaleDateString()}</small></span>`;
    row.addEventListener("click", () => openPage("history"));
    ui.recentSessions.append(row);
  });
}

async function loadSession(id) {
  try {
    selectedSessionId = id;
    const { session } = await request("get_session", { session_id: id });
    ui.sessionDetail.replaceChildren();
    const header = document.createElement("header");
    header.className = "detail-head";
    const info = document.createElement("div");
    const title = document.createElement("h3");
    title.textContent = `${session.mode[0].toUpperCase()}${session.mode.slice(1)} session`;
    const date = document.createElement("p");
    date.textContent = `${new Date(session.created_at).toLocaleString()} · ${session.state}`;
    info.append(title, date);
    const actions = document.createElement("div");
    actions.className = "detail-actions";
    const copy = document.createElement("button");
    copy.className = "small-button";
    copy.textContent = "Copy transcript";
    copy.disabled = !session.source_transcript;
    copy.addEventListener("click", async () => {
      try {
        if (navigator.clipboard?.writeText) await navigator.clipboard.writeText(session.source_transcript || "");
        else throw new Error("Clipboard is unavailable.");
        notify("Transcript copied.");
      } catch { notify("Could not copy the transcript.", true); }
    });
    const remove = document.createElement("button");
    remove.className = "small-button danger";
    remove.textContent = "Delete";
    remove.addEventListener("click", async () => {
      if (!window.confirm("Delete this recording, transcript, and saved audio from this device? This cannot be undone.")) return;
      try { await request("delete_session", { session_id: id }); selectedSessionId = ""; notify("Recording deleted from this device."); await loadSessions(); }
      catch (error) { notify(error.message, true); }
    });
    actions.append(copy, remove);
    header.append(info, actions);
    const transcript = document.createElement("div");
    transcript.className = "transcript-body";
    transcript.textContent = session.source_transcript || "No transcript is available for this recording.";
    ui.sessionDetail.append(header, transcript);
    document.querySelectorAll(".session-row").forEach((row) => row.classList.toggle("selected", row.dataset.sessionId === id));
  } catch (error) { notify(error.message, true); }
}

async function loadDictionary() {
  ui.termList.innerHTML = '<div class="empty-state">Loading your words…</div>';
  try { renderTerms((await request("dictionary", { action: "list" })).terms || []); }
  catch (error) { ui.termList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`; }
}

function renderTerms(terms) {
  ui.termCount.textContent = `${terms.length} ${terms.length === 1 ? "term" : "terms"}`;
  ui.termList.replaceChildren();
  if (!terms.length) {
    ui.termList.innerHTML = '<div class="empty-state">Your dictionary is empty. Add a name or specialist term above.</div>';
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
      try { renderTerms((await request("dictionary", { action: "remove", term })).terms || []); notify(`Removed “${term}”.`); }
      catch (error) { notify(error.message, true); }
    });
    row.append(label, remove);
    ui.termList.append(row);
  });
}

function beginSession(mode) {
  pendingMode = mode;
  ui.consentCheck.checked = false;
  ui.confirmSession.disabled = true;
  document.querySelector("#consent-title").textContent = mode === "lecture" ? "Start a lecture" : "Start a meeting";
  document.querySelector("#consent-copy").textContent = mode === "lecture" ? "Please let everyone in the room know that the lecture will be recorded." : "Please let everyone know that this conversation will be recorded.";
  ui.consent.showModal();
}

document.querySelectorAll("[data-page]").forEach((button) => button.addEventListener("click", () => openPage(button.dataset.page)));
ui.micButton.addEventListener("click", () => void openMicrophonePicker());
if (!preview) {
  const appWindow = getCurrentWindow();
  document.querySelector("#window-minimize").addEventListener("click", () => appWindow.minimize());
  document.querySelector("#window-maximize").addEventListener("click", () => appWindow.toggleMaximize());
  document.querySelector("#window-close").addEventListener("click", () => appWindow.close());
}
document.querySelectorAll("[data-shortcut-help]").forEach((button) => button.addEventListener("click", () => document.querySelector("#shortcut-dialog").showModal()));
document.querySelector("#copy-shortcut-command").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText("wicx toggle"); notify("Shortcut command copied."); }
  catch { notify("Copy is unavailable here. Select and copy `wicx toggle` from the dialog.", true); }
});
document.querySelectorAll("[data-session-mode]").forEach((button) => button.addEventListener("click", () => beginSession(button.dataset.sessionMode)));
document.querySelector("#finish-active-session").addEventListener("click", async () => {
  try { updateStatus(await request(mockStatus.session_mode === "dictation" ? "toggle" : "stop_session")); notify("Finishing your recording…"); }
  catch (error) { notify(error.message, true); }
});
document.querySelector("#cancel-active-session").addEventListener("click", async () => {
  if (!window.confirm("Cancel this recording and discard the audio captured so far?")) return;
  try { updateStatus(await request("cancel")); notify("Recording cancelled."); }
  catch (error) { notify(error.message, true); }
});
ui.consentCheck.addEventListener("change", () => { ui.confirmSession.disabled = !ui.consentCheck.checked; });
ui.confirmSession.addEventListener("click", async (event) => {
  event.preventDefault();
  if (!ui.consentCheck.checked || !pendingMode) return;
  ui.confirmSession.disabled = true;
  try {
    updateStatus(await request("start_session", { mode: pendingMode, consent_acknowledged: true }));
    ui.consent.close();
    notify(preview ? "Preview only — session recording starts when connected to the Wicx service." : `${pendingMode === "lecture" ? "Lecture" : "Meeting"} recording started. Use the Finish recording control here when you’re done.`);
  } catch (error) { notify(error.message, true); }
  finally { ui.confirmSession.disabled = !ui.consentCheck.checked; }
});
document.querySelector("#add-term-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#new-term");
  try { const term = input.value.trim(); renderTerms((await request("dictionary", { action: "add", term })).terms || []); notify(`Added “${term}” to your dictionary.`); input.value = ""; }
  catch (error) { notify(error.message, true); }
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
  try { updateStatus(await request("set_recording_pill_hidden", { hidden: ui.hidePill.checked })); notify("Recording pill preference saved."); }
  catch (error) { ui.hidePill.checked = !ui.hidePill.checked; notify(error.message, true); }
});
ui.rewrite.addEventListener("change", async () => {
  try { updateStatus(await request("set_rewrite", { enabled: ui.rewrite.checked })); notify("Rewrite preference saved."); }
  catch (error) { ui.rewrite.checked = !ui.rewrite.checked; notify(error.message, true); }
});

void refreshStatus();
void refreshMicrophones().catch((error) => { if (!preview) notify(error.message, true); });
void request("sessions", { limit: 3, offset: 0 }).then((response) => renderRecent(response.sessions || [])).catch(() => {});
if (!preview) window.setInterval(() => void refreshStatus(), 1200);
