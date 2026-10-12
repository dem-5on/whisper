import { listen, emit } from "@tauri-apps/api/event";
import "./pill.css";

const controls = document.querySelector("#recording-controls");
const processing = document.querySelector("#processing-state");
const processingLabel = document.querySelector("#processing-label");
const waveformBars = [...document.querySelectorAll("#waveform i")];

function renderStatus(status) {
  const recording = status?.stage === "recording";
  controls.hidden = !recording;
  processing.hidden = recording;
  if (!recording) {
    processingLabel.textContent = status?.stage === "rewriting" ? "Rewriting" : "Thinking";
    document.body.dataset.stage = status?.stage || "thinking";
    return;
  }
  document.body.dataset.stage = "recording";
  const level = Math.min(1, Math.max(0, Number(status.audio_level) || 0));
  const samples = Array.isArray(status.waveform) ? status.waveform : [];
  waveformBars.forEach((bar, index) => {
    const sampleIndex = Math.min(samples.length - 1, Math.floor(index * samples.length / waveformBars.length));
    const sample = Math.abs(Number(samples[sampleIndex]) || 0);
    const height = 4 + Math.max(level * 20, sample * 48);
    bar.style.height = `${Math.min(30, height).toFixed(1)}px`;
  });
  document.querySelector("#waveform").setAttribute("aria-label", `Microphone input level ${Math.round(level * 100)} percent`);
}

document.querySelector(".cancel").addEventListener("click", () => emit("pill-action", { action: "cancel" }));
document.querySelector(".finish").addEventListener("click", () => emit("pill-action", { action: "finish" }));
listen("pill-status", ({ payload }) => renderStatus(payload));
