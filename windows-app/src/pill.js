import { listen, emit } from "@tauri-apps/api/event";
import "./pill.css";

const controls = document.querySelector("#recording-controls");
const processing = document.querySelector("#processing-state");
const processingLabel = document.querySelector("#processing-label");
const waveform = document.querySelector("#waveform-line");

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
  const points = samples.length > 1
    ? samples.map((sample, index) => `${(index * 100 / (samples.length - 1)).toFixed(1)},${(15 - Math.max(-1, Math.min(1, Number(sample) || 0)) * 13).toFixed(1)}`).join(" ")
    : "0,15 100,15";
  waveform.setAttribute("points", points);
  document.querySelector("#waveform").setAttribute("aria-label", `Microphone input level ${Math.round(level * 100)} percent`);
}

document.querySelector(".cancel").addEventListener("click", () => emit("pill-action", { action: "cancel" }));
document.querySelector(".finish").addEventListener("click", () => emit("pill-action", { action: "finish" }));
listen("pill-status", ({ payload }) => renderStatus(payload));
