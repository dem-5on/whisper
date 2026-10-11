# Wicx Product Specification

**Status:** Product direction and requirements captured from planning discussions  
**Scope:** Desktop-first product plan for Linux and Windows, with macOS and mobile to follow  
**Important:** This describes the intended product. It does not imply that every feature is implemented or available today.

## 1. Product vision

Wicx is a voice-first productivity tool for people who want to speak naturally and get useful written output without managing speech models, providers, or technical setup. It should feel reliable and approachable to a non-technical user while still serving developers, students, and teams with advanced capabilities.

The product has three primary modes:

1. **Dictation** — speak into the currently focused application.
2. **Lecture** — capture a class or other long-form teaching session and produce a transcript and study materials.
3. **Meeting** — capture a multi-speaker discussion and produce a speaker-labeled record and meeting outcomes.

Dictation is the everyday, low-friction workflow. Lecture and Meeting are structured session workflows: both capture and transcribe audio, but their generated outputs differ. Lecture and Meeting should remain separate product modes rather than one ambiguous mode, while sharing the underlying long-session capture and transcription infrastructure.

## 2. Product principles

- **Sell outcomes, not model controls.** Most users should not need to understand providers, model names, API keys, or inference settings.
- **Local-first access, paid cloud value.** Local transcription supports free dictation and provides continuity/fallback. Subscription features provide cloud-powered rewriting and long-form analysis.
- **Never lose an active recording because a quota or network condition changed.** Keep capture and local transcription available where possible; make unavailable paid processing clear.
- **Preserve what the speaker said.** Keep source-language transcription when translation is used, and avoid silently replacing a completed transcript later.
- **Be explicit about recording.** Make recording state visible and warn users before recording other people.
- **Build for real platform constraints.** Linux desktop environments and input systems differ; support claims must follow tested compatibility, not assumptions.
- **Make privacy legible.** Explain what is processed locally, what is sent to cloud services, what is retained, and how users delete or export it.

## 3. Users and primary jobs

### Everyday dictation user

Wants to speak naturally and have the words appear in whichever app currently has focus, without opening a large window or changing their writing workflow.

### Student or learner

Wants to record a lecture, get a faithful transcript plus organized study notes, revisit the audio, and export or share the materials.

### Meeting participant

Wants to capture a discussion, distinguish speakers, find decisions and action items, correct speaker names, and share a useful meeting record.

### Multilingual speaker

May switch languages during a sentence or conversation (for example, English mixed with Twi in Ghana or English mixed with French elsewhere). Wants accurate recognition of the different languages and clear English output when translation is requested or required by the workflow.

### Technical or domain-specific speaker

Uses names, jargon, acronyms, product names, code identifiers, and personal vocabulary that generic dictation often misrecognizes. Wants to teach the system preferred spellings and have them applied consistently.

## 4. Platforms and rollout

### Initial desktop focus

- Linux and Windows are the initial desktop targets.
- Linux support is intended to be broad across distributions and desktop environments, not restricted conceptually to one distro, Wayland, or X11. However, global shortcuts, microphone access, focus handling, and text insertion vary by compositor, display server, desktop environment, and input method. Each combination needs a compatibility strategy and testing before it is advertised as supported.
- Windows is a native-feeling desktop tray/flyout app with global shortcut support and insertion into the previously focused application.

### Later platforms

- macOS is planned after the Linux and Windows desktop experience is established.
- Android and iPhone are part of the longer-term cross-platform direction.
- Feature parity should be evaluated per platform. Mobile operating systems impose different background recording, keyboard/input, microphone, and distribution constraints; do not assume desktop interaction patterns can be copied unchanged.

### Compatibility and support claims

Maintain a tested platform matrix covering at minimum operating system/version, desktop environment or window manager, display server where applicable, shortcut registration, microphone capture, text insertion, tray/panel behavior, and known limitations. “Available on Linux” must not imply that every distro/environment works identically until validated.

## 5. Account, identity, and subscription

### Sign-in

- Users can create an account or sign in only with **Google** or **Apple**.
- Do not build a first-party username/password, phone-number, or independent email/password flow.
- Account identity will be used for subscription entitlements and, if enabled, paired-device sync.
- The exact platform-specific availability and account recovery behavior remain implementation questions, especially Apple sign-in on Linux/Windows clients.

