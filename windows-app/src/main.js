import { getCurrentWindow } from "@tauri-apps/api/window";
import { listen } from "@tauri-apps/api/event";
import { invoke } from "@tauri-apps/api/core";
import { writeText } from "@tauri-apps/plugin-clipboard-manager";
import { deriveControlState } from "./control-state.js";
import { microphoneLabel } from "./display.js";
import "./style.css";

const SOCKET_URL = "ws://127.0.0.1:47651";
const HOTKEY_PENDING_WINDOW_MS = 2000;
let focusTestBuild = true;
const PROVIDERS = [
  ["local", "Local", "Runs transcription on this computer"],
  ["groq", "Groq", "Requires a Groq API key"],
  ["openrouter", "OpenRouter", "Requires an OpenRouter API key"],
  ["openai", "OpenAI", "Requires an OpenAI API key"],
];
const SVG_NS = "http://www.w3.org/2000/svg";
const ICON_PATHS = {
  mic: [
    ["rect", { x: "9", y: "3", width: "6", height: "12", rx: "3" }],
    ["path", { d: "M5 11a7 7 0 0 0 14 0M12 18v3m-4 0h8" }],
  ],
  stop: [["rect", { x: "5", y: "5", width: "14", height: "14", rx: "3" }]],
  cancel: [["path", { d: "M9 3h6l6 6v6l-6 6H9l-6-6V9z" }]],
  copy: [
    ["rect", { x: "8", y: "8", width: "12", height: "13", rx: "1" }],
    ["path", { d: "M16 5V3H4v14h2" }],
  ],
  provider: [
    ["rect", { x: "3", y: "3", width: "18", height: "5", rx: "1" }],
    ["rect", { x: "3", y: "10", width: "18", height: "5", rx: "1" }],
    ["rect", { x: "3", y: "17", width: "18", height: "4", rx: "1" }],
    ["path", { d: "M7 5.5h.01M7 12.5h.01M7 19h.01" }],
  ],
  model: [["path", { d: "M9 3h6m-5 0v6l-5 9a2 2 0 0 0 2 3h10a2 2 0 0 0 2-3l-5-9V3M8 15h8" }]],
};

