import test from "node:test";
import assert from "node:assert/strict";
import { hasFeature, isCloudBackend } from "../src/feature-access.js";

test("only an explicit true feature value grants access", () => {
  assert.equal(hasFeature({ cloud_transcription: true }, "cloud_transcription"), true);
  assert.equal(hasFeature({ cloud_transcription: false }, "cloud_transcription"), false);
  assert.equal(hasFeature({}, "cloud_transcription"), false);
  assert.equal(hasFeature(null, "cloud_transcription"), false);
});

test("local is the only non-cloud transcription backend", () => {
  assert.equal(isCloudBackend("local"), false);
  assert.equal(isCloudBackend("openai"), true);
  assert.equal(isCloudBackend("groq"), true);
  assert.equal(isCloudBackend("openrouter"), true);
  assert.equal(isCloudBackend(undefined), false);
});