### Subscription model

- Provide multiple subscription tiers; higher tiers provide higher usage allowances.
- Plans should communicate generous/unlimited-style access subject to a clearly explained fair-use policy. Exact quotas, pricing, reset windows, and fair-use thresholds are not yet specified.
- Give users a clear warning before they reach their allowance and explain what will continue working after the limit.
- When a user needs a subscription-only feature, show a subscription/packages screen with the available plans rather than an unexplained failure.

### Behavior at the usage limit — confirmed

- The user can continue to record Lectures and Meetings.
- Transcription falls back to the local model.
- The user retains the resulting local transcript.
- Subscription-only outputs and processing are unavailable after the limit: generated notes, topic grouping, preparation of questions, and other paid AI features do not run.
- Those paid outputs require upgrading or otherwise regaining subscription allowance.
- The free tier includes local Dictation, but not paid rewriting or cloud-generated Lecture/Meeting analysis.

The precise timing of quota checks, allowance reset behavior, and treatment of a session that crosses its allowance during cloud processing still need definition. The user's confirmed priority is that recording and local transcription remain possible and paid analysis does not run after entitlement is exhausted.

## 6. Modes and feature requirements

### 6.1 Dictation

**Purpose:** Turn speech into text in the focused application with minimal interruption.

Requirements:

- A global shortcut starts recording; pressing the same shortcut again finishes it. The user also chooses the checkmark on the recording pill to finish.
- The X on the pill cancels/discards the current recording. The checkmark finishes; it does **not** pause.
- On finish, transcription and any enabled rewrite continue. The user should see processing status rather than assume the app stopped responding.
- The finalized text is inserted into the application that had focus before the overlay appeared. The overlay must not steal focus in a way that redirects typing into itself.
- Verbatim transcription is the default. **Rewrite is off by default.**
- When Rewrite is enabled and the user is online/entitled, the system rewrites the transcript and inserts the rewritten result directly into the focused application. Do not require a confirmation dialog.
- Rewrite should improve clarity, grammar, punctuation, and removal of disfluencies while preserving meaning and speaker intent. It must not invent claims or silently change technical terms.
- A small rewrite/lightbulb affordance beside completed text can request an on-demand rewrite, subject to the user's entitlement. This does not replace the setting-controlled automatic rewrite behavior.
- Keep Dictation history as text. Audio retention is not required for ordinary Dictation.
- Local Dictation is available in the free tier. Cloud transcription, rewriting, and other paid AI features require an active subscription/allowance.

### 6.2 Lecture

**Purpose:** Capture a class or lecture and turn it into a reviewable study resource.

Requirements:

- Support long-form recording; the existing short recording limit is not suitable for this mode.
- Record and transcribe throughout the session. Run note preparation and other heavier analysis after capture/transcription, rather than interrupting recording.
- Produce and retain:
  - the source-language transcript;
  - an English translation where translation is applied;
  - structured lecture notes;
  - topic/section grouping;
  - key terms and definitions;
  - review questions and preparation materials.
- Keep the original recording until the user deletes it. The user must be able to delete recordings and associated session data.
- Allow export of transcripts and notes. Exporting is the current sharing direction; share links are not part of the initial plan.
- At the usage limit, continue recording and transcribe locally, but do not generate the subscription-only notes, topic grouping, questions, or other paid outputs.
- If internet is lost during an entitled session, continue recording and transcribe locally. When connectivity returns, deferred post-session processing can proceed if the user still has entitlement. Do not silently cloud-retranscribe and replace the local transcript; cloud processing may use the retained transcript. Any later audio-based improvement should be an explicit user action and remains an open product decision.

### 6.3 Meeting

**Purpose:** Capture a multi-person discussion and produce an actionable record.

Requirements:

- Share long-session capture, retention, export, and outage behavior with Lecture mode.
- Produce and retain:
  - the source-language transcript;
  - an English translation where translation is applied;
  - timestamped transcript spans with automatically detected speaker labels (initial display names such as “Speaker 1”, “Speaker 2”);
  - a summary;
  - decisions;
  - action items, owners, and follow-ups.
