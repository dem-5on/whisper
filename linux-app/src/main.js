import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import appInfo from "../package.json";
import "./style.css";
console.timeStamp("wicx: frontend module start");

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
  void setupLiveStatus(status);
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
  document.body.classList.toggle("in-setup", page === "setup");
  ui.pages.forEach((element) => {
    const active = element.id === `page-${page}`;
    element.hidden = !active;
    element.classList.toggle("active", active);
  });
  document.querySelectorAll(".nav-item[data-page]").forEach((button) => button.classList.toggle("selected", button.dataset.page === page));
  if (page === "history") void loadSessions();
  if (page === "dictionary") void loadDictionary();
  void setupBgControl();
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

// ---- First-run setup walkthrough: Welcome > Set up > Experience it

const SHORTCUT_ACTIONS = ["dictation", "meeting", "lecture"];
const SHORTCUT_DEFAULTS = {
  dictation: { display: "Right Alt", accelerator: "RightAlt" },
  meeting: { display: "", accelerator: null },
  lecture: { display: "", accelerator: null },
};
const SHORTCUT_BLURB = {
  dictation: "Speak into whatever app you're using.",
  meeting: "Capture a discussion with speaker labels and outcomes.",
  lecture: "Capture a class with audio and transcript together.",
};
const ROLES = [
  ["student", "Student"], ["engineering", "Engineering / data"], ["writer", "Writer"],
  ["healthcare", "Healthcare"], ["legal", "Legal"], ["finance", "Finance"],
  ["educator", "Educator"], ["marketing", "Marketing"], ["sales", "Sales"],
  ["creator", "Creator"], ["other", "Other"],
];
let mockShortcuts = {
  bindings: {
    dictation: { display: "Right Alt", accelerator: "RightAlt", register_error: null },
    meeting: { display: "", accelerator: null, register_error: null },
    lecture: { display: "", accelerator: null, register_error: null },
  },
  long_consent: false,
  setup_completed: false,
  role: "",
};
let shortcutDraft = JSON.parse(JSON.stringify(SHORTCUT_DEFAULTS));
let setupRole = "";
let setupScreen = "welcome";
let capturingAction = null;
let pressedCodes = new Set();
let seenMods = new Set();
let testedAction = { dictation: false, meeting: false, lecture: false };
let setupMic = "default";
let setupMicChoices = [];
let micTesting = false;
let micMaxPeak = 0;

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
  if (command === "set_role") {
    mockShortcuts.role = String(params.role || "");
    return { role: mockShortcuts.role, seeded: 5 };
  }
  if (command === "set_shortcut_test") return { shortcut_test: params.active === true };
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

function modOf(code) {
  if (code === "AltRight" || code === "AltLeft") return { display: "Alt", code: "Alt" };
  if (code === "ControlLeft" || code === "ControlRight") return { display: "Ctrl", code: "Control" };
  if (code === "ShiftLeft" || code === "ShiftRight") return { display: "Shift", code: "Shift" };
  if (code === "MetaLeft" || code === "MetaRight") return { display: "Super", code: "Super" };
  return null;
}

function describeCapture() {
  const mods = [];
  const seen = new Set();
  let rightAlt = false;
  let leftAlt = false;
  const note = (mod) => {
    if (!seen.has(mod.code)) {
      seen.add(mod.code);
      mods.push(mod);
    }
  };
  for (const code of pressedCodes) {
    if (code === "AltRight") rightAlt = true;
    else if (code === "AltLeft") leftAlt = true;
    else {
      const mod = modOf(code);
      if (mod) note(mod);
    }
  }
  for (const code of seenMods) {
    const mod = modOf(code);
    if (mod) note(mod);
  }
  const mains = [...pressedCodes].map(codeToKeyName).filter(Boolean);
  return { mods, rightAlt, leftAlt, mains };
}

function clearCaptureState() {
  capturingAction = null;
  pressedCodes.clear();
  seenMods.clear();
}

function renderCapture(action, interim) {
  const display = document.querySelector("#capture-" + action);
  const error = document.querySelector("#error-" + action);
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
  const error = document.querySelector("#error-" + action);
  if (!error) return;
  error.textContent = message;
  error.hidden = false;
}


