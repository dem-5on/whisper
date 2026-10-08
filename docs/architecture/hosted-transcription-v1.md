# Hosted transcription service: v1 boundary and protocol

## Status and goal

This document proposes the first hosted-service boundary for Whisper. It is an
architecture and protocol plan, not an implementation or a commitment to a
particular model or latency target. The initial client is the existing
Linux/GNOME application. The service API is platform-neutral so later Windows
and macOS clients can reuse it.

The service is intended for an invite-only deployment hosted by the project
owner, initially for friends and family. It must not require users to supply an
OpenAI or OpenRouter key. The service owner pays for and operates the VPS and
sets usage limits. The existing local and OpenRouter transcription paths remain
available and unchanged.

## Responsibilities and trust boundary

### Desktop client (including its local daemon)

- Requests microphone permission and selects the audio source.
- Captures and frames audio locally.
- Owns the recording UI, hotkeys, cancellation, and reconnect behavior.
- Shows partial transcript updates and delivers final text into the focused
  application using the existing local delivery mechanism.
- May retain a local recording for an explicitly requested retry, according to
  the user's local retention settings.
- Keeps user credentials in the operating system credential store, not in the
  ordinary YAML configuration file.

### Hosted service

- Authenticates users, enforces quotas and concurrent-session limits, and
  dispatches authorized sessions to an inference worker.
- Returns partial and final transcript events, or a structured error.
- Owns model loading, caching, and server-side resource management.
- Does **not** access the user's microphone, clipboard, focused window, or
  keyboard; it never types transcript text into another application.

Authentication/session handling and model inference should be separate
interfaces. A service API should not depend directly on one model library, and
an inference worker should not own user authentication or transport concerns.

## Hardware selection

The service API is hardware-neutral. At startup, an `auto` deployment mode
should probe for a *usable supported accelerator* (device, compatible driver,
runtime, and sufficient memory), not merely a GPU device name. If a supported
GPU worker can initialize, use it; otherwise select the CPU worker and report
the reason in an operator-facing health/status command. The operator can also
force `cpu` or a specific supported GPU backend in server configuration.

Worker selection and model-profile selection are related but distinct: each
profile declares which worker backends and resource requirements it supports.
If the requested profile cannot run on the detected device, fail with a clear
configuration error rather than silently changing model quality. CPU and GPU
workers implement the same inference interface and session protocol, so adding
hardware support must not change the desktop client or the OpenRouter path.
The first release should ship the CPU worker and only GPU backends that can be
tested on supported hardware; detection must not imply every GPU vendor is
supported.

### Current implementation slice

The first server-side slice adds a standalone `whisper_service` package and an
operator diagnostic command for device selection. It recognizes CPU and
CTranslate2 CUDA availability; CUDA is the only GPU backend considered in this
slice. This is runtime detection, not a guarantee that every model fits in
device memory. Actual worker initialization must validate the chosen profile,
and server health must surface initialization failures. The HTTP/WebSocket
service, authentication, and inference worker are subsequent implementation
steps.

## Proposed v1 API

Use a versioned HTTPS service base URL and WebSocket for live sessions:

- `wss://<service-host>/v1/live` — one live recording per connection.
- `POST https://<service-host>/v1/transcriptions` — batch transcription for
  stream-off, retry, and re-transcribe workflows.

All service traffic uses TLS. A reverse proxy may terminate TLS and forward to
the application on a private interface. Do not expose an unauthenticated model
worker directly to the public network.

### Authentication

The WebSocket upgrade and batch request carry an authorization header:

```http
Authorization: Bearer <user-scoped-token>
```

Never put credentials in a URL or query string. Tokens must be revocable and
scoped to a user; the service must not ship a shared provider secret in the
desktop package. The invite-only pilot can issue user credentials out of band.
Before broad distribution, use a native-app sign-in flow such as OAuth
Authorization Code with PKCE, with credentials stored in the OS keychain.

### Live session messages

After the authenticated WebSocket connects, the client sends a JSON start
message. Model selection should use an allow-listed server profile rather than
an arbitrary model name or filesystem path.

```json
{
  "type": "session.start",
  "audio": {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1},
  "profile": "default",
  "language": "auto"
}
```

The server either rejects the request with a structured error or replies with
`session.ready`, including a session ID and accepted limits. The client then
sends ordered binary audio messages. The existing client audio path is 16 kHz,
mono PCM16; it can bundle its small frames into bounded chunks (initially no
larger than about 100 ms) to reduce transport overhead without adding much
latency.

The client sends JSON control messages:

```json
{"type": "session.finish"}
```

or, to abort and discard the current result:

```json
{"type": "session.cancel"}
```

The service emits JSON events:

```json
{"type": "transcript.partial", "revision": 4, "text": "recognized words so far"}
{"type": "transcript.final", "text": "the finalized transcript"}
{"type": "session.closed"}
```

Partial `text` is the complete current hypothesis, not a delta. `revision`
increases monotonically so the client can reject stale updates. On finish, the
service sends one authoritative final transcript before closing. Errors use a
stable machine-readable code and a user-safe message:

```json
{"type": "session.error", "code": "session_limit", "retryable": false, "message": "The session limit was reached."}
```

The protocol must define maximum audio duration/bytes, heartbeat behavior,
queue bounds, and what happens after disconnect. The server must apply
backpressure or terminate an over-limit session; it must not accumulate
unbounded audio in memory. A disconnected session should be cancelled and
discarded after a short, documented grace period, not silently continued.

### Batch requests

The batch endpoint accepts an authenticated audio upload plus an allow-listed
profile and optional language. It returns a final transcript or the same
structured error format. Batch mode supports stream-off, retries, and
re-transcribing a locally retained recording; it is not a fallback that the
client silently invokes after a live error.

## Client integration and provider behavior

Add a separate self-hosted live-engine adapter implementing the existing live
engine interface. Keep `openai_realtime.py` and the existing OpenRouter batch
backend unchanged. The hosted client adapter owns WebSocket transport and maps
service events into the daemon's existing partial/final event interface.

The configuration model should distinguish the final transcription provider
from the live transport/engine, as it does today. Add a provider/capability
entry for “Whisper Server” (self-hosted), with live and batch capabilities and
the configured service URL. Do not store an access token in the regular config
file.

Avoid duplicate inference when the hosted service is both the selected live
engine and final provider: use the authoritative `transcript.final` returned by
the live session. If the user selects a different batch provider such as
OpenRouter for final transcription, live text remains a preview and the current
selected batch provider remains authoritative. This preserves existing
provider semantics and makes the additional cost/data transfer explicit.

When live mode is off, or the user requests a retry/re-transcription, use the
selected batch provider. Do not silently fall back to OpenAI, OpenRouter, or a
hosted endpoint: fallback could incur cost or send private audio to a different
operator. Show a clear error and let the user choose another provider.

## Privacy, security, and operations

- Default to no server-side audio or transcript persistence. Process live audio
  in bounded session memory and clear it on finish, cancel, disconnect expiry,
  or error.
- Do not log audio, transcript text, bearer tokens, or other credentials. Logs
  may include request/session IDs, user pseudonymous ID, byte counts, timing,
  selected profile, and stable error codes.
- Apply per-user duration, request-rate, and concurrent-session limits, plus a
  service-wide resource cap. Return clear limit errors.
- Keep inference workers behind a narrow interface and isolate session state
  between users. Restrict worker network access where practical.
- Make retention, quotas, service operator, and data handling visible to users
  before they enable the hosted provider.
- Provide a way to revoke a lost device's credential without disabling every
  other user.

## Test gates

Before enabling hosted transcription for real users, test:

1. Protocol conformance with a fake inference worker: start, ordered audio,
   partial revisions, finish/final, cancel, disconnect, and structured errors.
2. Authentication, revocation, profile allow-listing, quotas, concurrent
   sessions, and strict session isolation.
3. Privacy invariants: no audio/transcript persistence or content in logs by
   default; cancellation and failures clear temporary state.
4. Client adapter behavior using a fake WebSocket server, including stale
   partials, network loss, cancellation, and final transcript reuse.
5. Regression tests proving local transcription, OpenAI Realtime, and
   OpenRouter batch behavior remain unchanged.
6. Loopback and two-device LAN smoke tests, followed by external TLS staging.
   Measure end-to-end partial and final latency at p50/p95, plus real-time
   factor and memory under concurrent sessions.

Do not promise a latency or concurrency target until the actual VPS CPU, RAM,
GPU availability, and hosting region are known and benchmarked. Network latency
and model inference time are separate measurements.

## Delivery sequence

1. Review and approve this boundary and protocol before touching the existing
   provider pipelines.
2. Record VPS hardware, region, domain/TLS plan, and initial user/quota policy.
3. Implement a standalone service shell and protocol tests with a fake worker.
4. Add a model worker behind the service interface and benchmark candidate
   models on the target VPS.
5. Implement the separate desktop hosted-service adapter and final-result reuse.
6. Validate loopback, LAN, and external staging; then invite a small pilot.
7. Add other desktop platforms as independent clients of the same API.

Initial operational defaults: invite-only access, TLS at a reverse proxy,
short-lived/revocable user credentials, no server-side transcript/audio
retention, and conservative per-user concurrency and duration limits.

## Decisions still needed before implementation

- VPS has a 4-core CPU, 8 GB RAM, and 512 GB disk; it is currently understood
  to be CPU-only. The exact CPU model/generation and hosting region are still
  needed for a meaningful inference and network-latency benchmark. Disk size is
  ample for model files but does not by itself indicate transcription speed.
- Which GPU backends to support in the first release (automatic detection must
  only select backends we explicitly support and test).
- Domain and TLS termination approach.
- Whether the invite-only pilot uses issued tokens initially or OAuth/PKCE from
  the first release.
- Initial maximum recording duration, per-user concurrency, and usage quota.
