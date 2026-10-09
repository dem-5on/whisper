import assert from "node:assert/strict";
import test from "node:test";
import { formatRecordingText, sessionResetPatch } from "../src/status-view.js";

test("live text is prefixed with the hearing indicator", () => {
  assert.equal(formatRecordingText({ liveText: "hello", hearing: true }), "● hello");
  assert.equal(formatRecordingText({ liveText: "hello", hearing: false }), "○ hello");
});

test("empty partials distinguish hearing from silent microphones", () => {
  assert.match(formatRecordingText({ hearing: true }), /Hearing you/);
  assert.match(formatRecordingText({ hearing: false }), /Listening/);
});

test("decode errors and unavailable reasons win over the idle fallback", () => {
  assert.match(formatRecordingText({ decodeError: "busy" }), /retrying: busy/);
  assert.equal(
    formatRecordingText({ streaming: false, unavailableReason: "Batch-only." }),
    "Batch-only.",
  );
});

test("session reset only fires when entering a new recording", () => {
  const patch = sessionResetPatch("IDLE", "RECORDING");
  assert.equal(patch.live_text, "");
  assert.equal(patch.live_revision, 0);
  assert.equal(patch.error, "");
  assert.equal(sessionResetPatch("RECORDING", "RECORDING"), null);
  assert.equal(sessionResetPatch("RECORDING", "IDLE"), null);
  assert.equal(sessionResetPatch("IDLE", "IDLE"), null);
});