function tryCommitCapture() {
  if (!capturingAction) return;
  const action = capturingAction;
  const { mods, rightAlt, leftAlt, mains } = describeCapture();
  const main = mains[0];
  const commit = (display, accelerator) => {
    shortcutDraft[action] = { display, accelerator };
    clearCaptureState();
    testedAction[action] = false;
    renderSingleCards();
    void shortcutInvoke("set_shortcut", { action, display, accelerator })
      .then(renderShortcutErrors)
      .catch((error) => captureError(action, error && error.message ? error.message : String(error)));
  };
  if (!main) {
    const onlyAlt = mods.every((mod) => mod.code === "Alt");
    if (rightAlt && !leftAlt && onlyAlt) {
      if (action !== "dictation") {
        captureError(action, "Right Alt alone is reserved for Dictation. Pick a combination.");
        return;
      }
      commit("Right Alt", "RightAlt");
    }
    return;
  }
  const displays = [];
  const codes = [];
  if ((rightAlt || leftAlt) && !mods.some((mod) => mod.code === "Alt")) {
    displays.push("Alt");
    codes.push("Alt");
  }
  for (const mod of mods) {
    displays.push(mod.display);
    codes.push(mod.code);
  }
  displays.push(main.display);
  codes.push(main.code);
  commit(displays.join(" + "), codes.join("+"));
}

function startCapture(action) {
  const def = SHORTCUT_DEFAULTS[action] || { display: "", accelerator: null };
  shortcutDraft[action] = { display: def.display, accelerator: def.accelerator };
  testedAction[action] = false;
  renderSingleCards();
  void shortcutInvoke("set_shortcut", {
    action, display: def.display || "", accelerator: def.accelerator,
  }).then(renderShortcutErrors).catch(() => {});
  capturingAction = action;
  pressedCodes.clear();
  seenMods.clear();
  const status = document.querySelector("#teststatus-" + action);
  if (status) status.textContent = "Press your shortcut now — the key lights up when seen. (Esc cancels)";
  refreshYesButtons();
}

document.addEventListener("keydown", (event) => {
  if (!capturingAction) return;
  event.preventDefault();
  event.stopPropagation();
  if (event.code === "Escape") {
    clearCaptureState();
    renderSingleCards();
    return;
  }
  if (event.repeat) return;
  if ((event.code === "Backspace" || event.code === "Delete") && pressedCodes.size === 0) {
    shortcutDraft[capturingAction] = { display: "", accelerator: null };
    const action = capturingAction;
    clearCaptureState();
    renderCapture(action, null);
    renderSingleCards();
    return;
  }
  pressedCodes.add(event.code);
  if (modOf(event.code)) seenMods.add(event.code);
  tryCommitCapture();
}, true);

document.addEventListener("keyup", (event) => {
  if (capturingAction) pressedCodes.delete(event.code);
}, true);

// ---- Walkthrough screens

const SETUP_PHASE = {
  welcome: "welcome",
  "shortcut-dictation": "setup", "shortcut-meeting": "setup", "shortcut-lecture": "setup",
  mic: "setup", role: "setup",
  intro: "experience", exercise: "experience", summary: "experience",
};

let longConsentDraft = false;

function isShortcutScreen() {
  return setupScreen === "shortcut-dictation" || setupScreen === "shortcut-meeting" || setupScreen === "shortcut-lecture";
}

function showSetup(name) {
  if (!SETUP_PHASE[name]) return;
  clearCaptureState();
  setupScreen = name;
  const head = document.querySelector("#setup-head");
  if (head) head.hidden = !isShortcutScreen();
  document.querySelectorAll("[data-setup-page]").forEach((page) => {
    page.hidden = page.dataset.setupPage !== name;
  });
  document.querySelectorAll("[data-phase]").forEach((item) => {
    item.classList.toggle("selected", item.dataset.phase === SETUP_PHASE[name]);
  });
  void setTestMode(isShortcutScreen() || name === "mic");
  void setupBgControl();
  if (isShortcutScreen()) renderSingleCards();
  if (name === "mic") void renderSetupMics();
  if (name === "role") renderRoleGrid();
  if (name === "exercise") {
    const box = document.querySelector("#exercise-box");
    if (box) {
      box.value = "";
      const go = document.querySelector("#exercise-continue");
      if (go) go.disabled = true;
      const note = document.querySelector("#exercise-note");
      if (note) note.hidden = true;
      window.setTimeout(() => box.focus(), 60);
    }
  }
  if (name === "summary") renderSummaryCards();
}

