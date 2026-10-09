const BATCH_ONLY_PROVIDERS = new Set(["groq", "openrouter"]);
const BUSY_STATES = new Set(["PROCESSING", "DELIVERING"]);

export function deriveControlState(status, connected, focusTestBuild) {
  const state = String(status?.state || "IDLE");
  const recording = state === "RECORDING";
  const busy = BUSY_STATES.has(state);
  const providerBlocksLive = BATCH_ONLY_PROVIDERS.has(status?.backend)
    && status?.live_engine !== "hosted-whisper";
  const locked = focusTestBuild || !connected;

  return {
    toggleDisabled: locked || busy,
    cancelDisabled: locked || (!recording && state !== "ERROR"),
    copyDisabled: locked || !status?.last_audio || busy,
    retryDisabled: locked || !status?.last_audio || busy || recording,
    streamingDisabled: locked || providerBlocksLive || recording || busy,
    settingsDisabled: locked,
  };
}