function createIcon(name, className = "icon") {
  const svg = document.createElementNS(SVG_NS, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  for (const [tag, attributes] of ICON_PATHS[name] || []) {
    const shape = document.createElementNS(SVG_NS, tag);
    for (const [key, value] of Object.entries(attributes)) shape.setAttribute(key, value);
    svg.append(shape);
  }
  return svg;
}

function setActionContent(button, iconName, label) {
  button.replaceChildren(createIcon(iconName), document.createTextNode(label));
}

const ui = {
  connection: document.querySelector("#connection"),
  focusResult: document.querySelector("#focus-result"),
  error: document.querySelector("#error"),
  toggle: document.querySelector("#toggle"),
  cancel: document.querySelector("#cancel"),
  copy: document.querySelector("#copy"),
  retry: document.querySelector("#retry"),
  transcript: document.querySelector("#transcript"),
  chooser: document.querySelector("#chooser"),
  chooserTitle: document.querySelector("#chooser-title"),
  chooserIcon: document.querySelector("#chooser-icon"),
  chooserOptions: document.querySelector("#chooser-options"),
  manageKeys: document.querySelector("#manage-keys"),
  streaming: document.querySelector("#streaming-toggle"),
  liveNote: document.querySelector("#live-note"),
  liveTranscript: document.querySelector("#live-transcript"),
  shortcutNote: document.querySelector("#shortcut-note"),
  hint: document.querySelector("#hint"),
};

let currentStatus = null;
let currentChooser = null;
let reconnectDelay = 400;
let requestSequence = 0;
let connection = null;
let trayVisualState = null;
let microphoneSources = [];
let pendingHotkeyToggles = 0;
let pendingHotkeyTimer = null;
const pending = new Map();

function setConnection(text, connected = false) {
  ui.connection.replaceChildren();
  const dot = document.createElement("span");
  dot.className = "state-dot";
  ui.connection.append(dot, document.createTextNode(text));
  ui.connection.dataset.connected = String(connected);
  const controls = deriveControlState(currentStatus, connected, focusTestBuild);
  ui.toggle.disabled = controls.toggleDisabled;
  ui.cancel.disabled = controls.cancelDisabled;
  ui.copy.disabled = controls.copyDisabled;
  ui.retry.disabled = controls.retryDisabled;
  ui.streaming.disabled = controls.streamingDisabled;
  document.querySelectorAll(".tile[data-setting]").forEach((tile) => {
    tile.disabled = controls.settingsDisabled;
  });
}

function showError(message = "") {
  ui.error.textContent = message;
  ui.error.hidden = !message;
  ui.error.dataset.success = "false";
}

function showFeedback(message, success = false) {
  showError(message);
  ui.error.dataset.success = String(success);
}

async function loadBuildMode() {
  try {
    focusTestBuild = await invoke("focus_test_build");
  } catch {
    // Fail closed outside the Tauri shell or if the build-mode query fails.
    focusTestBuild = true;
  }
  ui.hint.hidden = !focusTestBuild;
  ui.focusResult.hidden = !focusTestBuild;
  if (focusTestBuild) {
    ui.shortcutNote.hidden = true;
  } else {
    try {
      const shortcutStatus = await invoke("recording_shortcut_status");
      const messages = {
        available: "Ctrl+Alt+R · Start or stop recording",
        unavailable: "Ctrl+Alt+R is unavailable because another app is using it",
        development: "Ctrl+Alt+R is enabled in the installed app",
      };
      ui.shortcutNote.textContent = messages[shortcutStatus] || "";
      ui.shortcutNote.hidden = !ui.shortcutNote.textContent;
    } catch {
      ui.shortcutNote.hidden = true;
    }
  }
  document.querySelectorAll(".tile[data-setting]").forEach((tile) => {
    tile.disabled = focusTestBuild;
  });
  if (currentStatus) updateStatus(currentStatus);
}

function updateStatus(status) {
  if (!status || typeof status !== "object") return;
  currentStatus = { ...currentStatus, ...status };
  const active = connection?.readyState === WebSocket.OPEN;
  const state = String(currentStatus.state || "IDLE");
  const recording = state === "RECORDING";
  const busy = ["PROCESSING", "DELIVERING"].includes(state);
  const controls = deriveControlState(currentStatus, active, focusTestBuild);
  const nextTrayVisualState = recording ? "recording" : busy ? "processing" : state === "ERROR" ? "error" : "idle";
  if (nextTrayVisualState !== trayVisualState) {
    trayVisualState = nextTrayVisualState;
    invoke("set_tray_state", { state: nextTrayVisualState }).catch(() => {});
  }
  const label = recording
    ? `Recording · ${Number(currentStatus.recording_elapsed || 0).toFixed(1)}s`
    : busy
      ? "Transcribing…"
      : state === "ERROR"
        ? "Needs attention"
        : "Ready";
  setConnection(label, active);
  ui.connection.dataset.recording = String(recording);
  setActionContent(ui.toggle, recording ? "stop" : "mic", recording
    ? "Stop recording"
    : state === "ERROR" ? "Try again" : "Start recording");
  ui.toggle.disabled = controls.toggleDisabled;
  ui.cancel.disabled = controls.cancelDisabled;
  document.querySelector("#provider-value").textContent = providerLabel(currentStatus.backend);
  document.querySelector("#model-value").textContent = currentStatus.model || "base";
  document.querySelector("#mic-value").textContent = microphoneLabel(currentStatus.mic, microphoneSources);
  ui.retry.disabled = controls.retryDisabled;
  ui.copy.disabled = controls.copyDisabled;
  ui.streaming.checked = currentStatus.streaming !== false;
  ui.streaming.disabled = controls.streamingDisabled;
  const providerBlocksLive = ["groq", "openrouter"].includes(currentStatus.backend)
    && currentStatus.live_engine !== "hosted-whisper";
  ui.liveNote.textContent = providerBlocksLive
    ? `${providerLabel(currentStatus.backend)} is batch-only. Select Local or OpenAI for live transcription.`
    : currentStatus.streaming !== false && !currentStatus.live_available
      ? currentStatus.live_unavailable_reason || "Live transcription is unavailable with the current settings."
      : "";
  ui.liveNote.hidden = !ui.liveNote.textContent;
  const liveText = String(currentStatus.live_text || "").trim();
  if (recording) {
    ui.liveTranscript.textContent = liveText || (currentStatus.live_decode_error
      ? `Live decode retrying: ${currentStatus.live_decode_error}`
      : currentStatus.streaming !== false && !currentStatus.live_available
        ? currentStatus.live_unavailable_reason || "Live partials unavailable."
        : "Listening… press Stop recording when done.");
    ui.liveTranscript.hidden = false;
  } else if (busy) {
    ui.liveTranscript.textContent = liveText ? `Finalizing: ${liveText}` : "Transcribing… full text appears here when done.";
    ui.liveTranscript.hidden = false;
  } else {
    ui.liveTranscript.textContent = "";
    ui.liveTranscript.hidden = true;
  }
  ui.transcript.textContent = currentStatus.last_preview || "";
  ui.transcript.hidden = !currentStatus.last_preview;
  showError(currentStatus.error || "");
}

function providerLabel(provider) {
  return PROVIDERS.find(([id]) => id === provider)?.[1] || provider || "Local";
}

function updateMicrophoneSources(sources) {
  microphoneSources = Array.isArray(sources) ? sources : [];
  if (currentStatus) {
    document.querySelector("#mic-value").textContent = microphoneLabel(currentStatus.mic, microphoneSources);
  }
}

async function connect() {
  let socketUrl = SOCKET_URL;
  try {
    const token = await invoke("websocket_token");
    if (token) socketUrl += `?token=${encodeURIComponent(token)}`;
  } catch {
    // The development daemon has no launch token; packaged builds do.
  }
  connection = new WebSocket(socketUrl);
  setConnection("Connecting to Whisper…");
  connection.addEventListener("open", () => {
    reconnectDelay = 400;
    setConnection("Connected", true);
    request("status").catch(() => {});
    request("subscribe", {
      since: Number(currentStatus?.live_revision || 0),
      session_id: currentStatus?.session_id || undefined,
    }).catch(() => {});
    request("mics").then((response) => updateMicrophoneSources(response.mics)).catch(() => {});
    flushPendingHotkeys();
  });
  connection.addEventListener("message", ({ data }) => {
    let message;
    try { message = JSON.parse(data); } catch { return; }
    if (message.request_id != null) {
      const waiter = pending.get(message.request_id);
      if (waiter) {
        pending.delete(message.request_id);
        clearTimeout(waiter.timeout);
        if (message.ok === "false") waiter.reject(new Error(message.error || "Whisper command failed"));
        else waiter.resolve(message);
      }
    }
    if (message.type === "status") updateStatus(message.status);
    if (message.type === "daemon_event") {
      const event = message.event || {};
      if (event.type === "final" && event.text) {
        currentStatus = { ...currentStatus, last_preview: event.text };
        updateStatus(currentStatus);
      }
    }
    if (message.state) updateStatus(message);
  });
  connection.addEventListener("close", () => {
    for (const waiter of pending.values()) {
      clearTimeout(waiter.timeout);
      waiter.reject(new Error("Whisper daemon disconnected"));
    }
    pending.clear();
    setConnection("Whisper daemon unavailable");
    window.setTimeout(connect, reconnectDelay);
    reconnectDelay = Math.min(5000, reconnectDelay * 1.7);
  });
  connection.addEventListener("error", () => connection.close());
}

function request(command, parameters = {}) {
  if (!connection || connection.readyState !== WebSocket.OPEN) {
    return Promise.reject(new Error("Whisper daemon is unavailable"));
  }
  const requestId = ++requestSequence;
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      pending.delete(requestId);
      reject(new Error("Whisper did not respond in time"));
    }, 10000);
    pending.set(requestId, { resolve, reject, timeout });
    connection.send(JSON.stringify({ command, request_id: requestId, ...parameters }));
  });
}