async function setTestMode(on) {
  if (preview) return;
  try {
    await invoke("set_shortcut_test", { active: on === true });
  } catch (error) {
    /* test mode is best-effort; capture still works in-app */
  }
}

function renderSingleCards() {
  for (const action of SHORTCUT_ACTIONS) {
    const mount = document.querySelector("#single-" + action);
    if (!mount) continue;
    const draft = shortcutDraft[action];
    mount.replaceChildren();
    const card = document.createElement("article");
    card.className = "setup-step";
    const head = document.createElement("div");
    head.className = "setup-step-head";
    const title = document.createElement("div");
    const name = document.createElement("h2");
    name.textContent = action.charAt(0).toUpperCase() + action.slice(1);
    const blurb = document.createElement("p");
    blurb.textContent = SHORTCUT_BLURB[action];
    title.append(name, blurb);
    head.append(title);
    const visual = document.createElement("div");
    visual.className = "test-key-visual";
    const keycap = document.createElement("div");
    keycap.className = "test-keycap test-keycap-big";
    keycap.id = "testkey-" + action;
    keycap.textContent = draft.display || "Not set";
    if (testedAction[action]) keycap.classList.add("lit");
    visual.append(keycap);
    const row = document.createElement("div");
    row.className = "capture-row";
    const status = document.createElement("span");
    status.className = "capture-hint test-status-inline";
    status.id = "teststatus-" + action;
    status.textContent = testedAction[action]
      ? "Working. Press it anytime."
      : "Press " + (draft.display || "your shortcut") + " now — the key lights up when seen.";
    const change = document.createElement("button");
    change.className = "secondary-button";
    change.type = "button";
    change.textContent = "Change";
    change.addEventListener("click", () => startCapture(action));
    row.append(change, status);
    const error = document.createElement("p");
    error.className = "capture-error";
    error.id = "error-" + action;
    error.hidden = true;
    card.append(head, visual, row, error);
    card.id = "testcard-" + action;
    mount.append(card);
  }
  refreshYesButtons();
}

function refreshYesButtons() {
  const yesDictation = document.querySelector("#yes-dictation");
  if (yesDictation) yesDictation.disabled = testedAction.dictation !== true;
  for (const action of ["meeting", "lecture"]) {
    const yes = document.querySelector("#yes-" + action);
    if (!yes) continue;
    if (!shortcutDraft[action].accelerator) {
      yes.disabled = false;
      continue;
    }
    yes.disabled = testedAction[action] !== true;
  }
}

function checkDuplicates() {
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

function setKeycapLit(action, on) {
  const keycap = document.querySelector("#testkey-" + action);
  if (!keycap) return;
  keycap.classList.toggle("lit", on === true);
}

async function verifyTestPress(action) {
  if (!isShortcutScreen() || capturingAction) return;
  const draft = shortcutDraft[action];
  if (!draft.accelerator) return;
  setKeycapLit(action, true);
  try {
    const state = await shortcutInvoke("set_shortcut", {
      action, display: draft.display || "", accelerator: draft.accelerator,
    });
    renderShortcutErrors(state);
    const refused = state.bindings?.[action]?.register_error;
    if (refused) {
      setKeycapLit(action, false);
      captureError(action, refused);
      return;
    }
    testedAction[action] = true;
    renderSingleCards();
    setKeycapLit(action, true);
  } catch (error) {
    setKeycapLit(action, false);
    captureError(action, error && error.message ? error.message : String(error));
  }
}

function comboFromEvent(event) {
  if (event.code === "AltRight") return { action: "dictation", matched: true };
  const mods = [];
  if (event.altKey) mods.push("Alt");
  if (event.ctrlKey) mods.push("Control");
  if (event.shiftKey) mods.push("Shift");
  if (event.metaKey) mods.push("Super");
  const main = codeToKeyName(event.code);
  if (!main || mods.length === 0) return { matched: false };
  const combo = mods.concat([main.code]).join("+").toUpperCase();
  for (const action of SHORTCUT_ACTIONS) {
    const accel = shortcutDraft[action].accelerator;
    if (accel && accel.toUpperCase() === combo) return { action, matched: true };
  }
  return { matched: false };
}

document.addEventListener("keydown", (event) => {
  if (!isShortcutScreen()) return;
  if (capturingAction || event.repeat) return;
  const hit = comboFromEvent(event);
  if (hit.matched) void verifyTestPress(hit.action);
});

if (!preview) {
  listen("shortcut-test-press", (event) => {
    const action = String(event.payload || "");
    if (SHORTCUT_ACTIONS.includes(action)) void verifyTestPress(action);
  }).catch(() => {});
  listen("shortcut-needs-consent", (event) => {
    const mode = String(event.payload || "");
    if (mode === "lecture" || mode === "meeting") beginSession(mode);
  }).catch(() => {});
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
      radio.checked = setupMic === mic.id;
      radio.addEventListener("change", () => {
        setupMic = mic.id;
        if (!preview) invoke("daemon_request", { command: "set_mic", params: { device: mic.id } }).catch(() => {});
      });
      const text = document.createElement("span");
      text.textContent = mic.name;
      label.append(radio, text);
      box.append(label);
    }
  } catch (error) {
    box.innerHTML = '<div class="empty-state">Could not list microphones. You can pick one later.</div>';
  }
}

