# Wicx Implementation Task Board

**Purpose:** A durable handoff checklist for the owner and any coding agent continuing Wicx work. Read this file together with [`product-spec.md`](product-spec.md) before starting a feature.

**Last reviewed:** 2026-10-13  
**Scope:** Desktop-first Linux and Windows product. macOS and mobile follow after the desktop foundation is validated.  
**Status rule:** A feature is not `DONE` because an endpoint, screen, or prototype exists. Mark it done only after its end-to-end acceptance checks pass and the relevant tests/documentation are updated.

## How to work from this board

1. At the start of a task, inspect the current branch, working-tree changes, relevant code, and this board. Preserve unrelated or untracked user work.
2. Follow the task's branch boundary below. Do not move unrelated changes between branches.
3. Before implementation, confirm the task's acceptance checks and add tests where practical. Keep UI, backend, and platform-specific work separated when that avoids cross-platform regressions.
4. After implementation, run the narrow tests plus relevant regression/build checks. Record what ran and what could not run.
5. Update this file's status and notes in the same change as the implementation. Do not mark a task complete if it still depends on a mock, manual entitlement, or unintegrated client workflow.
6. Keep product behavior aligned with `product-spec.md`. If a decision is still open, document the question and ask the user before making a materially different product choice.
7. Push completed work to its assigned branch. Use the user's author identity; **never use Codex as commit author and never add a Codex co-author line.**

### Branch ownership

- `New-features`: shared backend/service, API contracts, data model, product specifications, and cross-platform foundations.
- `linux`: Linux app UI and Linux-specific native integration/package work.
- `windows`: Windows app UI and Windows-specific shell, installer, and integration work.
- Later platform branches should be created for macOS, Android, and iOS when those efforts begin.
- Do not put shared backend changes only on a platform branch. Coordinate API changes through `New-features`, then integrate platform clients against the agreed contract.

### Status meanings

- `[ ] TODO` — not implemented or not yet investigated.
- `[~] IN PROGRESS` — partial implementation, prototype, or integration/testing remains.
- `[x] DONE` — end-to-end acceptance checks pass on the stated scope.

## Current baseline (do not assume more than this)

- `[~]` Linux-first transcription daemon and Linux Tauri app are evolving. Linux UI work is tracked separately on `linux`.
- `[~]` Windows Tauri client exists but remains in testing; installer and focused-app behavior still need validation.
- `[~]` Local recording/transcription, session storage, transcript export, dictionary operations, and speaker-label storage exist in some form. Audit current behavior before extending them.
- `[~]` Hosted cloud transcription/rewrite, OAuth, Paystack billing/webhook, and diarization services have prototypes/endpoints. Their existence does not mean desktop sign-in, purchase, entitlements, or post-processing work end to end.
- `[~]` A self-hosted diarization worker and queued API prototype exist. The desktop job lifecycle, durable recovery, and billing entitlement source are unfinished.
- `[ ]` No public release should claim full multilingual, all-distro Linux, full Lecture/Meeting analysis, account/subscription, sync, or speaker-detection support until the acceptance checks below are met.

### Audited feature matrix (2026-10-13)

