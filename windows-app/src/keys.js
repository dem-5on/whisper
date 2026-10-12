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
  loadPlans: document.querySelector("#load-plans"),
  plansState: document.querySelector("#plans-state"),
  billingPlans: document.querySelector("#billing-plans"),
  checkoutForm: document.querySelector("#checkout-form"),
  checkoutEmail: document.querySelector("#checkout-email"),
  startCheckout: document.querySelector("#start-checkout"),
  cancelCheckout: document.querySelector("#cancel-checkout"),
};

let connection;
let retryDelay = 500;
let sequence = 0;
let currentStatus = {};
let liveSettingsDirty = false;
const pending = new Map();
let accountSignedIn = false;
let selectedPlanId = "";
let cachedBillingPlans = [];

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
    accountSignedIn = Boolean(signedIn);
    ui.signOut.hidden = !signedIn;
    ui.oauthButtons.forEach((button) => { button.hidden = Boolean(signedIn); });
    setAccountState(signedIn ? "You are signed in to your Wicx account." : "Not signed in. Sign in to unlock subscription features.");
    if (signedIn) await refreshBilling();
    else clearBilling("Sign in to check your subscription.");
    if (ui.billingPlans.childElementCount > 0) await refreshPlans();
  } catch (error) {
    setAccountState(error.message || "Could not load account settings.", true);
  }
}

async function refreshPlans() {
  ui.loadPlans.disabled = true;
  ui.plansState.textContent = "Loading plans…";
  ui.plansState.dataset.error = "false";
  ui.billingPlans.replaceChildren();
  try {
    const result = await invoke("fetch_billing_plans");
    const plans = Array.isArray(result.plans) ? result.plans : [];
    cachedBillingPlans = plans;
    if (plans.length === 0) {
      ui.plansState.textContent = "No plans are currently available.";
      return;
    }
    ui.plansState.textContent = "Pricing and features are supplied by your Wicx service.";
    for (const plan of plans) {
      const card = document.createElement("article");
      card.className = "plan-card";
      const copy = document.createElement("div");
      copy.className = "plan-card-copy";
      const name = document.createElement("h4");
      name.className = "plan-card-name";
      name.textContent = String(plan.plan_id).replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
      const price = document.createElement("p");
      price.className = "plan-card-price";
      const amount = Number(plan.amount_minor) / 100;
      const paymentTerms = plan.billing_mode === "one_time"
        ? `one-time${plan.period_days ? ` / ${plan.period_days} days` : ""}`
        : "recurring";
      const channels = Array.isArray(plan.channels) && plan.channels.length
        ? ` · ${plan.channels.map((channel) => String(channel).replaceAll("_", " ")).join(" / ")}`
        : "";
      price.textContent = `${new Intl.NumberFormat(undefined, { style: "currency", currency: plan.currency }).format(amount)} · ${paymentTerms}${channels}`;
      const features = document.createElement("p");
      features.className = "plan-card-features";
      features.textContent = Array.isArray(plan.features)
        ? `Includes ${plan.features.map((feature) => String(feature).replaceAll("_", " ")).join(" · ")}`
        : "Subscription features included";
      copy.append(name, price, features);
      const choose = document.createElement("button");
      choose.className = "key-save";
      choose.type = "button";
      choose.textContent = accountSignedIn ? "Choose" : "Sign in first";
      choose.disabled = !accountSignedIn;
      choose.addEventListener("click", () => {
        selectedPlanId = String(plan.plan_id);
        ui.checkoutForm.hidden = false;
        ui.checkoutEmail.focus();
        ui.plansState.textContent = `Selected ${name.textContent}. Enter a billing email to continue.`;
      });
      card.append(copy, choose);
      ui.billingPlans.append(card);
    }
  } catch (error) {
    ui.plansState.textContent = error.message || "Could not load plans.";
    ui.plansState.dataset.error = "true";
  } finally {
    ui.loadPlans.disabled = false;
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
    try {
      const catalog = await invoke("fetch_billing_plans");
      cachedBillingPlans = Array.isArray(catalog.plans) ? catalog.plans : [];
    } catch {
      cachedBillingPlans = [];
    }
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
    ui.billingPlan.replaceChildren();
    if (subscriptions.length === 0) {
      ui.billingPlan.textContent = "No subscription is currently on this account.";
    } else {
      for (const subscription of subscriptions) {
        const row = document.createElement("div");
        row.className = "subscription-row";
        const detail = document.createElement("p");
        const planId = String(subscription.plan_id || "");
        const planName = planId.replaceAll("-", " ").replace(/\b\w/g, (letter) => letter.toUpperCase()) || "Subscription";
        const state = String(subscription.status || "unknown");
        const endDate = subscription.current_period_end ? new Date(subscription.current_period_end) : null;
        const paidThrough = endDate && !Number.isNaN(endDate.valueOf())
          ? ` · paid through ${new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(endDate)}`
          : "";
        detail.textContent = `${planName} · ${state}${paidThrough}`;
        row.append(detail);
        const plan = cachedBillingPlans.find((candidate) => candidate.plan_id === planId);
        if (accountSignedIn && state === "active" && plan?.billing_mode === "recurring") {
          const manage = document.createElement("button");
          manage.type = "button";
          manage.className = "key-remove";
          manage.textContent = "Manage";
          manage.addEventListener("click", async () => {
            manage.disabled = true;
            try {
              await invoke("manage_subscription", { planId });
              ui.billingState.dataset.error = "false";
              ui.billingState.textContent = "Subscription management opened in your browser. Changes appear here after the service verifies the update.";
            } catch (error) {
              ui.billingState.dataset.error = "true";
              ui.billingState.textContent = error.message || "Could not open subscription management.";
            } finally {
              manage.disabled = false;
            }
          });
          row.append(manage);
        }
        ui.billingPlan.append(row);
      }
    }
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
ui.loadPlans.addEventListener("click", refreshPlans);
ui.cancelCheckout.addEventListener("click", () => {
  selectedPlanId = "";
  ui.checkoutForm.hidden = true;
  ui.checkoutEmail.value = "";
});
ui.checkoutForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!selectedPlanId) return;
  const email = ui.checkoutEmail.value.trim();
  if (!email) {
    ui.plansState.textContent = "Enter a billing email to continue to Paystack.";
    ui.plansState.dataset.error = "true";
    return;
  }
  ui.startCheckout.disabled = true;
  try {
    await invoke("create_checkout", { planId: selectedPlanId, email });
    ui.checkoutForm.hidden = true;
    ui.checkoutEmail.value = "";
    ui.plansState.dataset.error = "false";
    ui.plansState.textContent = "Secure checkout opened in your browser. Feature access updates after payment is verified; refresh subscription access when you return.";
  } catch (error) {
    ui.plansState.textContent = error.message || "Could not start checkout.";
    ui.plansState.dataset.error = "true";
  } finally {
    ui.startCheckout.disabled = false;
  }
});

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