function ensureMicBars() {
  const box = document.querySelector("#mic-test-bars");
  if (!box || box.dataset.built === "true") return;
  box.dataset.built = "true";
  for (let i = 0; i < 12; i += 1) {
    const bar = document.createElement("span");
    box.append(bar);
  }
}

function setupLiveStatus(status) {
  ensureMicBars();
  if (setupScreen !== "mic" || !micTesting) return;
  if (status.state !== "RECORDING") return;
  const samples = Array.isArray(status.audio_waveform) ? status.audio_waveform : [];
  let peak = 0;
  const bars = document.querySelectorAll("#mic-test-bars span");
  samples.forEach((sample, index) => {
    const value = Math.abs(Number(sample) || 0);
    if (index < 12 * 2 && index % 2 === 0 && value > peak) peak = value;
  });
  if (peak > micMaxPeak) micMaxPeak = peak;
  const pairs = [];
  for (let i = 0; i < 12; i += 1) {
    const a = Math.abs(Number(samples[i * 2]) || 0);
    const b = Math.abs(Number(samples[i * 2 + 1]) || 0);
    pairs.push(Math.max(a, b));
  }
  bars.forEach((bar, index) => {
    const value = pairs[index] || 0;
    bar.style.height = (value < 0.002 ? 3 : 3 + Math.pow(value, 0.4) * 30).toFixed(1) + "px";
    bar.classList.toggle("hot", value >= 0.02);
  });
}

async function toggleMicTest() {
  const button = document.querySelector("#mic-test-btn");
  const error = document.querySelector("#mic-test-error");
  if (!micTesting) {
    micTesting = true;
    micMaxPeak = 0;
    if (error) error.hidden = true;
    if (button) button.textContent = "Stop & discard";
    try {
      if (!preview) await invoke("daemon_request", { command: "toggle", params: {} });
    } catch (err) {
      micTesting = false;
      if (button) button.textContent = "Test microphone";
      if (error) {
        error.textContent = err && err.message ? err.message : String(err);
        error.hidden = false;
      }
    }
    return;
  }
  micTesting = false;
  if (button) button.textContent = "Test microphone";
  try {
    if (!preview) await invoke("daemon_request", { command: "cancel", params: {} });
  } catch (err) {
    /* already stopped; fall through to evaluation */
  }
  const yes = document.querySelector("#mic-test-yes");
  if (micMaxPeak > 0.02) {
    if (yes) yes.disabled = false;
    notify("Microphone responds. Continue when ready.");
  } else if (error) {
    error.textContent = "No speech detected — check the selected input and try again.";
    error.hidden = false;
  }
}

function renderRoleGrid() {
  const box = document.querySelector("#role-grid");
  if (!box || box.dataset.built === "true") return;
  box.dataset.built = "true";
  for (const [id, label] of ROLES) {
    const button = document.createElement("button");
    button.className = "role-chip";
    button.type = "button";
    button.textContent = label;
    button.dataset.role = id;
    if (setupRole === id) button.classList.add("selected");
    button.addEventListener("click", () => {
      setupRole = id;
      box.querySelectorAll(".role-chip").forEach((chip) => {
        chip.classList.toggle("selected", chip.dataset.role === id);
      });
      const go = document.querySelector("#role-continue");
      if (go) go.disabled = false;
    });
    box.append(button);
  }
  const selected = document.querySelector("#role-continue");
  if (selected && setupRole) selected.disabled = false;
}