async function sendCommandAndHide(command) {
  if (!connection || connection.readyState !== WebSocket.OPEN) {
    showError("Whisper daemon is unavailable");
    return;
  }
  const pendingRequest = request(command);
  try {
    await getCurrentWindow().hide();
    updateStatus(await pendingRequest);
  } catch (error) {
    showError(error.message);
  }
}

const toggleRecording = () => sendCommandAndHide("toggle");
const cancelRecording = () => sendCommandAndHide("cancel");

function queuePendingHotkey() {
  pendingHotkeyToggles = (pendingHotkeyToggles + 1) % 2;
  window.clearTimeout(pendingHotkeyTimer);
  pendingHotkeyTimer = null;
  if (pendingHotkeyToggles) {
    pendingHotkeyTimer = window.setTimeout(() => {
      pendingHotkeyToggles = 0;
      pendingHotkeyTimer = null;
    }, HOTKEY_PENDING_WINDOW_MS);
  }
}

function flushPendingHotkeys() {
  if (pendingHotkeyToggles === 0 || connection?.readyState !== WebSocket.OPEN) return;
  const shouldToggle = pendingHotkeyToggles === 1;
  pendingHotkeyToggles = 0;
  window.clearTimeout(pendingHotkeyTimer);
  pendingHotkeyTimer = null;
  if (shouldToggle) void toggleRecording();
}