- **Automatic speaker detection (diarization) is a planned subscription feature, not yet implemented.** It is a distinct AI task from speech recognition: analyze the meeting audio to determine which speaker was active during each time range, then associate those ranges with timestamped transcript spans. It must not delay or block the initial transcript becoming available.
- **Hosting decision confirmed:** Wicx will host the diarization inference itself; meeting audio will not be sent to a third-party diarization API. The initial implementation is evaluating pyannote Community-1 as the self-hostable baseline. Its model access requires an operator Hugging Face token and acceptance of the model conditions; its model card specifies CC-BY-4.0 attribution. This is an operator setup requirement, not an end-user API key.
- Run speaker detection as asynchronous post-session processing. Show a clear processing state; make the transcript available first and add speaker labels when detection completes. Do not run diarization in Dictation or add it to Dictation's latency-critical path.
- The intended initial product path is asynchronous diarization on Wicx-hosted infrastructure for entitled subscribers. This is remote from the user's device but self-hosted by Wicx, not a third-party diarization API. If the user is offline, retain the recording and local transcript and defer analysis until connectivity returns, provided the user remains entitled. At quota exhaustion, keep recording and local transcription available but do not run subscription-only diarization or other paid analysis.
- An optional local diarization model may be evaluated later as a separately downloaded, opt-in capability. Do not bundle a large model/runtime into the default installer until model size, hardware impact, cross-platform behavior, licensing/attribution, and first-run download flow have been reviewed.
- Let the user rename speaker labels to actual names, correct misassigned spans, and merge duplicate speaker identities. Automated diarization can be wrong; the UI must make corrections possible and must not imply perfect identity recognition.
- Warn before recording that other people may be recorded. Keep a visible recording indicator throughout the session.
- At the usage limit, continue recording and transcribe locally, but block paid summary, speaker analysis/grouping, action-item extraction, and other subscription outputs.
- If offline, local capture/transcription continues. Any cloud-dependent meeting analysis can happen after connectivity returns if entitlement allows.
- Diarization is post-session and must not gate local transcription or the first transcript view. Offline/over-limit fallback does not assign automatic speakers; show labels as pending, unavailable, or not included rather than fabricating speaker assignments.

### 6.4 Explicit translation and multilingual behavior

- The platform must handle multilingual speech, including language switching inside a sentence or session, rather than forcing a single language choice for the entire recording.
- The target output for the user's stated examples is clear English, including when the speaker uses Twi or mixes Twi and English, or uses French and English.
- Preserve both the source-language transcript and the English translation for saved sessions. English is the primary displayed result; the source transcript remains available for verification and fidelity.
- Detect/identify the languages represented in the recording and retain appropriate language/segment metadata where practical. The exact granularity (per phrase, utterance, or session) is an architecture decision.
- Translation must preserve technical terms, names, jargon, and code identifiers where appropriate; translation should not normalize away meaningful vocabulary.
- Language coverage and quality must be validated empirically. Support for multilingual speech-to-text in general does not establish reliable Twi-English code-switching. Build a representative evaluation set, including local accents and mixed-language examples, before making accuracy claims. Provide honest language support/quality information.

### 6.5 Vocabulary, jargon, and domain terms

- Include a user-managed custom vocabulary/dictionary for names, jargon, acronyms, technical terms, and project-specific words across domains—not only software development.
- Let users add, edit, and delete entries; exact interaction and import/export are still to be designed.
- Use vocabulary to improve recognition and/or apply a user's preferred spelling without damaging unrelated words.
- Preserve terms such as framework names, library names, product names, and personal variable/function names when spoken.
- Dictionary sync across paired devices is included in the planned sync data, subject to the sync feature being enabled.

## 7. Recording pill, shortcut, and interaction design

### Recording/processing pill

- Show a compact floating status pill when recording begins by default.
- The user can enable a setting called **Hide recording pill**. It is off by default, meaning the pill is visible; when on, hide the pill during recording. The user can still inspect status through the system tray/menu.
- The pill must not take focus away from the target application.
- During recording, display a **real audio-derived waveform/level visualization**, not a looping CSS animation. It should respond to captured microphone samples and reflect silence/speech changes.
- When recording finishes, show **Thinking** during transcription.
- If Rewrite is enabled and processing is online/entitled, show **Rewriting** during that stage.
- For longer Lecture/Meeting processing, expose clear stage/progress/status information so the app does not appear stuck.
- If the network is unavailable and Rewrite cannot run, explicitly say that rewriting is unavailable/offline and that the local transcript will be inserted. Do not silently imply that rewriting occurred.
- X cancels and discards the current capture; check finishes it. Neither action means pause. The shortcut toggles start/finish for Dictation. Lecture/Meeting start/stop shortcut semantics should be consistent but may need a confirmation flow when stopping a long session.

