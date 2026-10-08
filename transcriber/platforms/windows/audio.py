"""Windows microphone capture using PortAudio through the optional sounddevice package."""

from __future__ import annotations

import queue
import os
import tempfile
import threading
import wave
from pathlib import Path

from ...audio import AudioError, AudioSource
from ...config import AudioConfig


class WindowsRecorder:
    """Capture a WAV and expose bounded PCM frames to the shared live pipeline."""

    def __init__(self, config: AudioConfig, frame_ms: int = 20, max_queue_seconds: int = 30,
                 rolling_seconds: int = 30) -> None:
        if not 20 <= frame_ms <= 100:
            raise AudioError("frame_ms must be in 20..100")
        self.config = config
        self.frame_ms = frame_ms
        self.frame_bytes = config.sample_rate * frame_ms // 1000 * config.channels * 2
        frame_limit = max(8, max_queue_seconds * 1000 // frame_ms)
        self._input_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=frame_limit)
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=frame_limit)
        self._rolling_limit = max(1, rolling_seconds) * config.sample_rate * config.channels * 2
        self._rolling = bytearray()
        self._lock = threading.Lock()
        self._stream = None
        self._writer: threading.Thread | None = None
        self._wav: wave.Wave_write | None = None
        self._path: Path | None = None
        self._captured_frames = 0
        self._capture_error: str | None = None

    @property
    def live_available(self) -> bool:
        return self._stream is not None

    def start(self) -> Path:
        if self._stream is not None:
            raise AudioError("Recording is already active")
        try:
            import sounddevice as sd
        except ImportError as exc:
            raise AudioError("Windows audio capture needs the 'windows' extra (sounddevice)") from exc

        try:
            fd, filename = tempfile.mkstemp(prefix="whisper-", suffix=".wav")
            os.close(fd)
            self._path = Path(filename)
            self._wav = wave.open(str(self._path), "wb")
            self._wav.setnchannels(self.config.channels)
            self._wav.setsampwidth(2)
            self._wav.setframerate(self.config.sample_rate)
        except (OSError, wave.Error) as exc:
            self._discard()
            raise AudioError("Could not prepare captured audio") from exc

        self._clear_queues()
        self._rolling.clear()
        self._captured_frames = 0
        self._capture_error = None
        self._writer = threading.Thread(target=self._write_audio, name="whisper-audio-writer", daemon=True)
        self._writer.start()

        device = self.config.input_device
        if device in (None, "", "default"):
            device = None
        elif str(device).isdigit():
            device = int(device)
        try:
            stream = sd.InputStream(
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                dtype="int16",
                blocksize=self.config.sample_rate * self.frame_ms // 1000,
                device=device,
                callback=self._on_audio,
            )
            stream.start()
            self._stream = stream
        except Exception as exc:
            try:
                stream.close()
            except Exception:
                pass
            self._stop_writer()
            self._discard()
            raise AudioError(f"Could not open Windows microphone: {exc}") from exc
        assert self._path is not None
        return self._path

    def _on_audio(self, indata, frames: int, time_info, status) -> None:  # type: ignore[no-untyped-def]
        if status:
            self._capture_error = str(status)
        data = bytes(indata)
        try:
            self._input_queue.put_nowait(data)
        except queue.Full:
            self._capture_error = "Audio writer could not keep up with microphone input"

    def _write_audio(self) -> None:
        while True:
            data = self._input_queue.get()
            if data is None:
                break
            try:
                if self._wav is not None:
                    self._wav.writeframesraw(data)
            except (OSError, wave.Error) as exc:
                self._capture_error = str(exc)
                continue
            with self._lock:
                self._captured_frames += len(data) // (2 * self.config.channels)
                self._rolling.extend(data)
                if len(self._rolling) > self._rolling_limit:
                    del self._rolling[:-self._rolling_limit]
            try:
                self._frames.put_nowait(data)
            except queue.Full:
                try:
                    self._frames.get_nowait()
                    self._frames.put_nowait(data)
                except queue.Empty:
                    pass

    def read_frame(self, timeout: float | None = None) -> bytes | None:
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def window_bytes(self, max_seconds: int = 30) -> bytes:
        with self._lock:
            limit = min(self._rolling_limit, max_seconds * self.config.sample_rate * self.config.channels * 2)
            return bytes(self._rolling[-limit:])

    def captured_seconds(self) -> float:
        with self._lock:
            return self._captured_frames / self.config.sample_rate

    def stop(self) -> Path:
        if self._stream is None or self._path is None:
            raise AudioError("No recording is active")
        stream, self._stream = self._stream, None
        path = self._path
        stop_error: Exception | None = None
        try:
            stream.stop()
        except Exception as exc:
            stop_error = exc
            try:
                stream.abort()
            except Exception:
                pass
        try:
            stream.close()
        except Exception as exc:
            stop_error = stop_error or exc
        self._stop_writer()
        self._close_wav()
        if stop_error:
            self._discard()
            raise AudioError("Windows microphone did not stop cleanly") from stop_error
        if self._capture_error:
            self._discard()
            raise AudioError(f"Windows audio capture failed: {self._capture_error}")
        if not path.exists() or path.stat().st_size <= 44:
            self._discard()
            raise AudioError("Recorder produced no audio")
        return path

    def cancel(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
                stream.close()
            except Exception:
                pass
        self._stop_writer()
        self._discard()

    def _stop_writer(self) -> None:
        writer = self._writer
        self._writer = None
        if writer is None:
            return
        self._input_queue.put(None)
        writer.join(timeout=10)
        if writer.is_alive():
            self._capture_error = self._capture_error or "Audio writer did not stop"

    def _close_wav(self) -> None:
        wav, self._wav = self._wav, None
        if wav is not None:
            try:
                wav.close()
            except (OSError, wave.Error) as exc:
                self._capture_error = self._capture_error or str(exc)

    def _discard(self) -> None:
        self._close_wav()
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None

    def _clear_queues(self) -> None:
        for frames in (self._input_queue, self._frames):
            while True:
                try:
                    frames.get_nowait()
                except queue.Empty:
                    break


def list_sources() -> list[AudioSource]:
    try:
        import sounddevice as sd

        default = sd.default.device[0]
        sources = []
        for index, device in enumerate(sd.query_devices()):
            if int(device.get("max_input_channels", 0)) < 1:
                continue
            sources.append(AudioSource(
                id=str(index),
                name=str(device.get("name", f"Microphone {index}")),
                default=index == default,
            ))
        return sources
    except (ImportError, OSError, RuntimeError):
        return []
