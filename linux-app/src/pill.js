import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import "./pill.css";

const waveform = document.querySelector("#pill-waveform");
const statusLabel = document.querySelector("#pill-status");
const cancelButton = document.querySelector("#pill-cancel");
const finishButton = document.querySelector("#pill-finish");
const bars = [];

for (let index = 0; index < 12; index += 1) {
  const bar = document.createElement("span");
  bar.className = "pill-bar";
  waveform.append(bar);
  bars.push(bar);
}

listen("recording-status", ({ payload }) => {
  const recording = payload.recording === true;
  waveform.hidden = !recording;
  statusLabel.hidden = recording;
  statusLabel.textContent = payload.title || "Thinking";
  cancelButton.hidden = !recording;
  finishButton.hidden = !recording;
  waveform.dataset.recording = String(recording);

  const samples = Array.isArray(payload.audio_waveform) ? payload.audio_waveform : [];
  const level = Math.min(1, Math.max(0, Number(payload.audio_level) || 0));
  bars.forEach((bar, index) => {
    const peak = Math.max(Math.abs(Number(samples[index * 2]) || 0), Math.abs(Number(samples[index * 2 + 1]) || 0));
    const measured = peak || level;
    const height = measured === 0 ? 3 : 3 + Math.sqrt(measured) * 38;
    bar.style.height = `${height.toFixed(1)}px`;
  });
});

async function send(command) {
  cancelButton.disabled = true;
  finishButton.disabled = true;
  try {
    await invoke("daemon_request", { command, params: {} });
  } finally {
    cancelButton.disabled = false;
    finishButton.disabled = false;
  }
}

cancelButton.addEventListener("click", () => void send("cancel"));
finishButton.addEventListener("click", async () => {
  const status = await invoke("daemon_request", { command: "status", params: {} });
  await send(status.session_mode === "dictation" ? "toggle" : "stop_session");
});