### Keyboard shortcut

- The intended default shortcut is **Ctrl + Space** for starting/finishing Dictation.
- The shortcut should work globally on supported platforms and should be configurable if platform conflicts require it. Conflict handling, accessibility, and shortcut reassignment UI remain to be designed.
- Linux shortcut registration may be owned by the desktop environment/compositor on some platforms. Provide a guided setup and platform-specific fallback rather than claiming one mechanism works everywhere.

### Tray and compact UI

- The tray/menu is the persistent home for opening the main UI, accessing modes/history/settings, microphone selection, shortcut help, and quitting.
- The main recording interface should remain compact, polished, and platform-appropriate; the Windows visual direction is the supplied Fluent-style design reference, but the product should not blindly copy static demo markup or fake example data.
- UI should distinguish real enabled state from disabled/unavailable controls, avoid misleading blue styling for disabled actions, and make backend/network/quota errors actionable.
- Core user workflows should not expose provider/model selection by default. Advanced settings may expose diagnostics or local model details when useful.

## 8. Offline behavior and processing pipeline

### Dictation

- Local transcription is the free baseline.
- If Rewrite is on but the network is unavailable, transcribe locally, insert the unrewritten local transcript, and clearly notify the user that rewrite was skipped because the network was unavailable.
- Do not later rewrite or replace already-inserted text without the user's explicit action.

### Lecture and Meeting

- Recording and local transcription should continue during an outage.
- Defer cloud-dependent notes, grouping, questions, summaries, or other post-processing until connectivity returns, subject to current subscription entitlement and quota.
- Keep local transcript and audio intact if deferred paid processing cannot run.
- Show whether processing is local, queued, complete, or blocked by entitlement/network so users can distinguish failure from waiting.
- Do not silently perform a second cloud transcription over the local transcript. A deliberate “improve transcript from audio” action is not yet decided.
- Meeting diarization may use the retained meeting audio for speaker-time detection and align its time ranges with the local transcript. It is a separate analysis operation, not cloud retranscription; define the exact payload, retention/deletion behavior, and entitlement check before implementation.

## 9. Audio, history, retention, and export

- Retain Lecture and Meeting audio until the user deletes it.
- Retain transcript(s), translations, generated outputs, speaker labels, and edits as session data until deletion, subject to the eventual privacy/retention policy.
- Dictation history stores text, not audio by default.
- Provide clear deletion controls and explain whether deletion covers local copies, paired-device copies, and any temporary cloud processing artifacts.
- Provide export for lecture and meeting outputs. File formats and export layout are open implementation/product decisions.
- Do not add shareable public links in the initial scope; file/text export is sufficient for now.

## 10. Privacy, consent, and cloud processing

- Before a Lecture or Meeting recording begins, clearly warn that other people may be recorded and that the user is responsible for obtaining any required consent.
- Show a persistent recording indicator during capture.
- Local transcription should remain local to the device.
- Cloud transcription/rewriting/analysis sends only the data required for the requested operation. The stated direction is that session content is not kept permanently on the service: it is sent for processing and discarded afterward, while the user's durable session data remains device-based unless the user opts into paired-device sync.
- Product copy and implementation must align. Define concrete deletion/logging/retention controls for cloud requests before release; do not promise immediate server erasure until verified technically and contractually.
- Avoid logging credentials, raw audio, transcripts, or other sensitive content unnecessarily.

## 11. Paired-device sync

### User-approved direction

- Device content is local by default.
- If the user wants the same data on another device, they pair devices and explicitly approve the pairing.
- When paired devices are online, changes sync automatically; an offline device catches up when it reconnects.
- Use a coordination service and an encrypted relay only when needed to establish/forward data. The service is not intended to act as permanent storage for session content.
- Sync is intended for subscribers and should include:
  - Lecture and Meeting audio, transcripts, translations, notes, and related session data;
  - Dictation history;
  - custom vocabulary;
  - settings.
