"""faster-whisper inference worker for hosted CPU/CUDA deployments."""

from __future__ import annotations

import asyncio
from dataclasses import replace
import logging
import os
import threading
from typing import Any, Callable

from .hardware import HardwareSelection, detect_hardware
from .sessions import ModelProfile, PCM_BYTES_PER_SECOND, WorkerSession
from .stream_text import RollingTranscript


logger = logging.getLogger("whisper_service.worker")


class WorkerStartupError(RuntimeError):
    """The selected inference runtime or model couldn't be initialized."""


class FasterWhisperWorker:
    """Shared model runtime; each client gets isolated, bounded session memory."""

    def __init__(self, hardware: HardwareSelection, *, partial_interval_seconds: float = 1.0,
                 rolling_window_seconds: int = 8, cpu_threads: int | None = None,
                 model_factory: Callable[..., Any] | None = None) -> None:
        if partial_interval_seconds < 0.2:
            raise ValueError("partial interval must be at least 0.2 seconds")
        if rolling_window_seconds < 1:
            raise ValueError("rolling window must be at least one second")
        self.hardware = hardware
        self.partial_interval_seconds = partial_interval_seconds
        self.rolling_window_seconds = rolling_window_seconds
        self.cpu_threads = cpu_threads or min(4, max(1, os.cpu_count() or 1))
        self._model_factory = model_factory
        self._models: dict[str, Any] = {}
        self._model_lock = threading.Lock()
        # A conservative default keeps a modest VPS from oversubscribing cores
        # or GPU memory when multiple user sessions decode at once.
        self._decode_slots = asyncio.Semaphore(1)

    async def prepare(self, profile: ModelProfile) -> None:
        """Load and warm the model before opening a public listener."""
        try:
            model = await asyncio.to_thread(self._get_model, profile.model)
            await self._decode(model, b"\x00\x00" * 8000, None, beam_size=1)
        except Exception as exc:
            raise WorkerStartupError("The configured speech model could not be initialized") from exc

    async def open_session(self, profile: ModelProfile, language: str | None) -> "FasterWhisperSession":
        if self.hardware.device not in profile.devices:
            raise WorkerStartupError("The requested model profile is not compatible with the selected device")
        model = await asyncio.to_thread(self._get_model, profile.model)
        return FasterWhisperSession(self, model, language)

    def _get_model(self, model_name: str) -> Any:
        with self._model_lock:
            model = self._models.get(model_name)
            if model is not None:
                return model
            factory = self._model_factory
            if factory is None:
                try:
                    from faster_whisper import WhisperModel
                except ImportError as exc:
                    raise WorkerStartupError("Install the hosted service extra to load faster-whisper") from exc
                factory = WhisperModel
            try:
                model = factory(
                    model_name,
                    device=self.hardware.device,
                    compute_type=self.hardware.compute_type,
                    cpu_threads=self.cpu_threads,
                )
            except Exception as exc:
                raise WorkerStartupError("faster-whisper couldn't load the configured model") from exc
            self._models[model_name] = model
            return model

    async def _decode(self, model: Any, pcm_s16le: bytes, language: str | None, *, beam_size: int) -> str:
        async with self._decode_slots:
            return await asyncio.to_thread(_decode_pcm, model, pcm_s16le, language, beam_size)


class FasterWhisperSession(WorkerSession):
    def __init__(self, worker: FasterWhisperWorker, model: Any, language: str | None) -> None:
        self._worker = worker
        self._model = model
        self._language = language
        self._all_audio = bytearray()
        self._rolling_audio = bytearray()
        self._rolling_capacity = worker.rolling_window_seconds * PCM_BYTES_PER_SECOND
        self._partial_threshold = int(worker.partial_interval_seconds * PCM_BYTES_PER_SECOND)
        self._bytes_since_partial = 0
        self._stitcher = RollingTranscript()
        self._closed = False

    async def submit_audio(self, pcm_s16le: bytes) -> str | None:
        if self._closed:
            return None
        self._all_audio.extend(pcm_s16le)
        self._rolling_audio.extend(pcm_s16le)
        overflow = len(self._rolling_audio) - self._rolling_capacity
        if overflow > 0:
            del self._rolling_audio[:overflow]
        self._bytes_since_partial += len(pcm_s16le)
        if self._bytes_since_partial < self._partial_threshold:
            return None
        self._bytes_since_partial %= self._partial_threshold
        current_window = bytes(self._rolling_audio)
        hypothesis = await self._worker._decode(self._model, current_window, self._language, beam_size=1)
        return self._stitcher.update(hypothesis) if hypothesis else None

    async def finish(self) -> str:
        if self._closed:
            raise RuntimeError("inference session is closed")
        self._closed = True
        try:
            if not self._all_audio:
                return ""
            return await self._worker._decode(self._model, bytes(self._all_audio), self._language, beam_size=5)
        finally:
            self._clear()

    async def cancel(self) -> None:
        self._closed = True
        self._clear()

    def _clear(self) -> None:
        # Overwrite mutable buffers before releasing their storage. Immutable
        # temporary copies passed into inference are released when decode ends.
        self._all_audio[:] = b"\x00" * len(self._all_audio)
        self._rolling_audio[:] = b"\x00" * len(self._rolling_audio)
        self._all_audio.clear()
        self._rolling_audio.clear()
        self._stitcher.clear()


async def prepare_worker(mode: str, profile: ModelProfile, *, partial_interval_seconds: float = 1.0,
                         rolling_window_seconds: int = 8) -> tuple[HardwareSelection, FasterWhisperWorker]:
    """Initialize the selected worker; auto mode falls back if CUDA model load fails."""
    selection = detect_hardware(mode)
    worker = FasterWhisperWorker(selection, partial_interval_seconds=partial_interval_seconds,
                                 rolling_window_seconds=rolling_window_seconds)
    try:
        await worker.prepare(profile)
        return selection, worker
    except WorkerStartupError:
        if mode != "auto" or selection.device != "cuda" or "cpu" not in profile.devices:
            raise
        logger.warning("CUDA worker initialization failed; retrying with CPU")
        cpu = detect_hardware("cpu")
        cpu_worker = FasterWhisperWorker(cpu, partial_interval_seconds=partial_interval_seconds,
                                         rolling_window_seconds=rolling_window_seconds)
        await cpu_worker.prepare(profile)
        fallback = replace(selection, device="cpu", compute_type=cpu.compute_type,
                           reason="CUDA model initialization failed; using CPU")
        return fallback, cpu_worker


def _decode_pcm(model: Any, pcm_s16le: bytes, language: str | None, beam_size: int) -> str:
    try:
        import numpy as np
    except ImportError as exc:
        raise WorkerStartupError("NumPy is required by the hosted transcription worker") from exc
    samples = np.frombuffer(pcm_s16le, dtype="<i2").astype(np.float32) / 32768.0
    options: dict[str, object] = {
        "beam_size": beam_size,
        "condition_on_previous_text": False,
        "vad_filter": False,
    }
    if language:
        options["language"] = language
    segments, _info = model.transcribe(samples, **options)
    return "".join(segment.text for segment in segments).strip()
