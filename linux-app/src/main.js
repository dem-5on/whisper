import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import appInfo from "../package.json";
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
  consent: document.querySelector("#consent-dialog"),
  consentCheck: document.querySelector("#consent-confirm"),
  confirmSession: document.querySelector("#confirm-session"),
  hidePill: document.querySelector("#setting-hide-pill"),
  rewrite: document.querySelector("#setting-rewrite"),
  micButton: document.querySelector("#choose-microphone"),
  micDialog: document.querySelector("#mic-dialog"),
  micOptions: document.querySelector("#microphone-options"),
  updateButton: document.querySelector("#check-updates"),
  updateDialog: document.querySelector("#update-dialog"),
  updateTitle: document.querySelector("#update-title"),
  updateMessage: document.querySelector("#update-message"),
  updateReleaseLink: document.querySelector("#update-release-link"),
  setupSave: document.querySelector("#setup-save"),
  setupConsent: document.querySelector("#setup-consent"),
};

const currentVersion = String(appInfo.version);
document.querySelector("#app-version").textContent = `Version v${currentVersion}`;

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
let statusTimer;
let pillSyncing = false;
let previousState = "IDLE";

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
  const recording = status.state === "RECORDING";
  if (!busy) {
    mockStatus.session_mode = "";
  }
  void syncRecordingPill(status, busy, activeMode, recording);
  ui.hidePill.checked = status.hide_recording_pill === true;
  ui.rewrite.checked = status.rewrite_enabled === true;
  document.querySelector("#model-name").textContent = `${status.backend === "local" ? "Local" : status.backend || "Local"} · ${status.effective_model || status.model || "base"}`;
  const activeMic = availableMics.find((mic) => mic.id === status.mic);
  document.querySelector("#mic-name").textContent = activeMic?.name || (status.mic && status.mic !== "default" ? status.mic : "System default");
  ui.micButton.disabled = busy;
  document.querySelectorAll("[data-session-mode], #start-dictation, #mode-dictation").forEach((button) => { button.disabled = busy || (!ready && !preview); });
  if (status.state === "ERROR" && status.error) notify(status.error, true);
  if (previousState !== status.state && !busy) {
    void loadActivityStats();
  }
  previousState = status.state;
}

async function syncRecordingPill(status, busy, activeMode, recording) {
  if (preview || pillSyncing) return;
  pillSyncing = true;
  try {
    const shouldShow = busy && status.hide_recording_pill !== true;
    const rewriting = status.rewrite_state === "rewriting";
    await invoke("update_recording_pill", {
      visible: shouldShow,
      status: {
        state: status.state,
        mode: activeMode,
        recording,
        title: recording ? "Recording" : rewriting ? "Rewriting" : "Thinking",
        audio_waveform: status.audio_waveform,
        audio_level: status.audio_level,
      },
    });
  } catch (error) {
    console.warn("Floating recording pill is unavailable.", error);
  } finally {
    pillSyncing = false;
  }
}

async function refreshStatus() {
  try { updateStatus(await request("status")); }
  catch (error) {
    ui.connection.dataset.ready = "false";
    ui.connection.querySelector("span").textContent = preview ? "Preview" : "Offline";
    if (!preview) notify(error.message, true);
  }
}