| Area | Current evidence | State / remaining gap |
| --- | --- | --- |
| Local recording, transcription, session storage | Python daemon modules and tests in `transcriber/`, `tests/test_audio.py`, `tests/test_transcription.py`, `tests/test_session_store.py` | `[~]` Core paths exist; long-session durability, crash recovery, and end-to-end platform validation remain. |
| Dictation delivery and recording pill | Linux and Windows clients plus native daemon integration | `[~]` UI/platform code exists; validate global activation, focus preservation, real waveform/state transitions, cancel/finish, and delivery on each supported OS/environment. |
| History, export, custom vocabulary | `transcriber/session_store.py`, `session_export.py`, `vocabulary.py` and associated tests | `[~]` Local storage and API operations exist; verify complete client workflows, editing/import/export, deletion, and vocabulary effect on recognition. |
| Cloud transcription | Hosted service WebSocket/protocol and service tests; `cloud_transcription` feature grant | `[~]` Service prototype is tested; desktop sign-in/entitlement integration and production quota lifecycle are incomplete. |
| Rewrite | Hosted rewrite endpoint/tests; Windows Dictation integration described in `README.md` | `[~]` Provider/service path exists; Linux client integration, complete entitlement flow, and quality/latency evaluation remain. |
| Google/Apple identity | Hosted OAuth/OIDC routes, PKCE handoff, and tests | `[~]` Server path exists; desktop callback/deep-link, secure token storage, sign-in/out, and lifecycle wiring remain. |
| Paystack subscriptions | Checkout/status/webhook and billing ledger code/tests in `whisper_service/` and `tests/` | `[~]` Backend prototype exists; approved live plans, desktop checkout/status UX, full lifecycle/support cases, and entitlement reconciliation remain. |
| Usage limits and feature gates | Server entitlement checks and optional cloud-minute metering | `[~]` Core mechanisms exist; normal production source must be billing-backed, limits/policy finalized, and all client fallbacks verified. |
| Lecture outputs | Long-session and session primitives exist | `[ ]` Structured notes, topic grouping, key terms, and study questions are not a complete end-to-end workflow. |
| Meeting outputs and speaker detection | Self-hosted queued diarization worker/API; local segment alignment and speaker-label persistence | `[~]` Prototype exists; desktop upload/poll/retry, durable jobs, billing entitlements, user correction workflow, and meeting summaries/actions remain. |
| Translation and code-switching | Multilingual transcription/provider options exist | `[ ]` No validated source-plus-English workflow or representative Twi-English/French-English quality evaluation is established. |
| Encrypted paired-device sync | Product direction documented in `product-spec.md` | `[ ]` Not implemented; protocol, threat model, keys, pairing, relay, recovery, and conflict behavior need design first. |
| Linux/Windows release readiness | Linux installer currently documents limited GNOME Wayland Debian/Ubuntu support; Windows app/installer is still in testing | `[~]` Compatibility matrices, clean-machine packaging, focus checks, and release validation remain. |
| macOS and mobile | Product direction only | `[ ]` No implementation; assess after Linux and Windows desktop acceptance. |

**Test evidence:** `.venv/bin/python -m pytest -q -p no:cacheprovider -k 'not windows'` passed on 2026-10-13 (one aiohttp `NotAppKeyWarning`). The complete run exposed five failures in `tests/test_windows_tauri_spike.py` where assertions disagree with current Windows config/styles/dependencies; reconcile those on `windows`. Two OAuth tests omitted the fixed test time when redeeming PKCE handoffs; those test calls are corrected to pass the fixture time. The first unprivileged run could not bind local test sockets due to sandbox restrictions. Node/npm were unavailable in this execution environment, so JavaScript test/build commands were not run.

## Priority 0 — Establish an honest, testable baseline

### P0.1 Audit current end-to-end behavior `[x] DONE — 2026-10-13`

Inventory the daemon, hosted service, Linux client, and Windows client. For each current feature, record whether it is implemented, prototype-only, mocked, manually configured, or absent. Identify API mismatches and duplicate/obsolete paths before building more layers.

**Acceptance:** Feature matrix and current test evidence are recorded above. End-to-end verification is still required by the feature tasks; audit completion does not mean those features are production-ready.

### P0.2 Define a smoke/regression suite `[~] IN PROGRESS`

Cover local Dictation, cancellation/finish, transcript insertion, saved sessions, export, dictionary add/remove, offline fallback, rewrite failure fallback, service restart, and data deletion. Add Linux/Windows-specific checks where behavior differs.

**Current evidence:** Broad Python tests and platform-specific CI workflows exist. The [smoke-test runbook](smoke-test-runbook.md) now lists commands and manual checks. Non-Windows Python tests passed; five Windows static checks need reconciliation on the Windows branch. Frontend build/test commands have not been run in this agent environment.

**Acceptance:** One documented command or short runbook runs the automated checks; manual-only checks are explicitly listed with expected outcomes.

## Priority 1 — Make core desktop Dictation dependable

### P1.1 Linux compatibility and native behavior `[~]`