- These sync categories are included in the intended scope; their exact defaults, per-category opt-outs, encryption/key recovery, conflict resolution, and subscription requirements must be finalized before implementation.
- Export remains available independently of sync.

### Architecture implications to resolve

Encrypted relay/sync is feasible in principle but is a substantial subsystem. Before building it, specify device identity and pairing, end-to-end encryption and key recovery, reconnect/backoff, large audio transfer/resume, conflict resolution, device revocation, quota/storage limits, and what happens when a subscription lapses. Design local data records with stable IDs and sync-friendly metadata early, even if sync ships later.

## 12. Design direction

- The supplied Wicx flyout HTML and Typeless screenshots are visual/workflow references, not a mandate to reproduce every component.
- Aim for a modern compact desktop panel with clear hierarchy, refined typography, legible state, restrained color, consistent iconography, small configuration cards, and clean spacing.
- Remove controls that confuse everyday users or expose implementation details unnecessarily.
- No fake subscription status, fake license key, fabricated hardware readiness, or demo-only interactions in production.
- Dictation should foreground the recording/transcript action. Lecture and Meeting should have dedicated session setup and post-session result views appropriate to longer, richer records.
- Keep the pill, tray menu, main settings, history, and session detail views visually related.
- Support light/dark themes where appropriate; theme behavior and default are not yet finalized.
- Responsive behavior matters for future mobile, but initial desktop layouts should be optimized for desktop flyout and settings/window sizes.

## 13. High-level architecture direction

This is a product-level direction, not a final technical design:

1. **Desktop shell and platform adapters:** tray/flyout, global shortcut, audio input, non-activating overlay, focused-app text delivery, notifications, installer/update integration. Keep OS-specific code behind platform adapters.
2. **Capture/session service:** one shared recording lifecycle for Dictation, Lecture, and Meeting, with mode-specific duration, persistence, and state transitions. Stream captured PCM for live/local work while safely writing long-form audio.
3. **Transcription layer:** pluggable local and cloud engines; language identification and multilingual/code-switch handling; source transcript and translated transcript as distinct artifacts; vocabulary support; capability/entitlement checks.
4. **Post-processing layer:** rewrite for Dictation; structured notes/questions for Lecture; asynchronous diarization/summary/decisions/action items for Meeting. Run only when connectivity and subscription entitlement permit. Keep meeting diarization off the transcript and Dictation critical paths.
5. **Local data store:** durable session metadata, audio references, transcript versions, translations, notes, speaker edits, vocabulary, and settings. Support user deletion, export, and future sync.
6. **Account/entitlement service:** Google/Apple identity, subscription state, allowance accounting, fair-use enforcement, upgrade flow, and clear local fallback behavior.
7. **Optional sync subsystem:** paired-device identity, encrypted peer/relay transfer, resumable large-file sync, conflict handling, and explicit revocation.

Avoid coupling UI to one inference provider. Product-facing controls should be phrased as outcomes (“Local”, “Cloud”, “Rewrite”) rather than a provider dashboard, unless an advanced diagnostic screen is intentionally opened.

## 14. Non-functional requirements

- **Reliability:** preserve recordings and local transcripts across app/network failures; use atomic/fault-tolerant persistence for long sessions.
- **Latency:** Dictation should give prompt feedback; show honest processing states when local hardware or cloud inference is slow.
- **Privacy/security:** protect account tokens, API credentials, audio, transcript data, sync keys, and relayed content; avoid sensitive logs.
- **Accessibility:** keyboard-operable controls, screen-reader names, visible focus, adequate contrast, and clear non-color status cues.
- **Resource use:** local transcription may consume device CPU/GPU, memory, battery, and storage; show model/storage requirements and allow users to manage downloaded models/audio.
- **Platform integrity:** never steal focus from the target app during Dictation; handle OS permission denial and unsupported shortcut/input APIs with useful instructions.
- **Observability:** diagnostics should help debug without collecting raw audio or transcript content by default.
- **Honest claims:** label experimental languages/platform combinations and do not imply guaranteed accuracy for low-resource or code-switched speech without evaluation.

## 15. Suggested acceptance criteria by workflow

### Dictation happy path

