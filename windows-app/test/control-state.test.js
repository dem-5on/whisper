import assert from "node:assert/strict";
import test from "node:test";
import { deriveControlState } from "../src/control-state.js";

test("disconnect disables controls that require the daemon", () => {
  assert.deepEqual(
    deriveControlState({ state: "ERROR", last_audio: true }, false, false),
    {
      toggleDisabled: true,
      cancelDisabled: true,
      copyDisabled: true,
      retryDisabled: true,
      streamingDisabled: true,
      settingsDisabled: true,
    },
  );
});

test("idle with a previous recording enables retry and copy", () => {
  assert.deepEqual(
    deriveControlState({ state: "IDLE", last_audio: true }, true, false),
    {
      toggleDisabled: false,
      cancelDisabled: true,
      copyDisabled: false,
      retryDisabled: false,
      streamingDisabled: false,
      settingsDisabled: false,
    },
  );
});

test("recording keeps stop and cancel available and blocks retry/live changes", () => {
  const state = deriveControlState({ state: "RECORDING", last_audio: true }, true, false);
  assert.equal(state.toggleDisabled, false);
  assert.equal(state.cancelDisabled, false);
  assert.equal(state.copyDisabled, false);
  assert.equal(state.retryDisabled, true);
  assert.equal(state.streamingDisabled, true);
});

test("batch-only providers disable live transcription", () => {
  const state = deriveControlState({
    state: "IDLE",
    last_audio: true,
    backend: "openrouter",
    live_engine: "local",
  }, true, false);
  assert.equal(state.streamingDisabled, true);
});

test("hosted live engine is not blocked by the batch provider selection", () => {
  const state = deriveControlState({
    state: "IDLE",
    backend: "openrouter",
    live_engine: "hosted-whisper",
  }, true, false);
  assert.equal(state.streamingDisabled, false);
});

test("focus-test builds keep all interactive controls disabled", () => {
  const state = deriveControlState({ state: "IDLE", last_audio: true }, true, true);
  assert.ok(Object.values(state).every(Boolean));
});
