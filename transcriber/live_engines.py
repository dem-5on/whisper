"""Pluggable live-transcription engines.

The daemon owns microphone capture, VAD, session state, and final delivery.
An engine owns only how live audio becomes partial/final transcript events.
Keeping that boundary explicit prevents batch providers (such as OpenRouter)
from being mixed into a streaming transport.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import threading
import time
from typing import Any, Callable, Protocol

from .config import StreamingConfig, TranscriptionConfig


class LiveEngine(Protocol):
    """A provider-specific source of live transcript events."""

    def run(self, stop: threading.Event, generation: int, session_id: str) -> None: ...

    def submit_audio(self, pcm_16k: bytes) -> None: ...

    def commit_turn(self) -> None: ...

    def close(self) -> None: ...


class LiveEngineUnavailable(RuntimeError):
    """Raised when the selected provider has no live engine."""


@dataclass(frozen=True)
class LocalEngineCallbacks:
    """Daemon operations intentionally exposed to the local live engine."""

    is_active: Callable[[int, str], bool]
    has_speech: Callable[[], bool]
    window_bytes: Callable[[int], bytes | None]
    create_backend: Callable[[], Any]
    decode: Callable[[Any, Any], str]
    publish_partial: Callable[[str, str, int], None]
    report_metrics: Callable[[float, float, str], None]


@dataclass(frozen=True)
class RemoteLiveCallbacks:
    publish_partial: Callable[[str, str, int], None]
    report_metrics: Callable[[float, float, str], None]


class LocalRollingEngine:
    """Rolling-window faster-whisper implementation for local partials."""

    def __init__(self, transcription: TranscriptionConfig, streaming: StreamingConfig,
                 callbacks: LocalEngineCallbacks) -> None:
        self._transcription = transcription
        self._streaming = streaming
        self._callbacks = callbacks

    def submit_audio(self, pcm_16k: bytes) -> None:
        # The local engine reads the recorder's bounded rolling PCM window.
        # It deliberately does not keep a second audio queue.
        _ = pcm_16k

    def commit_turn(self) -> None:
        # VAD endpointing is handled by the daemon; final transcription still
        # runs over the preserved WAV after capture stops.
        return

    def close(self) -> None:
        return

    def run(self, stop: threading.Event, generation: int, session_id: str) -> None:
        chunk_seconds = max(0.2, self._streaming.chunk_ms / 1000.0)
        try:
            backend = self._callbacks.create_backend()
            if not hasattr(backend, "transcribe_samples"):
                self._callbacks.report_metrics(0.0, 0.0, "Local engine does not support sample streaming")
                return
            import numpy as np
        except Exception as exc:
            self._callbacks.report_metrics(0.0, 0.0, type(exc).__name__)
            return

        next_decode = time.monotonic() + chunk_seconds
        while not stop.is_set():
            delay = next_decode - time.monotonic()
            if delay > 0:
                stop.wait(delay)
            if stop.is_set() or not self._callbacks.is_active(generation, session_id):
                return
            if not self._callbacks.has_speech():
                next_decode = time.monotonic() + chunk_seconds
                continue

            window = self._callbacks.window_bytes(self._streaming.window_seconds)
            if not window or len(window) < 3200:  # < ~0.1 seconds at 16 kHz PCM16
                next_decode = time.monotonic() + chunk_seconds
                continue
            try:
                samples = np.frombuffer(window, dtype="<i2").astype(np.float32) / 32768.0
                started = time.monotonic()
                hypothesis = self._callbacks.decode(backend, samples)
                elapsed = time.monotonic() - started
                lag = max(0.0, time.monotonic() - next_decode)
                self._callbacks.report_metrics(elapsed, lag, "")
            except Exception as exc:
                self._callbacks.report_metrics(0.0, 0.0, str(exc)[:120] or type(exc).__name__)
                next_decode = time.monotonic() + chunk_seconds
                continue
            if hypothesis:
                self._callbacks.publish_partial(session_id, hypothesis, generation)
            # Skip stale cadence ticks: fresh audio is more valuable than a
            # backlog of old rolling-window decodes.
            next_decode += chunk_seconds
            if next_decode < time.monotonic():
                next_decode = time.monotonic()


def local_engine_supported(config: TranscriptionConfig) -> tuple[bool, str]:
    if config.local_engine != "faster-whisper":
        return False, "Live partials require the faster-whisper engine"
    return True, ""


def make_live_engine(transcription: TranscriptionConfig, streaming: StreamingConfig,
                     callbacks: LocalEngineCallbacks,
                     remote_callbacks: RemoteLiveCallbacks | None = None) -> LiveEngine:
    """Select the live engine independently from batch transcription.

    Batch providers are intentionally not implicitly adapted into streaming
    providers. OpenAI Realtime has its own module and transport implementation.
    """
    # Selection is by provider/engine identity, while the chosen model and
    # its runtime settings are passed into that engine without changing batch routing.
    if streaming.engine == "local":
        supported, reason = local_engine_supported(transcription)
        if not supported:
            raise LiveEngineUnavailable(reason)
        live_transcription = replace(transcription, backend="local", model=streaming.model)
        return LocalRollingEngine(live_transcription, streaming, callbacks)
    if streaming.engine == "openai-realtime":
        if remote_callbacks is None:
            raise LiveEngineUnavailable("OpenAI Realtime callbacks are not configured")
        from .openai_realtime import OpenAIRealtimeEngine

        from .openai_realtime import OpenAIRealtimeCallbacks
        return OpenAIRealtimeEngine(streaming.model, transcription.language, OpenAIRealtimeCallbacks(
            remote_callbacks.publish_partial, remote_callbacks.report_metrics,
        ))
    raise LiveEngineUnavailable("Unknown live transcription engine")