async function saveRoleAndContinue() {
  if (!setupRole) {
    notify("Pick the role that fits best to continue.", true);
    return;
  }
  try {
    const result = await shortcutInvoke("set_role", { role: setupRole });
    if (result.seeded > 0) notify("Starter vocabulary for your field is in Dictionary.");
    showSetup("intro");
  } catch (error) {
    notify(error && error.message ? error.message : String(error), true);
  }
}

function renderSummaryCards() {
  const box = document.querySelector("#setup-summary-cards");
  if (!box) return;
  box.replaceChildren();
  for (const action of SHORTCUT_ACTIONS) {
    const card = document.createElement("div");
    card.className = "summary-card";
    const title = document.createElement("strong");
    title.textContent = action.charAt(0).toUpperCase() + action.slice(1);
    const key = document.createElement("code");
    key.textContent = shortcutDraft[action].display || "Not set";
    card.append(title, key);
    box.append(card);
  }
}

function advanceFromDictation() {
  if (testedAction.dictation !== true || !shortcutDraft.dictation.accelerator) {
    notify("Press Right Alt once to verify Dictation first.", true);
    return;
  }
  showSetup("shortcut-meeting");
}

function advanceFromLongMode(action, next) {
  if (shortcutDraft[action].accelerator && testedAction[action] !== true) {
    notify("Press the shortcut once to verify it first, or clear it to skip.", true);
    return;
  }
  if (shortcutDraft[action].accelerator && !longConsentDraft) {
    notify("Check the recording-consent box to enable this shortcut.", true);
    return;
  }
  const dup = checkDuplicates();
  if (dup) {
    captureError(dup, "Same shortcut as another mode. Press a different combination.");
    notify("Two modes share one shortcut. Give each its own.", true);
    showSetup("shortcut-dictation");
    return;
  }
  if (shortcutDraft[action].accelerator) {
    void shortcutInvoke("set_long_consent", { consented: true }).catch(() => {});
  }
  showSetup(next);
}

function renderShortcutState(state) {
  if (!state) return;
  if (state.bindings) {
    for (const action of SHORTCUT_ACTIONS) {
      const entry = state.bindings[action];
      if (entry && (entry.display || entry.accelerator)) {
        shortcutDraft[action] = { display: entry.display || "", accelerator: entry.accelerator || null };
      }
      renderCapture(action, null);
    }
    renderShortcutErrors(state);
  }
  longConsentDraft = state.long_consent === true;
  document.querySelectorAll(".setup-consent-box").forEach((box) => {
    box.checked = longConsentDraft;
  });
  if (state.role) {
    setupRole = String(state.role);
    const go = document.querySelector("#role-continue");
    if (go && setupRole) go.disabled = false;
  }
  renderSingleCards();
}

function renderShortcutErrors(state) {
  if (!state || !state.bindings) return;
  for (const action of SHORTCUT_ACTIONS) {
    const error = document.querySelector("#error-" + action);
    const registerError = state.bindings[action]?.register_error;
    if (error && registerError) {
      error.textContent = registerError;
      error.hidden = false;
    }
  }
}

async function finishSetup() {
  try {
    await shortcutInvoke("complete_setup", {});
    await setTestMode(false);
    notify("Setup complete. Press Right Alt anywhere to dictate.");
    openPage("home");
  } catch (error) {
    notify(error && error.message ? error.message : String(error), true);
  }
}

async function bootShortcuts() {
  try {
    const state = await shortcutInvoke("get_shortcuts", {});
    renderShortcutState(state);
    if (state.setup_completed !== true) {
      showSetup("welcome");
      openPage("setup");
    }
  } catch (error) {
    if (!preview) notify("Could not load keyboard shortcuts.", true);
  }
}

