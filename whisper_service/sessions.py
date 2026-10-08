"""Transport-independent live-session orchestration for the hosted service.

The WebSocket layer will translate JSON/binary frames to this API. This module
owns profile allow-listing, per-user/global concurrency, audio bounds, event
revisions, and cleanup; it deliberately does not retain submitted audio.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Awaitable, Callable, Protocol
from uuid import uuid4


PCM_BYTES_PER_SECOND = 16_000 * 2  # 16 kHz, mono, signed 16-bit PCM


@dataclass(frozen=True)
class ModelProfile:
    """A server-owned model selection; clients only send this profile ID."""

    profile_id: str
    model: str
    devices: tuple[str, ...] = ("cpu", "cuda")


class ServiceError(RuntimeError):
    """A safe, stable error suitable for mapping to a wire-protocol event."""

    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.message = message


class WorkerSession(Protocol):
    """One isolated inference stream, owned by the service session manager."""

    async def submit_audio(self, pcm_s16le: bytes) -> str | None:
        """Consume audio and optionally return a complete current hypothesis."""
        ...

    async def finish(self) -> str:
        """Flush decoding and return the authoritative final transcript."""
        ...

    async def cancel(self) -> None:
        """Abort decoding and discard session-local audio/state."""
        ...


class TranscriptionWorker(Protocol):
    async def open_session(self, profile: ModelProfile, language: str | None) -> WorkerSession: ...


@dataclass(frozen=True)
class SessionLimits:
    max_audio_seconds: int = 600
    max_audio_bytes: int = PCM_BYTES_PER_SECOND * 600
    max_concurrent_global: int = 2
    max_concurrent_per_user: int = 1
    max_chunk_bytes: int = PCM_BYTES_PER_SECOND // 10  # 100 ms

    def __post_init__(self) -> None:
        if self.max_audio_seconds < 1 or self.max_audio_seconds > 3600 or self.max_audio_bytes < 2:
            raise ValueError("audio duration must be between 1 and 3600 seconds, with a positive byte limit")
        if self.max_concurrent_global < 1 or self.max_concurrent_per_user < 1:
            raise ValueError("concurrency limits must be positive")
        if self.max_chunk_bytes < 2 or self.max_chunk_bytes % 2:
            raise ValueError("max_chunk_bytes must be a positive even number")


class SessionManager:
    """Creates isolated sessions and applies resource limits before inference."""

    def __init__(self, worker: TranscriptionWorker, profiles: tuple[ModelProfile, ...], *,
                 device: str, limits: SessionLimits | None = None) -> None:
        self._worker = worker
        self._profiles = {profile.profile_id: profile for profile in profiles}
        if not self._profiles:
            raise ValueError("at least one model profile is required")
        if len(self._profiles) != len(profiles):
            raise ValueError("model profile IDs must be unique")
        self._device = device
        self._limits = limits or SessionLimits()
        self._lock = asyncio.Lock()
        self._sessions: dict[str, LiveSession] = {}
        self._starting_by_user: dict[str, int] = {}
        self._starting_total = 0

    @property
    def limits(self) -> SessionLimits:
        return self._limits

    async def start(self, user_id: str, profile_id: str, language: str | None = None) -> "LiveSession":
        if not user_id:
            raise ServiceError("unauthorized", "Authentication is required.")
        profile = self._profiles.get(profile_id)
        if profile is None:
            raise ServiceError("invalid_profile", "The requested transcription profile is unavailable.")
        if self._device not in profile.devices:
            raise ServiceError("profile_unavailable", "The profile is not available on this server's inference device.")
        async with self._lock:
            user_sessions = sum(session.user_id == user_id for session in self._sessions.values())
            user_starting = self._starting_by_user.get(user_id, 0)
            if user_sessions + user_starting >= self._limits.max_concurrent_per_user:
                raise ServiceError("session_limit", "Your concurrent session limit was reached.", retryable=True)
            if len(self._sessions) + self._starting_total >= self._limits.max_concurrent_global:
                raise ServiceError("service_busy", "The service is at capacity. Try again shortly.", retryable=True)
            self._starting_total += 1
            self._starting_by_user[user_id] = user_starting + 1

        try:
            worker_session = await self._worker.open_session(profile, language)
        except BaseException as exc:
            await self._release_start(user_id)
            if not isinstance(exc, Exception):
                raise
            raise ServiceError("inference_unavailable", "The transcription worker could not start.", retryable=True) from exc

        session = LiveSession(uuid4().hex, user_id, profile, worker_session, self._limits, self._release_session)
        async with self._lock:
            self._starting_total -= 1
            user_starting = self._starting_by_user.get(user_id, 1) - 1
            if user_starting:
                self._starting_by_user[user_id] = user_starting
            else:
                self._starting_by_user.pop(user_id, None)
            self._sessions[session.session_id] = session
        return session

    async def _release_start(self, user_id: str) -> None:
        async with self._lock:
            self._starting_total -= 1
            user_starting = self._starting_by_user.get(user_id, 1) - 1
            if user_starting:
                self._starting_by_user[user_id] = user_starting
            else:
                self._starting_by_user.pop(user_id, None)

    async def _release_session(self, session_id: str) -> None:
        async with self._lock:
            self._sessions.pop(session_id, None)

    async def active_count(self) -> int:
        async with self._lock:
            return len(self._sessions)


class LiveSession:
    def __init__(self, session_id: str, user_id: str, profile: ModelProfile,
                 worker: WorkerSession, limits: SessionLimits,
                 release: Callable[[str], Awaitable[None]]) -> None:
        self.session_id = session_id
        self.user_id = user_id
        self.profile = profile
        self._worker = worker
        self._limits = limits
        self._release = release
        self._lock = asyncio.Lock()
        self._state = "open"
        self._audio_bytes = 0
        self._revision = 0

    async def submit_audio(self, pcm_s16le: bytes) -> dict[str, object] | None:
        if not pcm_s16le or len(pcm_s16le) % 2:
            raise ServiceError("invalid_audio", "Audio chunks must contain complete PCM16 samples.")
        if len(pcm_s16le) > self._limits.max_chunk_bytes:
            raise ServiceError("chunk_too_large", "The audio chunk exceeds the accepted size.")
        async with self._lock:
            self._require_open()
            next_size = self._audio_bytes + len(pcm_s16le)
            max_duration_bytes = self._limits.max_audio_seconds * PCM_BYTES_PER_SECOND
            if next_size > min(self._limits.max_audio_bytes, max_duration_bytes):
                await self._abort_locked()
                raise ServiceError("session_limit", "The recording exceeded the session duration or audio limit.")
            self._audio_bytes = next_size
            try:
                hypothesis = await self._worker.submit_audio(pcm_s16le)
            except Exception as exc:
                await self._abort_locked()
                raise ServiceError("inference_failed", "Live transcription failed.", retryable=True) from exc
            if hypothesis is None:
                return None
            self._revision += 1
            return {"type": "transcript.partial", "revision": self._revision, "text": hypothesis}

    async def finish(self) -> dict[str, object]:
        async with self._lock:
            self._require_open()
            self._state = "finishing"
            try:
                transcript = await self._worker.finish()
            except BaseException as exc:
                self._state = "closed"
                try:
                    await asyncio.shield(self._worker.cancel())
                except BaseException:
                    pass
                finally:
                    await self._release(self.session_id)
                if not isinstance(exc, Exception):
                    raise
                raise ServiceError("inference_failed", "Final transcription failed.", retryable=True) from exc
            self._state = "closed"
            await self._release(self.session_id)
            return {"type": "transcript.final", "text": transcript}

    async def cancel(self) -> None:
        async with self._lock:
            if self._state != "open":
                return
            await self._abort_locked()

    async def disconnect(self) -> None:
        """Disconnect cancels and discards the session; no hidden continuation."""
        await self.cancel()

    def _require_open(self) -> None:
        if self._state != "open":
            raise ServiceError("session_closed", "The transcription session is already closed.")

    async def _abort_locked(self) -> None:
        if self._state != "open":
            return
        self._state = "closed"
        try:
            await asyncio.shield(self._worker.cancel())
        except BaseException:
            # Cleanup is best-effort; do not retain a service slot if the
            # inference runtime itself fails while disposing session state.
            pass
        finally:
            await self._release(self.session_id)
