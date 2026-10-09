/**
 * Pure view helpers for the flyout, kept free of DOM and Tauri APIs so they
 * are unit-testable under plain node.
 */

/**
 * Text for the live-transcript area while recording.
 *
 * `hearing` comes from the daemon's `speech_active` flag, so a muted or
 * wrong microphone reads differently from a working one instead of showing
 * an identical "Listening…" screen.
 */
export function formatRecordingText({
  liveText = "",
  hearing = false,
  decodeError = "",
  streaming = true,
  liveAvailable = true,
  unavailableReason = "",
} = {}) {
  const text = String(liveText || "").trim();
  if (text) return `${hearing ? "●" : "○"} ${text}`;
  if (decodeError) return `Live decode retrying: ${decodeError}`;
  if (!streaming || !liveAvailable) {
    return unavailableReason || "Live partials unavailable.";
  }
  return hearing ? "● Hearing you… speak now." : "○ Listening… press Stop recording when done.";
}

/**
 * Session-scoped fields to clear when a *new* recording starts, so values
 * from the previous session (partial text, revision, error) cannot linger
 * on screen. Returns the reset patch, or null when no reset applies.
 */
export function sessionResetPatch(previousState, nextState) {
  if (String(previousState || "IDLE") === "RECORDING" || String(nextState || "IDLE") !== "RECORDING") {
    return null;
  }
  return {
    live_text: "",
    live_revision: 0,
    session_id: "",
    committed_text: "",
    provisional_text: "",
    error: "",
  };
}