Validate microphone capture, desktop shortcut setup, non-activating pill placement, tray behavior, and insertion into the previously focused app across an explicit Linux matrix (distro, desktop environment, X11/Wayland, audio stack). Keep the desktop environment responsible for global shortcut registration where required; do not claim universal Linux support before testing.

**Acceptance:** A published compatibility matrix records tested combinations and known limitations. Recording, cancel/finish, audio-derived waveform, processing states, and text insertion pass on every combination advertised as supported.

### P1.2 Windows native flow and installer `[~]`

Complete Windows global shortcut/tray behavior, preserve target-app focus while using the overlay, ensure the transcription sidecar is present and starts reliably, and verify install/uninstall/update behavior.

**Acceptance:** A clean Windows machine can install, launch, dictate into another app, cancel/finish, upgrade, and uninstall using the supported installer. No missing-sidecar or focus-stealing failure remains.

### P1.3 Dictation rewrite end to end `[~]`

Use local transcription as the fallback. When rewrite is enabled and the user is online and entitled, send only the transcript to the approved rewrite endpoint, show “Rewriting,” and insert the result into the app that had focus. If offline, denied, or failed, insert the original transcript and clearly report why rewrite was skipped. Never silently replace already-inserted text later.

**Acceptance:** Linux and Windows separately pass enabled/disabled, entitlement-denied, offline, timeout, malformed-output, and focused-app insertion tests. Rewrite remains off by default and preserves jargon/meaning in the evaluation set.

## Priority 2 — Connect identity, billing, and entitlements

### P2.1 Desktop Google/Apple sign-in `[~]`

Hosted OAuth routes exist as a prototype; wire desktop sign-in using the approved PKCE handoff, deep-link/callback registration, secure OS credential storage, sign-out, session expiry, and account recovery behavior. Do not add password/phone login.

**Acceptance:** Google and Apple login can each create/recover the same provider-linked account safely on supported desktop clients. Tokens are not stored in UI preferences/logs; expired/revoked sessions fail closed.

### P2.2 Paystack plans, checkout, and lifecycle `[~]`

Connect the backend checkout/status/webhook work to real approved plan configuration and the subscription experience. Finalize plan names, pricing, currencies, billing periods, usage allowances, fair-use rules, warning thresholds, reset behavior, refund/dispute handling, and Ghana mobile-money renewal behavior before public billing.

**Acceptance:** A test-mode purchase, verified webhook, cancellation/expiry, failed payment, refund/dispute path, and client entitlement refresh produce the expected account state. Client callbacks alone never grant access; raw card data is never handled or stored by Wicx.

### P2.3 One entitlement/quota source of truth `[~]`

Replace manual owner-only feature grants as the normal production path. Enforce cloud transcription, rewrite, diarization, and other paid features on the server, and accurately communicate limits/fallbacks in clients. Local Dictation continues after quota exhaustion; Lecture/Meeting recording and local transcription continue, but paid analysis is blocked.

**Acceptance:** Automated tests cover each tier, each paid feature, quota boundary/crossing, renewal/reset, expiry mid-session, offline use, and stale client credentials. The client cannot bypass server enforcement.

## Priority 3 — Complete Lecture and Meeting as long-form workflows

### P3.1 Durable long-session capture and recovery `[ ]`

Audit current recording limits and persistence. Support long Lecture/Meeting sessions, retained audio until user deletion, incremental/durable local capture, safe stop/cancel semantics, and recovery after app/daemon/network interruption.

**Acceptance:** Long-session tests exceed current short-session limits, recover after forced process termination without corrupting prior audio/transcript, and retain audio/transcript until explicit deletion. Dictation remains low-latency and does not inherit long-mode latency.

### P3.2 Lecture post-processing `[ ]`

After capture/transcription, generate structured notes, topic/section grouping, key terms/definitions, and review questions. Keep the source transcript and any translation distinct. Queue cloud-only work during outages and run it only when the user is still entitled.

**Acceptance:** A full Lecture produces transcript, notes, grouped topics, and study questions linked to the source session; local transcript remains available if cloud processing is unavailable or over limit. User can export and delete associated artifacts.

### P3.3 Meeting outputs and diarization client integration `[~]`

