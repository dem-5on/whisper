import assert from "node:assert/strict";
import test from "node:test";
import { microphoneLabel } from "../src/display.js";

test("default microphone has a stable friendly label", () => {
  assert.equal(microphoneLabel("default"), "Default");
  assert.equal(microphoneLabel(""), "Default");
  assert.equal(microphoneLabel(null), "Default");
});

test("a selected device ID is shown as its human-readable name", () => {
  assert.equal(
    microphoneLabel("12", [{ id: 12, name: "USB microphone" }]),
    "USB microphone",
  );
});

test("unknown device IDs remain visible instead of disappearing", () => {
  assert.equal(microphoneLabel("12", []), "12");
});