async function pollStatus() {
  await refreshStatus();
  const delay = mockStatus.state === "RECORDING" ? 100 : ["PROCESSING", "DELIVERING"].includes(mockStatus.state) ? 250 : 1200;
  statusTimer = window.setTimeout(pollStatus, delay);
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

async function loadActivityStats() {
  const sessions = [];
  try {
    let offset = 0;
    let hasMore = true;
    while (hasMore && offset < 100000) {
      const response = await request("sessions", { limit: 1000, offset });
      sessions.push(...(Array.isArray(response.sessions) ? response.sessions : []));
      hasMore = response.has_more === true;
      offset += 1000;
    }
    const today = new Date();
    const sameDay = (date) => date.getFullYear() === today.getFullYear() && date.getMonth() === today.getMonth() && date.getDate() === today.getDate();
    let totalWords = 0;
    let todaySeconds = 0;
    let totalSeconds = 0;
    sessions.forEach((session) => {
      const transcript = String(session.source_transcript || "").trim();
      const words = transcript ? transcript.split(/\s+/u).length : 0;
      totalWords += words;
      const started = Date.parse(session.created_at || "");
      const ended = Date.parse(session.finished_at || "");
      const seconds = Number.isFinite(started) && Number.isFinite(ended) && ended >= started ? (ended - started) / 1000 : 0;
      totalSeconds += seconds;
      if (Number.isFinite(started) && sameDay(new Date(started))) todaySeconds += seconds;
    });
    document.querySelector("#stat-today-minutes").textContent = `${Math.round(todaySeconds / 60)} min`;
    document.querySelector("#stat-total-words").textContent = new Intl.NumberFormat().format(totalWords);
    document.querySelector("#stat-average-wpm").textContent = totalSeconds > 0 ? `${Math.round(totalWords / (totalSeconds / 60))}` : "—";
    document.querySelector("#stat-total-minutes").textContent = `${Math.round(totalSeconds / 60)} min`;
  } catch (error) {
    console.warn("Could not calculate local activity stats.", error);
  }
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
    if (selectedSessionId && sessions.some((session) => session.id === selectedSessionId)) await loadSession(selectedSessionId);
    else showSessionPlaceholder();
  } catch (error) {
    ui.sessionList.innerHTML = `<div class="empty-state">${escapeHtml(error.message)}</div>`;
  }
}

function normalizeVersion(version) {
  const value = version.replace(/^v/i, "");
  // Existing Linux release tags use DDMMYY while the app package uses YYYY.MM.DD.
  if (/^\d{6}$/.test(value)) return `20${value.slice(4, 6)}.${value.slice(2, 4)}.${value.slice(0, 2)}`;
  return value;
}

function compareVersions(left, right) {
  const parts = (version) => normalizeVersion(version).split(/[.+-]/).map((part) => /^\d+$/.test(part) ? Number(part) : part.toLowerCase());
  const a = parts(left);
  const b = parts(right);
  for (let index = 0; index < Math.max(a.length, b.length); index += 1) {
    const first = a[index] ?? 0;
    const second = b[index] ?? 0;
    if (first === second) continue;
    if (typeof first === "number" && typeof second === "number") return first > second ? 1 : -1;
    return String(first).localeCompare(String(second), undefined, { numeric: true });
  }
  return 0;
}