Connect the authenticated self-hosted diarization API to desktop Meeting sessions with upload limits, durable job IDs, retry/poll/backoff, cancellation/expiry behavior, and entitlement checks. Keep initial transcript visible without waiting for diarization. Then generate summary, decisions, action items, owners, and follow-ups.

**Acceptance:** Transcript appears before speaker analysis. A restarted client/server can recover or clearly fail/retry a job. Offline/over-limit states never fabricate speaker assignments. Successful labels align to timestamps and user edits persist.

### P3.4 Speaker correction and safe retention `[~]`

Support provisional “Speaker 1/2” labels, rename, reassign transcript spans, merge speakers, and preserve edits against later processing. Define and test deletion of local originals and temporary server copies.

**Acceptance:** Mislabel corrections survive app restarts and are reflected in regenerated meeting outputs. Failure states are clear; the product does not imply perfect identity recognition.

## Priority 4 — Validate language quality and vocabulary

### P4.1 Multilingual/code-switch evaluation `[ ]`

Create a permissioned test set covering English, Twi-English, French-English, regional accents, and relevant long-form use. Decide language-ID/translation policy and preserve source transcript separately from English output when translation is applied.

**Acceptance:** Record per-language and mixed-language quality/latency results and supported-language limitations. No broad accuracy claim is made without measured evidence.

### P4.2 Dictionary and jargon quality `[~]`

Audit that custom terms influence recognition or preferred spelling (not just local storage). Add safe matching for names, acronyms, technical and non-technical domain terms; add edit and import/export behavior if approved.

**Acceptance:** A regression set verifies terms such as user-added names and jargon are recognized without causing unrelated words to be rewritten. Dictionary operations and deletion are covered across clients.

## Priority 5 — Privacy, sync, packaging, and release readiness

### P5.1 Encrypted paired-device sync `[ ]` — design before implementation

Finalize pairing/verification, end-to-end key management and recovery, encrypted relay behavior, large-file resumable transfer, conflict resolution, device revocation, storage/quota limits, and subscription-lapse behavior. Intended data includes Lecture/Meeting content, Dictation history, custom vocabulary, and settings; device-only remains the default unless the user pairs devices.

**Acceptance:** Threat model and protocol are reviewed before coding. Prototype passes pairing, offline catch-up, tamper/replay, revoked-device, interrupted-large-transfer, conflict, and key-recovery tests. Relay cannot read content.

### P5.2 Privacy and security hardening `[ ]`

Define cloud payload minimization, operational logging, retention/deletion guarantees, secret storage, account deletion, audio/transcript export/deletion, and incident handling. Verify actual service behavior before promising that content is immediately erased.

**Acceptance:** Security/privacy review and automated checks cover auth, authorization boundaries, private audio/transcripts, deletion paths, webhook signature/replay checks, and sensitive-log redaction.

### P5.3 Installers, release pipeline, and update delivery `[~]`

Finish Linux and Windows packaging, signing/trust expectations, clean-machine install/upgrade/uninstall, release assets, and accurate download/support documentation. The Linux app's update check reports published-release availability; it is not an automatic installer/updater.

**Acceptance:** Reproducible build artifacts install on supported clean environments, checksums/signatures are verified as applicable, update checks map to the actual release version, and release notes state limitations honestly.

### P5.4 Later platforms `[ ]`

After Linux and Windows desktop acceptance, assess macOS, Android, and iOS capture, shortcut/input, permissions, background-audio, store billing, and mode constraints. Keep each platform in its own application directory and test matrix.

**Acceptance:** Platform feasibility and feature parity are documented before committing to release dates or identical feature claims.

## Open product/technical decisions to resolve before affected work

- Actual plan prices, tiers, allowances, reset periods, and fair-use thresholds.
- Translation defaults and supported-language/quality claims, especially Twi-English code-switching.
- Exact long-session crash recovery and stop behavior.
- Diarization model/hosting cost, languages, accuracy target, and deletion guarantee.
- Sync key recovery, category controls, device limits, and conflict policy.
- Linux support matrix and release packaging per distribution.
- Public release/update channel and Windows installer/signing approach.

When a decision blocks implementation, ask the user with a short explanation of the impact; do not silently invent a policy.
