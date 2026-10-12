import { getCurrentWindow } from "@tauri-apps/api/window";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./keys.css";

const SOCKET_URL = "ws://127.0.0.1:47651";
const PROVIDERS = {
  openai: { name: "OpenAI", field: "has_openai_key" },
  groq: { name: "Groq", field: "has_groq_key" },
  openrouter: { name: "OpenRouter", field: "has_openrouter_key" },
  "hosted-whisper": { name: "Hosted transcription service", field: "has_whisper_service_token" },
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
  authUrl: document.querySelector("#auth-api-url"),
  saveAuthUrl: document.querySelector("#save-auth-api-url"),
  accountState: document.querySelector("#account-state"),
  signOut: document.querySelector("#sign-out"),
  oauthButtons: [...document.querySelectorAll("[data-oauth-provider]")],
  billingState: document.querySelector("#billing-state"),
  billingFeatures: document.querySelector("#billing-features"),
  billingPlan: document.querySelector("#billing-plan"),
  refreshBilling: document.querySelector("#refresh-billing"),
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

function setAccountState(message, isError = false) {
  ui.accountState.textContent = message;
  ui.accountState.dataset.error = String(isError);
}

async function refreshAccount() {
  try {
    const [url, signedIn] = await Promise.all([
      invoke("get_auth_api_url"),
      invoke("auth_is_signed_in"),
    ]);
    ui.authUrl.value = url || "";
    ui.signOut.hidden = !signedIn;
    ui.oauthButtons.forEach((button) => { button.hidden = Boolean(signedIn); });
    setAccountState(signedIn ? "You are signed in to your Wicx account." : "Not signed in. Sign in to unlock subscription features.");
    if (signedIn) await refreshBilling();
    else clearBilling("Sign in to check your subscription.");
  } catch (error) {
    setAccountState(error.message || "Could not load account settings.", true);
  }
}

function clearBilling(message, isError = false) {
  ui.billingState.textContent = message;
  ui.billingState.dataset.error = String(isError);
  ui.billingFeatures.replaceChildren();
  ui.billingPlan.textContent = "";
}

async function refreshBilling() {
  ui.refreshBilling.disabled = true;
  clearBilling("Checking subscription access…");
  try {
    const status = await invoke("fetch_billing_status");
    ui.billingFeatures.replaceChildren();
    const features = Object.entries(status.features || {});
    for (const [feature, enabled] of features) {
      const item = document.createElement("li");
      item.dataset.enabled = String(enabled);
      const label = feature.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
      item.textContent = `${label}: ${enabled ? "Included" : "Not included"}`;
      ui.billingFeatures.append(item);
    }
    const subscriptions = Array.isArray(status.subscriptions) ? status.subscriptions : [];
    const plans = subscriptions.map((subscription) => {
      const name = String(subscription.plan_id || "Plan").replaceAll("-", " ");
      const state = String(subscription.status || "unknown");
      return `${name} (${state})`;
    });
    ui.billingPlan.textContent = plans.length ? `Plans: ${plans.join(", ")}` : "No subscription is currently on this account.";
    ui.billingState.textContent = features.some(([, enabled]) => enabled)
      ? "Subscriber features are available on this account."
      : "Free account. Local dictation remains available.";
    ui.billingState.dataset.error = "false";
  } catch (error) {
    clearBilling(error.message || "Could not check subscription access.", true);
  } finally {
    ui.refreshBilling.disabled = false;
  }
}

async function finishOAuthCallback() {
  try {
    const completed = await invoke("complete_auth_callback");
    if (completed) {
      await refreshAccount();
      setAccountState("Sign-in complete. Your Wicx account is ready.");
    }
  } catch (error) {
    setAccountState(error.message || "Sign-in could not be completed.", true);
  }
}

ui.saveAuthUrl.addEventListener("click", async () => {
  try {
    const origin = await invoke("save_auth_api_url", { url: ui.authUrl.value });
    ui.authUrl.value = origin;
    setAccountState("Wicx service URL saved.");
    if (await invoke("auth_is_signed_in")) await refreshBilling();
  } catch (error) {
    setAccountState(error.message || "Could not save the service URL.", true);
  }
});

ui.oauthButtons.forEach((button) => {
  button.addEventListener("click", async () => {
    const provider = button.dataset.oauthProvider;
    setAccountState(`Opening ${provider === "google" ? "Google" : "Apple"} sign-in…`);
    try {
      await invoke("start_oauth", { provider });
      setAccountState("Finish signing in in your browser. Wicx will return here automatically.");
    } catch (error) {
      setAccountState(error.message || "Could not start sign-in.", true);
    }
  });
});

ui.signOut.addEventListener("click", async () => {
  try {
    await invoke("sign_out");
    await refreshAccount();
    setAccountState("You have been signed out.");
  } catch (error) {
    setAccountState(error.message || "Could not sign out.", true);
  }
});

ui.refreshBilling.addEventListener("click", refreshBilling);

async function connect() {
  ui.state.textContent = "Connecting to Wicx…";
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
      waiter.reject(new Error("Wicx transcription service disconnected"));
    }
    pending.clear();
    ui.state.textContent = "Wicx is unavailable. Reconnecting…";
    window.setTimeout(connect, retryDelay);
    retryDelay = Math.min(5000, retryDelay * 1.7);
  });
  connection.addEventListener("error", () => connection.close());
}

function request(command, parameters = {}) {
  if (!connection || connection.readyState !== WebSocket.OPEN) {
    return Promise.reject(new Error("Wicx transcription service is unavailable"));
  }
  const requestId = ++sequence;
  return new Promise((resolve, reject) => {
    const timeout = window.setTimeout(() => {
      pending.delete(requestId);
      reject(new Error("Wicx did not respond in time"));
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
    setFeedback("Opened Wicx's diagnostics folder.");
  } catch (error) {
    setFeedback(error.message, true);
  }
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") getCurrentWindow().hide();
});

listen("auth-callback-received", finishOAuthCallback)
  .then(() => finishOAuthCallback())
  .catch(() => {});
refreshAccount();
connect();