1. User focuses a text field and presses Ctrl+Space.
2. The pill appears without stealing focus and shows a waveform derived from actual microphone input.
3. User presses Ctrl+Space again or clicks the checkmark.
4. The pill shows Thinking; if Rewrite is enabled, it then shows Rewriting.
5. Final text appears in the previously focused field. Default mode preserves verbatim wording; enabled Rewrite inserts rewritten wording directly.
6. Cancel via X discards the capture and inserts nothing.

### Dictation offline/limit path

1. With Rewrite enabled but cloud unavailable or allowance exhausted, local transcription still runs.
2. The local transcript is inserted and the UI clearly says Rewrite was skipped/locked.
3. The app does not later replace the inserted text silently.

### Lecture/Meeting outage or quota path

1. User sees a recording consent warning before starting and a persistent recording indicator while capturing.
2. Recording and local transcription continue if connectivity drops or paid quota is exhausted.
3. The source transcript is retained; translation is retained as a separate output when performed.
4. Paid analysis is queued only when allowed by connectivity and entitlement; at quota exhaustion it is blocked and the user is offered an upgrade path.
5. The original audio remains until user deletion; export remains available.

### Meeting speaker editing

1. A timestamped transcript is available before diarization finishes.
2. For an entitled online session, post-session diarization assigns stable speaker IDs to timestamped spans and displays provisional names such as “Speaker 1”.
3. User can rename a speaker, correct an incorrect span assignment, or merge duplicate labels; edits persist and generated meeting outputs reflect corrected identities where practical.
4. If offline, over limit, or diarization fails, keep the transcript/audio intact and clearly indicate that automatic speaker labels are pending, unavailable, or failed. Never invent speaker assignments.
5. Dictation latency and insertion are unaffected by diarization; no diarization work runs in Dictation mode.

## 16. Scope boundaries and not-yet-decided items

These discussions have not yet finalized:

- Subscription prices, exact usage quotas, fair-use policy, reset timing, and whether usage is measured by audio minutes, processing units, or both.
- Whether translation is automatic for every non-English utterance, user-selected per mode, or based on a preferred output language. English is the requested target in the stated examples; broader output-language controls need design.
- Language identification granularity and verified language/quality list, especially Twi-English code-switching and regional accents.
- Whether a user can opt out of storing original audio for Lecture/Meeting, despite the current default of retaining it until deletion.
- Audio formats, export templates, searchable history details, retention UI, backups, and storage-management thresholds.
- How Lecture/Meeting stop confirmation works, whether either mode supports pause/resume, and how recovery behaves after a crash. The Dictation pill itself has no pause action.
- Exact behavior for subscription expiration mid-session, usage reset, and processing queued while the user is over limit.
- Diarization provider/model, supported languages and accents, accuracy targets, processing-time budget, confidence/review behavior, cloud payload and deletion guarantees, and whether an optional separately downloaded local model should be offered.
- Rewrite tone controls, supported output languages, and whether rewriting can be invoked on a selected portion of history.
- Whether cloud services permanently retain operational metadata, and precise server-side deletion guarantees.
- Google/Apple sign-in behavior on each client platform and account recovery when a provider account is lost.
- Sync rollout timing, encryption/key recovery model, per-category toggles, conflict policy, and device limits.
- Linux compatibility matrix and per-distro packaging/support strategy.
- macOS/mobile release sequencing and which modes/features are technically viable on each platform.
- Exact visual system, theme defaults, navigation structure, onboarding, and detailed screen designs.

## 17. Current implementation status

The repository currently contains an evolving Linux-first transcription daemon and panel, plus a Windows Tauri client in testing. Local session storage now supports timestamped transcript segments and optional stable speaker IDs, and users can persist speaker display names; **automatic diarization is not implemented**. Existing low-level recording/transcription/provider functionality should be audited before implementation planning is broken into tickets. The target product described here is substantially broader than the current application: account/subscriptions, entitlement/quota handling, polished rewrite, long-form Lecture and Meeting workflows, structured notes, automatic diarization and editing, multilingual code-switch evaluation, durable session history, and encrypted sync require product and technical design work.

Before public availability, verify actual behavior and update this section with a feature matrix. Keep the release/download page accurate; do not present planned or experimental features as shipped.

