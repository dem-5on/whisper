import { getCurrentWindow } from "@tauri-apps/api/window";
import { invoke } from "@tauri-apps/api/core";
import "./keys.css";

const SOCKET_URL = "ws://127.0.0.1:47651";
const PROVIDERS = {
  openai: { name: "OpenAI", field: "has_openai_key" },
  groq: { name: "Groq", field: "has_groq_key" },
  openrouter: { name: "OpenRouter", field: "has_openrouter_key" },
  "hosted-whisper": { name: "Hosted Whisper service", field: "has_whisper_service_token" },
};

const ui = {
  provider: document.querySelector("#key-provider"),
  input: document.querySelector("#key-value"),
  state: document.querySelector("#key-state"),
  feedback: document.querySelector("#key-feedback"),
  form: document.querySelector("#key-form"),
  clear: document.querySelector("#clear-key"),
  liveEngine: document.querySelector("#live-engine"),
  liveUrlRow: document.querySelector("#live-url-row"),
  liveUrl: document.querySelector("#live-server-url"),
  liveModel: document.querySelector("#live-model"),
  saveLive: document.querySelector("#save-live-settings"),
};

let connection;
let retryDelay = 500;
let sequence = 0;
let currentStatus = {};
let liveSettingsDirty = false;
const pending = new Map();

function selectedProvider() {
  return PROVIDERS[ui.provider.value];
}

function renderKeyStatus() {
  const provider = selectedProvider();
  const hasKey = Boolean(currentStatus[provider.field]);
  ui.state.textContent = hasKey
    ? `A key is saved for ${provider.name}. The key itself is never shown here.`
    : `No key is saved for ${provider.name}.`;
  ui.state.dataset.configured = String(hasKey);
  ui.clear.hidden = !hasKey;
}

function setFeedback(message, isError = false) {
  ui.feedback.textContent = message;
  ui.feedback.dataset.error = String(isError);
}

async function connect() {
  ui.state.textContent = "Connecting to Whisper…";
  let socketUrl = SOCKET_URL;
  try {
    const token = await invoke("websocket_token");
    if (token) socketUrl += `?token=${encodeURIComponent(token)}`;
  } catch {
    // The development daemon has no launch token; packaged builds do.
  }
  connection = new WebSocket(socketUrl);
  connection.addEventListener("open", () => {
    retryDelay = 500;
    request("status").then(updateStatus).catch((error) => setFeedback(error.message, true));
  });
  connection.addEventListener("message", ({ data }) => {
    let message;
    try { message = JSON.parse(data); } catch { return; }
    if (message.request_id != null && pending.has(message.request_id)) {
      const waiter = pending.get(message.request_id);
      pending.delete(message.request_id);
      clearTimeout(waiter.timeout);
      if (message.ok === "false") waiter.reject(new Error(message.error || "Could not update the key"));
      else waiter.resolve(message);
    }
    if (message.type === "status") updateStatus(message.status);
  });
  connection.addEventListener("close", () => {
    for (const waiter of pending.values()) {
      clearTimeout(waiter.timeout);
      waiter.reject(new Error("Whisper daemon disconnected"));
    }
    pending.clear();
    ui.state.textContent = "Whisper is unavailable. Reconnecting…";
    window.setTimeout(connect, retryDelay);
    retryDelay = Math.min(5000, retryDelay * 1.7);
  });
  connection.addEventListener("error", () => connection.close());
}

function request(command, parameters = {}) {
  if (!connection || connection.readyState !== WebSocket.OPEN) {
    return Promise.reject(new Error("Whisper daemon is unavailable"));
  }
  const requestId = ++sequence;
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      pending.delete(requestId);
      reject(new Error("Whisper did not respond in time"));
    }, 10000);
    pending.set(requestId, { resolve, reject, timeout });
    connection.send(JSON.stringify({ command, request_id: requestId, ...parameters }));
  });
}

function updateStatus(status) {
  if (!status || typeof status !== "object") return;
  currentStatus = { ...currentStatus, ...status };
  renderKeyStatus();
  if (!liveSettingsDirty) {
    ui.liveEngine.value = currentStatus.live_engine || "local";
    ui.liveUrl.value = currentStatus.live_server_url || "";
    ui.liveModel.value = currentStatus.live_model || "base";
  }
  renderLiveFields();
}

function renderLiveFields() {
  const engine = ui.liveEngine.value;
  ui.liveUrlRow.hidden = engine !== "hosted-whisper";
  ui.liveModel.placeholder = engine === "hosted-whisper"
    ? "default"
    : engine === "openai-realtime" ? "gpt-live-transcribe" : "base";
}

function chooseDefaultLiveModel() {
  const supported = {
    local: ["tiny", "base", "small", "medium", "large-v3", "turbo"],
    "openai-realtime": ["gpt-live-transcribe", "gpt-transcribe"],
  }[ui.liveEngine.value];
  if (ui.liveEngine.value === "hosted-whisper") {
    if (!ui.liveModel.value.trim()) ui.liveModel.value = "default";
  } else if (!supported.includes(ui.liveModel.value.trim())) {
    ui.liveModel.value = ui.liveEngine.value === "local" ? "base" : "gpt-live-transcribe";
  }
}

ui.provider.addEventListener("change", () => {
  ui.input.value = "";
  ui.feedback.textContent = "";
  renderKeyStatus();
});

ui.liveEngine.addEventListener("change", () => {
  liveSettingsDirty = true;
  chooseDefaultLiveModel();
  renderLiveFields();
});
ui.liveUrl.addEventListener("input", () => { liveSettingsDirty = true; });
ui.liveModel.addEventListener("input", () => { liveSettingsDirty = true; });

ui.saveLive.addEventListener("click", async () => {
  const engine = ui.liveEngine.value;
  chooseDefaultLiveModel();
  const model = ui.liveModel.value.trim();
  if (!model) {
    setFeedback("Enter a live model or hosted profile.", true);
    return;
  }
  try {
    const settings = { engine, server_url: ui.liveUrl.value.trim() || currentStatus.live_server_url || "" };
    let status = await request("set_live_engine", settings);
    if (status.ok === "false") throw new Error(status.error || "Could not save live engine settings");
    status = await request("set_live_model", { model });
    if (status.ok === "false") throw new Error(status.error || "Could not save live model settings");
    liveSettingsDirty = false;
    updateStatus(status);
    setFeedback("Live transcription settings saved.");
  } catch (error) {
    setFeedback(error.message, true);
  }
});

ui.form.addEventListener("submit", async (event) => {
  event.preventDefault();
  const key = ui.input.value.trim();
  if (!key) {
    setFeedback("Enter a key before saving.", true);
    return;
  }
  try {
    const status = await request("set_key", { provider: ui.provider.value, key });
    updateStatus(status);
    ui.input.value = "";
    setFeedback(`Key saved for ${selectedProvider().name}.`);
  } catch (error) {
    setFeedback(error.message, true);
  }
});

ui.clear.addEventListener("click", async () => {
  try {
    const status = await request("set_key", { provider: ui.provider.value, clear: true });
    updateStatus(status);
    ui.input.value = "";
    setFeedback(`Saved key removed for ${selectedProvider().name}.`);
  } catch (error) {
    setFeedback(error.message, true);
  }
});

document.querySelector("#open-diagnostics").addEventListener("click", async () => {
  try {
    await invoke("open_diagnostics_folder");
    setFeedback("Opened Whisper's diagnostics folder.");
  } catch (error) {
    setFeedback(error.message, true);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") getCurrentWindow().hide();
});

connect();