document.querySelector("#yes-dictation")?.addEventListener("click", advanceFromDictation);
document.querySelector("#yes-meeting")?.addEventListener("click", () => advanceFromLongMode("meeting", "shortcut-lecture"));
document.querySelector("#yes-lecture")?.addEventListener("click", () => advanceFromLongMode("lecture", "mic"));
document.querySelectorAll(".setup-consent-box").forEach((box) => {
  box.addEventListener("change", () => {
    longConsentDraft = box.checked;
    document.querySelectorAll(".setup-consent-box").forEach((other) => {
      other.checked = longConsentDraft;
    });
    refreshYesButtons();
  });
});
document.querySelector("#mic-test-btn")?.addEventListener("click", () => void toggleMicTest());
document.querySelector("#mic-test-yes")?.addEventListener("click", () => showSetup("role"));
document.querySelector("#role-continue")?.addEventListener("click", () => void saveRoleAndContinue());
document.querySelector("#setup-finish")?.addEventListener("click", () => void finishSetup());
document.querySelectorAll("[data-setup-go]").forEach((button) => {
  button.addEventListener("click", () => showSetup(button.dataset.setupGo));
});
document.querySelector("#exercise-box")?.addEventListener("input", (event) => {
  const go = document.querySelector("#exercise-continue");
  const done = event.target.value.trim().length > 3;
  if (go) go.disabled = !done;
  const note = document.querySelector("#exercise-note");
  if (note) note.hidden = !done;
});
document.querySelector("#exercise-continue")?.addEventListener("click", () => showSetup("summary"));
document.querySelectorAll("[data-capture]").forEach((button) => {
  button.addEventListener("click", () => startCapture(button.dataset.capture));
});
document.querySelectorAll("[data-clear]").forEach((button) => {
  button.addEventListener("click", () => {
    const action = button.dataset.clear;
    shortcutDraft[action] = { display: "", accelerator: null };
    testedAction[action] = false;
    renderCapture(action, null);
    renderSingleCards();
    void shortcutInvoke("set_shortcut", { action, display: "", accelerator: null }).catch(() => {});
  });
});
let setupBgRunning = false;

function setupBgDraw(now) {
  const canvas = document.querySelector("#setup-bg");
  if (!canvas) return;
  const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
  const w = window.innerWidth;
  const hgt = window.innerHeight;
  if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(hgt * dpr)) {
    canvas.width = Math.round(w * dpr);
    canvas.height = Math.round(hgt * dpr);
  }
  const ctx = canvas.getContext("2d");
  if (!ctx) return;
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, w, hgt);
  const t = now / 1000;
  const ribbons = [
    { y: 0.40, a: 44, f: 0.0062, sp: 0.35, r: 2.1, al: 0.20 },
    { y: 0.55, a: 68, f: 0.0041, sp: -0.22, r: 1.6, al: 0.12 },
    { y: 0.48, a: 24, f: 0.0110, sp: 0.50, r: 1.2, al: 0.10 },
  ];
  for (const ribbon of ribbons) {
    for (let x = -20; x < w + 20; x += 9) {
      const y = hgt * ribbon.y
        + Math.sin(x * ribbon.f + t * ribbon.sp) * ribbon.a
        + Math.sin(x * ribbon.f * 2.7 + t * ribbon.sp * 1.6) * ribbon.a * 0.35;
      const edge = Math.abs(y - hgt * ribbon.y) / (ribbon.a * 1.4);
      const alpha = Math.max(0.02, ribbon.al * (1 - edge));
      ctx.beginPath();
      ctx.fillStyle = "rgba(23, 24, 27, " + alpha.toFixed(3) + ")";
      ctx.arc(x, y, ribbon.r, 0, 6.2832);
      ctx.fill();
    }
  }
}

function setupBgLoop(now) {
  setupBgDraw(now);
  if (setupBgRunning) requestAnimationFrame(setupBgLoop);
}

function setupBgControl() {
  const canvas = document.querySelector("#setup-bg");
  const reduce = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  const page = document.querySelector("#page-setup");
  const active = document.body.classList.contains("in-setup") && !!page && !page.hidden;
  if (canvas) canvas.hidden = !active;
  if (!active || reduce) setupBgRunning = false;
  if (active && !reduce && !setupBgRunning) {
    setupBgRunning = true;
    requestAnimationFrame(setupBgLoop);
  }
  if (active && reduce) setupBgDraw(1200);
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) setupBgRunning = false;
  else void setupBgControl();
});
console.timeStamp("wicx: frontend boot calls issued");
void bootShortcuts();