async function checkForUpdates() {
  ui.updateTitle.textContent = "Checking for updates…";
  ui.updateMessage.textContent = "Please wait while Wicx checks the latest release.";
  ui.updateReleaseLink.hidden = true;
  ui.updateButton.disabled = true;
  if (!ui.updateDialog.open) ui.updateDialog.showModal();
  try {
    const response = await fetch("https://api.github.com/repos/dem-5on/whisper/releases/latest", {
      headers: { Accept: "application/vnd.github+json" },
      cache: "no-store",
    });
    if (response.status === 404) {
      ui.updateTitle.textContent = "No update is available yet";
      ui.updateMessage.textContent = "There isn't a published Wicx release to compare with this version yet.";
      return;
    }
    if (!response.ok) throw new Error(response.status === 403 ? "GitHub is temporarily limiting update checks. Try again in a little while." : `Update check failed (HTTP ${response.status}).`);
    const release = await response.json();
    const releaseTag = String(release.tag_name || "");
    if (!releaseTag) throw new Error("The latest release does not include a version number.");
    const latestVersion = normalizeVersion(releaseTag);
    if (compareVersions(latestVersion, currentVersion) > 0) {
      ui.updateTitle.textContent = `Wicx v${latestVersion} is available`;
      ui.updateMessage.textContent = `You're using v${currentVersion}. A newer version is ready to download.`;
      ui.updateReleaseLink.href = release.html_url;
      ui.updateReleaseLink.hidden = false;
    } else {
      ui.updateTitle.textContent = "You're up to date!";
      ui.updateMessage.textContent = `Wicx v${currentVersion} is the newest version available.`;
    }
  } catch (error) {
    ui.updateTitle.textContent = "Couldn't check for updates";
    ui.updateMessage.textContent = error instanceof TypeError ? "Connect to the internet and try again." : error.message;
  } finally {
    ui.updateButton.disabled = false;
  }
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
    const exportTranscript = document.createElement("button");
    exportTranscript.className = "small-button";
    exportTranscript.textContent = "Export transcript";
    exportTranscript.disabled = !session.source_transcript;
    exportTranscript.addEventListener("click", () => {
      const created = new Date(session.created_at);
      const filename = `wicx-${session.mode}-${created.toISOString().slice(0, 10)}.txt`;
      const contents = `Wicx ${session.mode} transcript\n${created.toLocaleString()}\n\n${session.source_transcript || ""}\n`;
      const url = URL.createObjectURL(new Blob([contents], { type: "text/plain;charset=utf-8" }));
      const link = document.createElement("a");
      link.href = url;
      link.download = filename;
      document.body.append(link);
      link.click();
      link.remove();
      window.setTimeout(() => URL.revokeObjectURL(url), 1000);
      notify("Transcript exported as a text file.");
    });
    const remove = document.createElement("button");
    remove.className = "small-button danger";
    remove.textContent = "Delete";
    remove.addEventListener("click", async () => {
      if (!window.confirm("Delete this recording, transcript, and saved audio from this device? This cannot be undone.")) return;
      try { await request("delete_session", { session_id: id }); selectedSessionId = ""; notify("Recording deleted from this device."); await loadSessions(); }
      catch (error) { notify(error.message, true); }
    });
    actions.append(copy, exportTranscript, remove);
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
document.querySelector("[data-account-open]").addEventListener("click", () => {
  openPage("settings");
  document.querySelectorAll("[data-settings-section]").forEach((item) => item.classList.toggle("selected", item.dataset.settingsSection === "account"));
  document.querySelectorAll("[data-settings-panel]").forEach((panel) => { panel.hidden = panel.dataset.settingsPanel !== "account"; });
});
document.querySelector("#open-help").addEventListener("click", () => document.querySelector("#help-dialog").showModal());
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

void refreshMicrophones().catch((error) => { if (!preview) notify(error.message, true); });
ui.updateButton.addEventListener("click", () => void checkForUpdates());
void loadActivityStats();

// ---- Global shortcut setup (first-run page, order: Dictation, Meeting, Lecture)

const SHORTCUT_ACTIONS = ["dictation", "meeting", "lecture"];
const SHORTCUT_DEFAULTS = {
  dictation: { display: "Right Alt", accelerator: "RightAlt" },
  meeting: { display: "", accelerator: null },
  lecture: { display: "", accelerator: null },
};
let mockShortcuts = {
  bindings: {
    dictation: { display: "Right Alt", accelerator: "RightAlt", register_error: null },
    meeting: { display: "", accelerator: null, register_error: null },
    lecture: { display: "", accelerator: null, register_error: null },
  },
  long_consent: false,
  setup_completed: false,
};
let shortcutDraft = JSON.parse(JSON.stringify(SHORTCUT_DEFAULTS));
let capturingAction = null;
let pressedCodes = new Set();

async function shortcutInvoke(command, params = {}) {
  if (!preview) return invoke(command, params);
  await new Promise((resolve) => window.setTimeout(resolve, 60));
  if (command === "get_shortcuts") return JSON.parse(JSON.stringify(mockShortcuts));
  if (command === "set_shortcut") {
    const { action, display, accelerator } = params;
    mockShortcuts.bindings[action] = {
      display: accelerator ? display : "",
      accelerator: accelerator || null,
      register_error: null,
    };
    return JSON.parse(JSON.stringify(mockShortcuts));
  }
  if (command === "set_long_consent") {
    mockShortcuts.long_consent = params.consented === true;
    return { long_consent: mockShortcuts.long_consent };
  }
  if (command === "complete_setup") {
    mockShortcuts.setup_completed = true;
    return { setup_completed: true };
  }
  throw new Error("Unsupported shortcut action.");
}

function codeToKeyName(code) {
  if (code === "Space") return { display: "Space", code: "Space" };
  if (/^Key[A-Z]$/.test(code)) return { display: code.slice(3), code };
  if (/^Digit[0-9]$/.test(code)) return { display: code.slice(5), code };
  if (/^F\d{1,2}$/.test(code)) return { display: code, code };
  const map = {
    Tab: "Tab", CapsLock: "CapsLock", Backquote: "`", Minus: "-", Equal: "=",
    BracketLeft: "[", BracketRight: "]", Backslash: "\\", Semicolon: ";",
    Quote: "'", Comma: ",", Period: ".", Slash: "/", Insert: "Insert",
    Delete: "Delete", Home: "Home", End: "End", PageUp: "PageUp",
    PageDown: "PageDown", ArrowUp: "Up", ArrowDown: "Down", ArrowLeft: "Left",
    ArrowRight: "Right", PrintScreen: "PrintScreen", ScrollLock: "ScrollLock", Pause: "Pause",
  };
  return map[code] ? { display: map[code], code } : null;
}

function describeCapture() {
  const mods = [];
  let rightAlt = false;
  let leftAlt = false;
  for (const code of pressedCodes) {
    if (code === "AltRight") rightAlt = true;
    else if (code === "AltLeft") leftAlt = true;
    else if (code === "ControlLeft" || code === "ControlRight") mods.push({ display: "Ctrl", code: "Control" });
    else if (code === "ShiftLeft" || code === "ShiftRight") mods.push({ display: "Shift", code: "Shift" });
    else if (code === "MetaLeft" || code === "MetaRight") mods.push({ display: "Super", code: "Super" });
  }
  const mains = [...pressedCodes].map(codeToKeyName).filter(Boolean);
  return { mods, rightAlt, leftAlt, mains };
}

function renderCapture(action, interim) {
  const display = document.querySelector(`#capture-${action}`);
  const error = document.querySelector(`#error-${action}`);
  if (!display) return;
  if (interim) {
    display.textContent = interim;
    display.classList.add("capturing");
    if (error) error.hidden = true;
    return;
  }
  display.classList.remove("capturing");
  const draft = shortcutDraft[action];
  display.textContent = draft.display || "Not set";
  display.classList.toggle("capture-empty", !draft.display);
  if (error) error.hidden = true;
}

function captureError(action, message) {
  const error = document.querySelector(`#error-${action}`);
  if (!error) return;
  error.textContent = message;
  error.hidden = false;
}

function tryCommitCapture() {
  if (!capturingAction) return;
  const action = capturingAction;
  const { mods, rightAlt, leftAlt, mains } = describeCapture();
  const main = mains[0];
  if (!main) {
    if (rightAlt && !leftAlt && mods.length === 0) {
      if (action !== "dictation") {
        captureError(action, "Right Alt alone is reserved for Dictation. Pick a combination.");
        renderCapture(action, "Right Alt + …");
        return;
      }
      shortcutDraft[action] = { display: "Right Alt", accelerator: "RightAlt" };
      capturingAction = null;
      pressedCodes.clear();
      renderCapture(action, null);
      return;
    }
    const parts = [];
    if (leftAlt) parts.push("Left Alt");
    if (rightAlt) parts.push("Right Alt");
    for (const mod of mods) parts.push(mod.display);
    if (parts.length > 0) {
      renderCapture(action, `${parts.join(" + ")} + …`);
      if (leftAlt && !rightAlt && mods.length === 0) {
        captureError(action, "Left Alt alone breaks app menus. Add another key, or press Right Alt instead.");
      }
    }
    return;
  }
  const displays = [];
  const codes = [];
  if (rightAlt || leftAlt) {
    displays.push("Alt");
    codes.push("Alt");
  }
  for (const mod of mods) {
    displays.push(mod.display);
    codes.push(mod.code);
  }
  displays.push(main.display);
  codes.push(main.code);
  shortcutDraft[action] = { display: displays.join(" + "), accelerator: codes.join("+") };
  capturingAction = null;
  pressedCodes.clear();
  renderCapture(action, null);
}

function startCapture(action) {
  capturingAction = action;
  pressedCodes.clear();
  renderCapture(action, "Press keys… (Esc cancels)");
}

document.addEventListener("keydown", (event) => {
  if (!capturingAction) return;
  event.preventDefault();
  event.stopPropagation();
  if (event.code === "Escape") {
    const action = capturingAction;
    capturingAction = null;
    pressedCodes.clear();
    renderCapture(action, null);
    return;
  }
  if (event.repeat) return;
  if ((event.code === "Backspace" || event.code === "Delete") && pressedCodes.size === 0) {
    shortcutDraft[capturingAction] = { display: "", accelerator: null };
    const action = capturingAction;
    capturingAction = null;
    renderCapture(action, null);
    return;
  }
  pressedCodes.add(event.code);
  tryCommitCapture();
}, true);

document.addEventListener("keyup", (event) => {
  if (capturingAction) pressedCodes.delete(event.code);
}, true);

document.querySelectorAll("[data-capture]").forEach((button) => {
  button.addEventListener("click", () => startCapture(button.dataset.capture));
});
document.querySelectorAll("[data-clear]").forEach((button) => {
  button.addEventListener("click", () => {
    const action = button.dataset.clear;
    shortcutDraft[action] = { display: "", accelerator: null };
    renderCapture(action, null);
  });
});

function renderShortcutState(state) {
  for (const action of SHORTCUT_ACTIONS) {
    const entry = state.bindings?.[action];
    if (entry && (entry.display || entry.accelerator)) {
      shortcutDraft[action] = { display: entry.display || "", accelerator: entry.accelerator || null };
    }
    renderCapture(action, null);
    const error = document.querySelector(`#error-${action}`);
    const registerError = entry?.register_error;
    if (error && registerError) {
      error.textContent = registerError;
      error.hidden = false;
    }
  }
  if (ui.setupConsent) ui.setupConsent.checked = state.long_consent === true;
}

let setupStep = 1;
let setupMic = "default";
let setupMicChoices = [];

function showSetupStep(n) {
  setupStep = n;
  document.querySelectorAll("[data-setup-page]").forEach((page) => {
    page.hidden = page.dataset.setupPage !== String(n);
  });
  document.querySelectorAll("[data-setup-progress]").forEach((item) => {
    item.classList.toggle("selected", Number(item.dataset.setupProgress) === n);
  });
  const titles = { 1: "Choose your shortcuts", 2: "Choose your microphone", 3: "You are ready" };
  const subtitles = {
    1: "Press a key or combination for each mode, in order. The same key starts and stops recording.",
    2: "Wicx listens to this input for every mode. You can change it later in the side panel.",
    3: "Review and finish. You can revisit setup anytime from Settings.",
  };
  const title = document.querySelector("#setup-title");
  const subtitle = document.querySelector("#setup-subtitle");
  if (title) title.textContent = titles[n];
  if (subtitle) subtitle.textContent = subtitles[n];
  if (n === 2) void renderSetupMics();
  if (n === 3) renderSetupSummary();
}

function setupMicLabel(id) {
  if (!id || id === "default") return "System default";
  const found = setupMicChoices.find((mic) => mic.id === id);
  return found ? found.name : id;
}

async function renderSetupMics() {
  const box = document.querySelector("#setup-mics");
  if (!box) return;
  box.innerHTML = '<div class="empty-state">Loading microphones…</div>';
  try {
    let mics = [{ id: "default", name: "System default" }];
    if (!preview) {
      const response = await invoke("daemon_request", { command: "mics", params: {} });
      if (Array.isArray(response.mics) && response.mics.length > 0) {
        mics = [{ id: "default", name: "System default" }];
        for (const mic of response.mics) {
          if (mic.id !== "default") mics.push({ id: String(mic.id), name: String(mic.name || mic.id) });
        }
      }
      const status = await invoke("daemon_request", { command: "status", params: {} });
      if (status.mic) setupMic = String(status.mic);
    } else {
      await new Promise((resolve) => window.setTimeout(resolve, 60));
    }
    setupMicChoices = mics;
    box.replaceChildren();
    for (const mic of mics) {
      const label = document.createElement("label");
      label.className = "mic-option";
      const radio = document.createElement("input");
      radio.type = "radio";
      radio.name = "setup-mic";
      radio.value = mic.id;
      radio.checked = setupMic === mic.id || (setupMic === "default" && mic.id === "default");
      radio.addEventListener("change", () => { setupMic = mic.id; });
      const text = document.createElement("span");
      text.textContent = mic.name;
      label.append(radio, text);
      box.append(label);
    }
  } catch (error) {
    box.innerHTML = '<div class="empty-state">Could not list microphones. You can pick one later.</div>';
  }
}

function renderSetupSummary() {
  const box = document.querySelector("#setup-summary");
  if (!box) return;
  const rows = SHORTCUT_ACTIONS.map((action) => {
    const name = action.charAt(0).toUpperCase() + action.slice(1);
    const binding = shortcutDraft[action].display || "Not set";
    return `<div class="summary-row"><span>${name}</span><code>${binding}</code></div>`;
  }).join("");
  box.innerHTML = rows + `<div class="summary-row"><span>Microphone</span><code>${setupMicLabel(setupMic)}</code></div>`;
}

function setupDuplicates() {
  const seen = new Map();
  for (const action of SHORTCUT_ACTIONS) {
    const accelerator = shortcutDraft[action].accelerator;
    if (!accelerator) continue;
    const key = accelerator.toUpperCase();
    if (seen.has(key)) return action;
    seen.set(key, action);
  }
  return null;
}

async function saveShortcuts() {
  const usesLongMode = ["meeting", "lecture"].some((action) => shortcutDraft[action].accelerator);
  if (usesLongMode && !(ui.setupConsent && ui.setupConsent.checked)) {
    notify("Check the recording-consent box to enable Meeting or Lecture shortcuts.", true);
    return;
  }
  try {
    let state = null;
    for (const action of SHORTCUT_ACTIONS) {
      state = await shortcutInvoke("set_shortcut", {
        action,
        display: shortcutDraft[action].display || "",
        accelerator: shortcutDraft[action].accelerator,
      });
    }
    if (usesLongMode) await shortcutInvoke("set_long_consent", { consented: true });
    if (!preview && setupMic) {
      try { await invoke("daemon_request", { command: "set_mic", params: { device: setupMic } }); }
      catch (error) { notify("Microphone could not be saved. Pick one later in the side panel.", true); }
    }
    renderShortcutState(state);
    const refused = SHORTCUT_ACTIONS.filter((action) => state.bindings?.[action]?.register_error);
    if (refused.length > 0) {
      notify(`Saved, but the system refused: ${refused.join(", ")}. Use the desktop-settings fallback for those.`, true);
      showSetupStep(1);
      return;
    }
    await shortcutInvoke("complete_setup", {});
    notify("Shortcuts saved. Press them anywhere to record.");
    openPage("home");
  } catch (error) {
    notify(error && error.message ? error.message : String(error), true);
  }
}

if (ui.setupSave) ui.setupSave.addEventListener("click", () => void saveShortcuts());

function setupNextFromShortcuts() {
  const dup = setupDuplicates();
  if (dup) {
    captureError(dup, "Same shortcut as another mode. Press a different combination.");
    notify("Two modes share one shortcut. Give each its own.", true);
    return;
  }
  const usesLongMode = ["meeting", "lecture"].some((action) => shortcutDraft[action].accelerator);
  if (usesLongMode && !(ui.setupConsent && ui.setupConsent.checked)) {
    notify("Check the recording-consent box to enable Meeting or Lecture shortcuts.", true);
    return;
  }
  if (!shortcutDraft.dictation.accelerator) {
    notify("Dictation needs a shortcut. Press Right Alt or a combination.", true);
    return;
  }
  showSetupStep(2);
}

async function bootShortcuts() {
  try {
    const state = await shortcutInvoke("get_shortcuts", {});
    renderShortcutState(state);
    if (state.setup_completed !== true) {
      showSetupStep(1);
      openPage("setup");
    }
  } catch (error) {
    if (!preview) notify("Could not load keyboard shortcuts.", true);
  }
}

document.querySelector("#setup-next-1")?.addEventListener("click", setupNextFromShortcuts);
document.querySelector("#setup-next-2")?.addEventListener("click", () => showSetupStep(3));
document.querySelector("#setup-back-2")?.addEventListener("click", () => showSetupStep(1));
document.querySelector("#setup-back-3")?.addEventListener("click", () => showSetupStep(2));

if (!preview) {
  listen("shortcut-needs-consent", (event) => {
    const mode = String(event.payload || "");
    if (mode === "lecture" || mode === "meeting") beginSession(mode);
  }).catch(() => {});
}
void bootShortcuts();
