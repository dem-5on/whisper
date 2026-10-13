# Wicx Smoke-Test Runbook

Use this before merging feature work and before preparing a desktop release. This complements the detailed acceptance criteria in [`implementation-tasks.md`](implementation-tasks.md) and product behavior in [`product-spec.md`](product-spec.md).

## Automated checks

Run from the repository root.

### Shared Python daemon and hosted service

Install development dependencies if needed:

```sh
python -m pip install -e '.[dev]'
```

Run all Python tests:

```sh
python -m pytest -q -p no:cacheprovider
```

The HTTP/API tests bind local sockets. If a sandbox blocks loopback sockets, rerun in an environment that permits local test servers. Do not disable those tests to claim a full pass.

### Windows client

```sh
npm --prefix windows-app ci
npm --prefix windows-app test
npm --prefix windows-app run build
cargo test --manifest-path windows-app/src-tauri/Cargo.toml
```

The native install/focus test also has a dedicated Windows workflow and must be exercised on Windows; a Linux build cannot verify Windows focus behavior or installer operation.

### Linux client

```sh
npm --prefix linux-app ci
npm --prefix linux-app run build
```

For native development, install the Tauri system prerequisites for the target distribution and run `npm --prefix linux-app run tauri dev`. Verify actual recording with the Wicx daemon; Vite browser preview data is not evidence of audio capture or text delivery.

## Manual end-to-end smoke checks

Run on each OS/desktop-environment combination claimed as supported. Record OS version, desktop environment, display server, audio stack, app version, and result.

1. **Local Dictation:** Start from another focused text editor, record speech, finish with the shortcut and with the pill check button, and verify text lands in the original editor. Repeat with cancel and verify no text is inserted.
2. **Pill and status:** Confirm the pill does not steal focus; microphone waveform responds to actual speech and silence; states progress through Recording, Thinking, and Rewriting when applicable; hidden-pill preference is respected.
3. **Offline fallback:** Disable network. Verify local recording/transcription still works. If Rewrite is enabled, verify the original local transcript is inserted and the user is told rewrite was skipped. Confirm it is not silently rewritten later.
4. **Saved sessions:** Record a short Lecture and Meeting, verify audio/transcript survive app restart, export the outputs available today, then delete and verify removal. Do not infer complete notes or diarization from saved audio alone.
5. **Vocabulary:** Add a custom name/jargon term, dictate it in context, verify preferred spelling, then remove it and confirm the dictionary reflects the change.
6. **Account and billing (test environment only):** Exercise Google/Apple sign-in, entitlement refresh, checkout return, signed webhook processing, cancellation/expiry, and quota fallback. Never use a live payment credential or trigger a real charge as a smoke test.
7. **Long-session recovery:** Capture beyond the old short-session limit, force-close only in a disposable test session, then verify prior persisted audio/transcript integrity and documented recovery behavior.

## Current known test caveats (2026-10-13)

- Python tests excluding Windows-specific checks passed with the repository virtual environment: `.venv/bin/python -m pytest -q -p no:cacheprovider -k 'not windows'` (one aiohttp `NotAppKeyWarning`).
- Five assertions in `tests/test_windows_tauri_spike.py` currently disagree with the Windows app's configured width, style, and dependency/source state. Resolve those on the `windows` branch; do not weaken them without checking the intended behavior.
- Two OAuth tests were missing the fixed fixture time for PKCE handoff redemption. Their calls now pass `now=self.now` so the tests remain deterministic.
- Node.js was available through the user's NVM installation but was not on the default shell `PATH`. The Linux Vite build and Windows Node unit tests were run using the explicit Node path; the Windows Vite production build and Tauri package build remain unverified in this environment.

## Reporting results

For each run, report the exact command, pass/fail/skip count, environment, and any manual checks not performed. A failed or unrun platform check is not a pass. Update `implementation-tasks.md` when a known blocker is resolved or a support claim changes.
