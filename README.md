# Wicx — Voice-to-Agent Transcription

A Linux-first, application-agnostic voice-input daemon. Your desktop environment owns the global shortcut; this project records, transcribes, and inserts text into whichever application currently has focus. It never talks to an editor or coding agent directly.

Daemon: Python. UI: GNOME Shell extension (JavaScript/GJS) in `extension/`.
The daemon owns the work; the panel only displays state and sends commands.

## Install

For GNOME on Wayland on Debian/Ubuntu-based Linux, open Terminal and run:

```bash
curl -fsSL https://raw.githubusercontent.com/dem-5on/whisper/main/install.sh | bash
```

The installer asks for permission to install SoX, ydotool, PipeWire tools, and
Python support; creates an isolated Python environment; installs and starts the
user daemon; and installs the GNOME panel extension. It keeps existing settings
and keys. Sign out and back in once if GNOME doesn't show the panel icon after
installation. The local `base` model is downloaded the first time the daemon
starts, so the first launch needs internet access.

The current download page is hosted at `https://whisper.intejers.com/`; it links to
the versioned files published on the project's
[GitHub Releases page](https://github.com/dem-5on/whisper/releases).
The repository, domain, and release asset names are legacy addresses retained
for compatibility during the Wicx rebrand.

To remove the app later while keeping settings, keys, recordings, and
transcripts:

```bash
bash ~/.local/share/whisper/uninstall.sh
```

For other Linux distributions, use `make package` to create a source bundle and
a GNOME extension ZIP under `dist/`, then follow the manual setup below.
The guided Linux installer supports GNOME on Wayland on Debian/Ubuntu; it does
not claim support for GNOME X11, KDE, or macOS. Windows is being rebuilt as a
Wicx native tray app and per-user installer; it is not available for download while
the Windows focus and installer checks are incomplete. The old PowerShell
installer is retired. Do not use `irm .../install.ps1 | iex`; it is not the
supported Windows installation path.

### Windows desktop app (in testing)

The Wicx Windows UI is a Tauri shell around the Python transcription service. The flyout is designed
to stay non-activating so dictated text continues going to the previously
focused app. The focused settings window handles provider credentials and live
transcription options. Development and verification instructions are in
[`windows-app/README.md`](windows-app/README.md). The private focus-test build
must pass its manual focus check before the daemon-enabled installer is
considered for release. Until then, the Windows download page intentionally
offers no installer or PowerShell command.

### Publishing a release

Release tags use six digits in `DDMMYY` form with no slashes, such as
`300826` (displayed to users as `30/08/26`). Before tagging, set the Python
package version in both `pyproject.toml` and `transcriber/__init__.py` to the
corresponding sortable date version `YYYY.M.D` (for that example,
`2026.8.30`), commit and push it, then push the six-digit tag. GitHub Actions
validates the date and version, builds the source bundle and GNOME extension
ZIP, and publishes both versioned files and stable `latest` download names.
The stable app download is
`https://github.com/dem-5on/whisper/releases/latest/download/whisper-linux-gnome.tar.gz`.

### Manual setup

```bash
python -m venv .venv
.venv/bin/pip install -e '.[local,dev]'
mkdir -p ~/.config/transcriber
cp config.example.yaml ~/.config/transcriber/config.yaml
```

Audio capture uses SoX's `rec`; microphone listing uses `wpctl` (PipeWire), falling back to `pactl` then `arecord -l`. On Wayland, automatic text insertion uses `ydotool` and its `ydotoold` daemon; the daemon needs access to `/dev/uinput`. Distros configure that permission differently, so if audio transcribes but isn't typed into the focused app, consult your distribution's ydotool setup instructions. This grants synthetic keyboard input and should only be configured for a trusted local user.

## Run

Start the daemon once, for example from a user service:

```bash
wicx-daemon
```

For systemd user sessions, install `systemd/transcriber.service` as
`~/.config/systemd/user/transcriber.service`, adjust `ExecStart` if the command
is not in `~/.local/bin`, then run `systemctl --user daemon-reload` and
`systemctl --user enable --now transcriber`.

Bind a desktop/compositor shortcut to `wicx toggle`; bind a second shortcut to `wicx cancel` if desired. The desktop environment—not this application—handles global shortcut detection, which makes this compatible with GNOME, KDE, Sway, Hyprland, and similar systems.

```bash
wicx toggle
wicx cancel
wicx status [--json]
wicx retry            # re-transcribe the preserved last recording
wicx last             # print the last transcript (for copy/recovery)
wicx history [--limit 50]  # list recent Dictation transcripts saved on this device
wicx sessions [--limit 50] [--offset 0] [--session-mode lecture] [--search "term"]  # page/filter saved sessions
wicx get-session --session <id>  # retrieve the full transcript and timestamped speaker segments
wicx delete-session --session <id>  # permanently delete that local history record
wicx rename-speaker --session <id> --speaker-id speaker_1 --name "Dr. Mensah"  # name a detected Meeting speaker
wicx label-segment --session <id> --segment-index 4 --speaker-id speaker_2  # correct one Meeting segment; omit speaker ID to clear
wicx merge-speakers --session <id> --speaker-id speaker_2 --target-speaker-id speaker_1  # merge duplicate Meeting identities
wicx retry-session --session <id>  # explicitly retry a failed Lecture/Meeting from its saved audio
wicx export-session --session <id> --output ./lecture.md  # export transcript and generated outputs as Markdown
wicx export-session-audio --session <id> --output ./lecture.wav  # copy the retained Lecture/Meeting recording
wicx dictionary --action list
wicx dictionary --action add --term 'CTranslate2'
wicx dictionary --action remove --term 'CTranslate2'
wicx start-session --mode lecture|meeting --consent  # start a retained long-form session
wicx stop-session  # finish the active Lecture or Meeting and transcribe locally
wicx mics [--json]    # list PipeWire/Pulse capture sources
wicx events [--since N] [--json]  # live partial/committed/final events
wicx subscribe        # persistent event stream (Ctrl-C to stop)
wicx set-provider --provider local|groq|openrouter
wicx set-model --model <id>   # local preset or provider model id; `models` lists valid ids
wicx set-live-engine --live-engine local|openai-realtime|hosted-whisper [--url wss://host/v1/live]
wicx set-live-model --model <id>  # independent live model
wicx set-mic --device default|<source-id>
wicx set-streaming [--enabled true|false]  # live partials + VAD auto-stop; omit to flip
wicx models [--json]          # transcription-capable models for the current backend
wicx set-key --provider groq|openrouter|openai|hosted-whisper [--key ...]  # hidden-prompt secret store (0600)
wicx set-key --provider groq|openrouter|openai|hosted-whisper --clear      # remove the stored key
```

Meeting speaker display names are stored with the local session and included in
Markdown exports. Local Faster-Whisper Lecture and Meeting transcripts now save
the model's timestamped text segments as well as the plain-text transcript.
Speaker IDs remain unassigned until diarization is implemented; the naming
and correction commands provide the persistence layer for that later feature.
Other backends that do not expose timestamps continue to save plain text only.

`whisper`, `whisper-daemon`, `transcriber`, and `transcriber-daemon` remain available as compatibility aliases for existing shortcuts and service setups.

Provider, model, and mic switches are written through to `~/.config/transcriber/config.yaml`,
so they survive daemon restarts. API keys are stored separately in `~/.config/transcriber/keys.env`
(mode 0600, loaded at daemon startup, never logged); `status --json` only reports key *presence*
(`has_groq_key`, `has_openrouter_key`, `has_openai_key`, `has_whisper_service_token`) so the panel can warn when a remote backend has no key.

The socket defaults to `$XDG_RUNTIME_DIR/transcriber.sock` (or `/tmp/transcriber-<uid>.sock`). Set `TRANSCRIBER_CONFIG` or pass `--config` to choose another config file.

## Live transcription

```
SoX/PipeWire capture → PCM frame queue → VAD → rolling Whisper decode
                                       └→ partial/final events → GNOME overlay
final stable text → cleanup/replacements → keyboard/clipboard delivery
```

Capture streams 16 kHz PCM frames (20–100 ms) from `rec` into a bounded
queue. The final WAV is written as frames arrive, while a separate bounded
rolling buffer feeds inference. VAD (energy-based by default, WebRTC VAD
when installed) detects speech and auto-stops the recording
after `silence_timeout_seconds` of trailing silence — leading silence
never cuts. Every `chunk_ms`, the most recent `window_seconds` are
re-decoded. The merge retains prior committed text when the rolling window
advances, then treats the newest words as provisional. Partials appear in
the panel within ~0.5–1 s when local decoding can keep pace; status reports
the most recent decode duration and lag.

Live engine selection and the local rolling engine are in
`transcriber/live_engines.py`; the OpenAI transport is isolated in
`transcriber/openai_realtime.py`. Configure `streaming.engine` and
`streaming.model` independently from the final batch provider/model. For
OpenAI live transcription, install the optional dependency and configure its key:

```bash
python -m pip install -e '.[realtime]'
wicx set-key --provider openai
wicx set-live-engine --live-engine openai-realtime
wicx set-live-model --model gpt-live-transcribe
```

This uses OpenAI's Realtime transcription WebSocket; OpenRouter remains
unchanged and can still handle final transcription. See the
[OpenAI Realtime transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription).

To stream audio to your own hosted transcription service, install the WebSocket extra and
configure its TLS endpoint and invite token on the desktop:

```bash
python -m pip install -e '.[realtime]'
wicx set-live-engine --live-engine hosted-whisper --url wss://api.example.com/v1/live
wicx set-key --provider hosted-whisper
wicx set-streaming --enabled true
```

The default server profile is `default` (faster-whisper `base` in the Docker
setup). The hosted service returns both live partials and the final transcript,
so the desktop does not run a second local or provider transcription pass.
The selected batch provider remains available when live transcription is off.

The Docker server applies CPU-friendly limits by default: one active session
globally, up to 300 seconds of audio per session, and ten session starts per
authenticated user per rolling hour. These are enforced server-side. Operators
can tune them in the Compose environment (for example, in a local `.env` file):
`WHISPER_MAX_SESSION_SECONDS`, `WHISPER_MAX_CONCURRENT`,
`WHISPER_MAX_STARTS_PER_WINDOW`, and `WHISPER_RATE_WINDOW_SECONDS`.

Self-hosted Meeting diarization runs as a separate, authenticated post-session
HTTP job API; it does not enter the live-dictation path. The initial worker uses
pyannote Community-1, which is intentionally optional. To enable it, build with
`WHISPER_DIARIZATION_BUILD=1`, accept the model conditions on Hugging Face, and
set `HF_TOKEN` for the service. Set `WHISPER_ENABLE_DIARIZATION=1` to expose
`POST /v1/diarization/jobs` and `GET /v1/diarization/jobs/{job_id}` on port 8766
(Bearer token authentication is the same as the live service). Upload uncompressed
mono PCM16 WAV at 16 kHz; uploads are capped at 1 GB and four hours, queue size is
bounded, and audio is deleted once processing finishes. Job state is currently
held in memory, so an interrupted server requires the client to retry the upload.
Job creation fails closed unless the authenticated user has the `diarization`
feature grant in the owner-only `/data/entitlements.json` file. The live cloud
transcription WebSocket likewise requires a separate `cloud_transcription`
grant. Cloud text rewriting requires `cloud_rewrite`. For prototype testing,
operators can grant or revoke these features with
`docker compose exec whisper-server whisper-server entitlement grant --user USER_ID --feature diarization`
or `docker compose exec whisper-server whisper-server entitlement grant --user USER_ID --feature cloud_transcription`
(or `--feature cloud_rewrite`)
(use `entitlement revoke` with the same arguments to remove a grant); grants may
have an `--expires-at` ISO-8601 timestamp. This manual grant store is not a
billing integration. Paystack is selected for desktop/web subscriptions: Wicx
will own the branded plans and checkout page, while Paystack securely collects
payment. The hosted service now has an optional Paystack webhook endpoint at
`POST /v1/billing/paystack/webhook` (`WHISPER_ENABLE_BILLING=1`) that verifies
Paystack's HMAC-SHA512 signature and records account-linked subscription events
in a private SQLite ledger with replay deduplication. Authenticated clients can
read their own plan summaries and effective feature access from
`GET /v1/billing/status`; provider subscription IDs and payment references are
not returned. `GET /v1/billing/plans` returns the configured customer-facing
prices, features, and renewal type without exposing Paystack plan codes.
Recurring subscribers can request Paystack's hosted subscription
management page from `POST /v1/billing/manage`; one-time plans are excluded.
Cancellation is reflected after Paystack's signed lifecycle webhook, and
non-renewing subscribers retain access through their paid period. Configure
`WHISPER_PAYSTACK_SECRET_KEY` and persist `WHISPER_BILLING_DATABASE` across
restarts. An authenticated `POST /v1/billing/checkout` endpoint can initialize
Paystack checkout when `WHISPER_PAYSTACK_PLANS` is configured as a JSON mapping
from Wicx plan IDs to server-owned pricing and feature settings. Existing
recurring plans use a Paystack plan code (for example,
`{"pro-monthly":{"code":"PLN_...","amount_minor":1250,"currency":"GHS","features":["cloud_transcription","diarization"]}}`).
One-time plans can instead set `"billing_mode":"one_time"`, a fixed
`period_days` (1–366), and allowed Paystack channels (for example,
`{"pro-monthly":{"billing_mode":"one_time","period_days":30,"channels":["mobile_money"],"amount_minor":1250,"currency":"GHS","features":["cloud_transcription","diarization"]}}`).
For these plans Paystack collects one payment, and verified successful charges
grant the configured access term; the user must manually renew with a new
checkout when it expires. Renewing early stacks the new term after the
existing paid-through date. A one-time payment is never represented as an
auto-renewing subscription.
Plans that include `cloud_transcription` may set `cloud_minutes` to a positive
integer in `WHISPER_PAYSTACK_PLANS`, e.g. `"cloud_minutes": 900`. The service
meters audio seconds against the account's current billing period, returns
used/remaining usage from `/v1/billing/status`, and caps each live session to
the remaining allowance. When omitted, that plan has no billing-period minute
allowance; existing fair-use safeguards still apply (300 seconds per session
and ten session starts per user per rolling hour). Usage records contain
counts only, not audio or transcript content.
The optional cloud rewrite endpoint is enabled with `WHISPER_ENABLE_REWRITE=1`,
`WHISPER_OPENROUTER_API_KEY`, and an operator-controlled comma-separated
`WHISPER_REWRITE_MODELS` allow-list. It exposes `POST /v1/rewrite` for
authenticated accounts with the `cloud_rewrite` entitlement, plus
`GET /v1/rewrite/models` to return the allowed candidates. The first model in
the allow-list is the default, and rewrite requests may select another listed
model for comparison. OpenRouter credentials remain on the service host. Rewrite
providers implement a small interface so a local model can be added separately
without changing the authenticated API. On Windows, Dictation rewrite is
available in Settings and is off by default. When enabled, the local
transcription completes first, then the native app sends its text to the
authenticated Wicx endpoint; account credentials stay in the OS credential
store, not in the UI or transcription sidecar. The panel shows “Rewriting”
during that step. If rewriting fails or the account lacks the entitlement, the
original transcript is inserted and the panel reports the fallback. This
integration currently covers Dictation only; Lecture/Meeting post-processing
and an on-device rewrite provider remain future work.
Use actual approved pricing and Paystack plan codes in deployment config; the
examples are illustrative only. The server supplies the plan code (for
recurring plans), amount, payment channel, and account metadata; clients cannot
set billing values or the account ID. Signed successful-charge
events are accepted only when Paystack's verified reference, amount, and
currency match the stored checkout and configured plan. Checkout initialization
currently accepts a payer email in the authenticated request. A private identity
store maps already-verified provider subjects to stable Wicx account IDs without
linking by email; Google/Apple sign-in is implemented in the optional hosted
OAuth service.
To enable sign-in routes, set `WHISPER_ENABLE_OAUTH=1` and configure
`WHISPER_OAUTH_BASE_URL` to the public HTTPS origin. Google requires
`WHISPER_GOOGLE_CLIENT_ID` and `WHISPER_GOOGLE_CLIENT_SECRET`. Apple requires
`WHISPER_APPLE_CLIENT_ID` (the Services ID), `WHISPER_APPLE_TEAM_ID`,
`WHISPER_APPLE_KEY_ID`, and `WHISPER_APPLE_PRIVATE_KEY_FILE`; register the
corresponding HTTPS callback URLs ending in
`/v1/auth/oauth/google/callback` and `/v1/auth/oauth/apple/callback` with the
providers. OAuth transaction records and account identities/sessions are kept
in private SQLite databases (`WHISPER_OAUTH_DATABASE` and
`WHISPER_ACCOUNTS_DATABASE` can override their paths). Provider secrets and
Apple's signing key remain server-side. The desktop obtains a one-time
PKCE-bound handoff and exchanges it at `POST /v1/auth/session/exchange` for a
30-day bearer session. The current desktop clients still need deep-link
registration and sign-in UI wiring before this flow is usable end to end.
Each checkout reference is stored against the
authenticated account, and successful charge webhooks are checked against
Paystack's transaction-verification API before they update the billing ledger.
Verified subscription status now feeds the hosted `cloud_transcription` and
`diarization` gates according to each configured plan's `features` list; access
expires at the subscription period end, and non-renewing subscriptions remain
entitled only through that date. Operator-managed grants remain available for
testing. Stable sign-in identities and the remaining account, refund, dispute,
and support workflows are still needed before production rollout. Never grant access from a frontend success
callback or store raw card details. Paystack's transaction initialization API
accepts a server-side recurring plan code and returns a hosted authorization
URL; see [Paystack's transaction API](https://paystack.com/docs/api/transaction/).
Paystack
supports recurring card billing; Ghana mobile-money payments are one-time, so
they need a manual renewal flow. iOS App Store and Google Play billing will be
separate adapters later and must feed the same account entitlement system.
The `whisper-server diarize-file --audio meeting.wav --device cpu` command remains
available for a local model smoke test. Model weights download from Hugging Face;
meeting audio is processed by the Wicx host, not sent to a third-party API.

The local model is loaded once at daemon startup (warmed with silence),
not per recording. Event types are `partial`, `committed`, `final`,
`speech_started`, and `speech_ended`, each with a session ID and revision:
poll `status` (`live_text`, `committed_text`, `provisional_text`,
`speech_active`), fetch deltas with `events --since N`, or hold a push
connection with `subscribe`. Local live partials require
`local_engine: faster-whisper`; OpenAI live transcription requires
`OPENAI_API_KEY` and the optional dependency. Either live engine can be paired
with any final batch backend. Live text never reaches the focused
app — only the finalized transcript is inserted. Tune everything under
`streaming:` in `config.yaml`; set `streaming.enabled: false` for the
legacy record-to-WAV-then-transcribe path. The stock VAD needs only NumPy;
installing `webrtcvad` (optional) upgrades per-frame speech classification;
the default 20 ms capture frame uses it directly.

### GNOME panel (`extension/`)

Install the `transcriber@local` extension (targets GNOME 45–50):

```bash
mkdir -p ~/.local/share/gnome-shell/extensions/transcriber@local
cp extension/{metadata.json,extension.js,stylesheet.css} ~/.local/share/gnome-shell/extensions/transcriber@local/
gnome-extensions enable transcriber@local
# Wayland: log out and back in (or restart the shell on X11 with Alt+F2 `r`).
```

The panel shows idle / red pulsing + elapsed timer while recording (with
the live partial rendered in the menu, polled every 500 ms) /
spinner while processing (showing the last partial as “Finalizing”) /
error with reason, plus provider, model, and mic
submenus, `Re-transcribe last`, `Copy last transcript`, a `Show transcript`
switch for the full completed transcript, rich done-notifications
(backend, duration, opt-in preview), a key-missing hint when a remote backend
has no stored key, and `Start daemon` when the socket is unreachable.
The Model submenu lists local presets for the `local` backend and the
provider's transcription-capable models (cached 24h) for `groq`/`openrouter`.
Configuration lives in the daemon config file; the only UI
display option is `ui.show_last_transcript` (default off).

## Safety and privacy

Local transcription is the default. Groq and OpenRouter are opt-in and read their credentials only from `GROQ_API_KEY` and `OPENROUTER_API_KEY`; credentials and transcript text are never written to logs. `submit_after_insert` defaults to `false`, so text is inserted but Enter is not pressed. The last recording (`~/.local/share/transcriber/last.wav`) and transcript (`last.txt`) are kept on disk for retry/copy recovery.

## State model

`IDLE → RECORDING → PROCESSING → DELIVERING → IDLE`, plus `ERROR` with a short reason on transcription/processing/delivery failure. Cancellation from `RECORDING` discards audio and returns to `IDLE`; cancellation from `ERROR` clears the error. Failures preserve the last recording so `retry` / `Re-transcribe last` can recover without re-speaking.
