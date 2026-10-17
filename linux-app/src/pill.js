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
  bars.forEach((bar, index) => {
    const peak = Math.max(Math.abs(Number(samples[index * 2]) || 0), Math.abs(Number(samples[index * 2 + 1]) || 0));
    // Use only captured PCM peaks: a silent/failed microphone stays visibly
    // still instead of presenting a synthetic animation as live audio.
    const height = peak < 0.002 ? 2 : 2 + Math.pow(peak, 0.34) * 23;
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
  cancelButton.disabled = true;
  finishButton.disabled = true;
  try {
    await invoke("request_stop_session");
  } finally {
    cancelButton.disabled = false;
    finishButton.disabled = false;
  }
});
