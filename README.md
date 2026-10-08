# Whisper — Voice-to-Agent Transcription

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

The download page is intended for `https://whisper.intejers.com/`; it links to
the versioned files published on the project's
[GitHub Releases page](https://github.com/dem-5on/whisper/releases).

To remove the app later while keeping settings, keys, recordings, and
transcripts:

```bash
bash ~/.local/share/whisper/uninstall.sh
```

For other Linux distributions, use `make package` to create a source bundle and
a GNOME extension ZIP under `dist/`, then follow the manual setup below.
Currently, the guided installer supports GNOME on Wayland on Debian/Ubuntu; it
does not claim support for GNOME X11, KDE, Windows, or macOS.

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
whisper-daemon
```

For systemd user sessions, install `systemd/transcriber.service` as
`~/.config/systemd/user/transcriber.service`, adjust `ExecStart` if the command
is not in `~/.local/bin`, then run `systemctl --user daemon-reload` and
`systemctl --user enable --now transcriber`.

Bind a desktop/compositor shortcut to `whisper toggle`; bind a second shortcut to `whisper cancel` if desired. The desktop environment—not this application—handles global shortcut detection, which makes this compatible with GNOME, KDE, Sway, Hyprland, and similar systems.

```bash
whisper toggle
whisper cancel
whisper status [--json]
whisper retry            # re-transcribe the preserved last recording
whisper last             # print the last transcript (for copy/recovery)
whisper mics [--json]    # list PipeWire/Pulse capture sources
whisper events [--since N] [--json]  # live partial/committed/final events
whisper subscribe        # persistent event stream (Ctrl-C to stop)
whisper set-provider --provider local|groq|openrouter
whisper set-model --model <id>   # local preset or provider model id; `models` lists valid ids
whisper set-live-engine --live-engine local|openai-realtime|hosted-whisper [--url wss://host/v1/live]
whisper set-live-model --model <id>  # independent live model
whisper set-mic --device default|<source-id>
whisper set-streaming [--enabled true|false]  # live partials + VAD auto-stop; omit to flip
whisper models [--json]          # transcription-capable models for the current backend
whisper set-key --provider groq|openrouter|openai|hosted-whisper [--key ...]  # hidden-prompt secret store (0600)
whisper set-key --provider groq|openrouter|openai|hosted-whisper --clear      # remove the stored key
```

`transcriber` and `transcriber-daemon` remain available as compatibility aliases for existing shortcuts and service setups.

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
whisper set-key --provider openai
whisper set-live-engine --live-engine openai-realtime
whisper set-live-model --model gpt-live-transcribe
```

This uses OpenAI's Realtime transcription WebSocket; OpenRouter remains
unchanged and can still handle final transcription. See the
[OpenAI Realtime transcription guide](https://developers.openai.com/api/docs/guides/realtime-transcription).

To stream audio to your own Whisper service, install the WebSocket extra and
configure its TLS endpoint and invite token on the desktop:

```bash
python -m pip install -e '.[realtime]'
whisper set-live-engine --live-engine hosted-whisper --url wss://api.example.com/v1/live
whisper set-key --provider hosted-whisper
whisper set-streaming --enabled true
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