function closeChooser() {
  const wasOpen = currentChooser !== null;
  currentChooser = null;
  ui.chooser.hidden = true;
  ui.manageKeys.hidden = true;
  document.querySelectorAll(".tile").forEach((tile) => tile.setAttribute("aria-expanded", "false"));
  if (wasOpen) invoke("resize_panel", { expanded: false }).catch(showError);
}

function renderChoices(kind, title, icon, choices, selected, onSelect) {
  currentChooser = kind;
  ui.manageKeys.hidden = kind !== "provider" || focusTestBuild;
  ui.chooserTitle.textContent = title;
  ui.chooserIcon.replaceChildren(createIcon(icon, "icon chooser-symbol"));
  ui.chooserOptions.replaceChildren();
  for (const [index, choice] of choices.entries()) {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "choice";
    button.setAttribute("role", "radio");
    button.setAttribute("aria-checked", String(choice.id === selected));
    button.tabIndex = choice.id === selected || (!choices.some((item) => item.id === selected) && index === 0) ? 0 : -1;
    const copy = document.createElement("span");
    copy.className = "choice-copy";
    copy.textContent = choice.label;
    if (choice.detail) {
      const detail = document.createElement("small");
      detail.className = "choice-detail";
      detail.textContent = choice.detail;
      copy.append(document.createElement("br"), detail);
    }
    button.append(copy);
    if (choice.id === selected) {
      const mark = document.createElement("span");
      mark.className = "choice-mark";
      mark.textContent = "✓";
      button.append(mark);
    }
    button.addEventListener("click", async () => {
      try {
        updateStatus(await onSelect(choice));
        closeChooser();
      } catch (error) {
        showError(error.message);
      }
    });
    ui.chooserOptions.append(button);
  }
  ui.chooser.hidden = false;
  document.querySelectorAll(".tile").forEach((tile) => {
    tile.setAttribute("aria-expanded", String(tile.dataset.setting === currentChooser));
  });
  invoke("resize_panel", { expanded: true }).catch(showError);
}

ui.chooserOptions.addEventListener("keydown", (event) => {
  if (!["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) return;
  const options = [...ui.chooserOptions.querySelectorAll('[role="radio"]')];
  if (!options.length) return;
  event.preventDefault();
  const current = options.indexOf(document.activeElement);
  const next = event.key === "Home" ? 0
    : event.key === "End" ? options.length - 1
      : (current + (event.key === "ArrowDown" ? 1 : -1) + options.length) % options.length;
  options.forEach((option, index) => { option.tabIndex = index === next ? 0 : -1; });
  options[next].focus();
  options[next].click();
});

async function openChooser(kind) {
  if (currentChooser === kind) { closeChooser(); return; }
  try {
    if (kind === "provider") {
      renderChoices("provider", "Choose a provider", "provider", PROVIDERS.map(([id, label, detail]) => ({ id, label, detail })),
        currentStatus?.backend || "local", (choice) => request("set_backend", { backend: choice.id }));
    } else if (kind === "model") {
      const response = await request("models");
      renderChoices("model", "Choose a transcription model", "model", (response.models || []).map((model) => ({ id: model.id, label: model.id })),
        currentStatus?.model, (choice) => request("set_model", { model: choice.id }));
    } else {
      const response = await request("mics");
      updateMicrophoneSources(response.mics);
      const choices = [{ id: "default", label: "Default microphone" }, ...microphoneSources.map((mic) => ({ id: mic.id, label: mic.name }))];
      renderChoices("mic", "Choose a microphone", "mic", choices, currentStatus?.mic || "default",
        (choice) => request("set_mic", { device: choice.id === "default" ? "" : choice.id }));
    }
  } catch (error) {
    showError(error.message);
  }
}

ui.toggle.addEventListener("click", toggleRecording);
setActionContent(ui.toggle, "mic", "Start recording");
setActionContent(ui.cancel, "cancel", "Cancel");
setActionContent(ui.copy, "copy", "Copy transcript");
document.querySelectorAll(".tile-icon[data-icon]").forEach((element) => {
  element.append(createIcon(element.dataset.icon, "icon tile-symbol"));
});
ui.cancel.addEventListener("click", cancelRecording);
ui.streaming.addEventListener("change", async () => {
  try {
    updateStatus(await request("set_streaming", { enabled: ui.streaming.checked }));
  } catch (error) {
    ui.streaming.checked = currentStatus?.streaming !== false;
    showError(error.message);
  }
});
ui.copy.addEventListener("click", async () => {
  try {
    const response = await request("last");
    if (!response.transcript) throw new Error("No previous transcript to copy");
    await writeText(response.transcript);
    showFeedback("Transcript copied to clipboard.", true);
  } catch (error) {
    showFeedback(error.message);
  }
});
ui.retry.addEventListener("click", () => sendCommandAndHide("retry"));
document.querySelectorAll(".tile").forEach((tile) => {
  tile.disabled = true;
  tile.addEventListener("click", () => openChooser(tile.dataset.setting));
});
document.querySelector("#close").addEventListener("click", () => getCurrentWindow().hide());
ui.manageKeys.addEventListener("click", async () => {
  try {
    await invoke("show_key_settings");
  } catch (error) {
    showError(error.message);
  }
});
listen("panel-shown", closeChooser);
listen("daemon-process", ({ payload }) => {
  if (typeof payload !== "string") return;
  const couldNotStart = payload.startsWith("unavailable:");
  const stopped = payload.startsWith("stopped:");
  if (!couldNotStart && !stopped) return;
  const prefix = couldNotStart ? "unavailable:" : "stopped:";
  const reason = payload.slice(prefix.length).trim();
  showError(couldNotStart
    ? `Whisper could not start its transcription service${reason ? `: ${reason}` : ". It will retry automatically."}`
    : `Whisper's transcription service stopped unexpectedly${reason ? ` (${reason})` : ""}. It will restart automatically.`);
});
listen("focus-check", ({ payload }) => {
  if (!focusTestBuild) return;
  const preserved = payload === true;
  ui.focusResult.textContent = preserved
    ? "Panel visible · previous app remains active. Now verify text entry."
    : "Focus check failed · panel hidden or another window took focus. Please report it.";
  ui.focusResult.dataset.preserved = String(preserved);
  ui.focusResult.hidden = false;
});
listen("recording-hotkey", () => {
  if (focusTestBuild) return;
  if (connection?.readyState !== WebSocket.OPEN) {
    queuePendingHotkey();
    return;
  }
  void toggleRecording();
});
listen("cancel-recording", () => {
  if (!focusTestBuild) void cancelRecording();
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") closeChooser();
});
window.setInterval(() => {
  if (currentStatus?.state === "RECORDING") {
    currentStatus.recording_elapsed = Number(currentStatus.recording_elapsed || 0) + 0.25;
    updateStatus(currentStatus);
  }
}, 250);

connect();
loadBuildMode();
