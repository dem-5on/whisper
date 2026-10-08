"""Linux audio capture behind a deliberately small interface."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import queue
import re
import shutil
import signal
import subprocess
import tempfile
import threading
import wave

from .config import AudioConfig


class AudioError(RuntimeError):
    pass


@dataclass(frozen=True)
class AudioSource:
    id: str
    name: str
    default: bool = False


class SoxRecorder:
    """Records normalized 16kHz/mono WAV using SoX's ``rec`` program."""

    def __init__(self, config: AudioConfig) -> None:
        self.config = config
        self._process: subprocess.Popen[bytes] | None = None
        self._path: Path | None = None

    def start(self) -> Path:
        if self._process is not None:
            raise AudioError("Recording is already active")
        fd, filename = tempfile.mkstemp(prefix="transcriber-", suffix=".wav")
        # SoX creates the file itself; close the reservation without retaining audio.
        os.close(fd)
        self._path = Path(filename)
        self._path.unlink()
        command = [
            "rec", "-q", "-r", str(self.config.sample_rate), "-c", str(self.config.channels),
            "-b", "16", str(self._path),
        ]
        env: dict[str, str] | None = None
        device = (self.config.input_device or "").strip() if self.config.input_device else ""
        if device and device != "default":
            # SoX uses the PulseAudio driver here (PipeWire-Pulse compatible);
            # PULSE_SOURCE selects the capture source without changing args.
            env = dict(os.environ)
            env["PULSE_SOURCE"] = device
        try:
            self._process = subprocess.Popen(
                command, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, env=env,
            )
        except OSError as exc:
            self._discard_file()
            raise AudioError("Unable to start SoX recorder; is 'rec' installed?") from exc
        return self._path

    def stop(self) -> Path:
        if self._process is None or self._path is None:
            raise AudioError("No recording is active")
        process, path = self._process, self._path
        self._process = None
        try:
            # SIGINT is SoX's normal graceful-stop signal and lets it finalize
            # the WAV header; SIGTERM is reserved for cancellation/shutdown.
            process.send_signal(signal.SIGINT)
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise AudioError("Audio recorder did not stop") from exc
        if not path.exists() or path.stat().st_size <= 44:
            self._discard_file()
            raise AudioError("Recorder produced no audio")
        return path

    def cancel(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        self._discard_file()

    def _discard_file(self) -> None:
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None


class StreamingRecorder:
    """SoX capture as a PCM frame stream plus a finalized WAV on stop.

    Same ``rec`` dependency as :class:`SoxRecorder`, but captures raw PCM to
    stdout (``rec ... -t raw -``) so the daemon receives 20-100 ms 16 kHz
    mono int16 frames *while* recording. Frames flow into a bounded queue
    (oldest dropped when full, so VAD cannot grow memory unboundedly). A
    separate bounded rolling buffer feeds live decoding; the complete audio
    is written directly to a temporary WAV for final transcription.

    Presence of :meth:`read_frame` marks a recorder as streaming-capable;
    the daemon feature-detects it and falls back to the file-only path for
    legacy/fake recorders.
    """

    def __init__(self, config: AudioConfig, frame_ms: int = 20, max_queue_seconds: int = 30,
                 rolling_seconds: int = 30) -> None:
        if frame_ms < 20 or frame_ms > 100:
            raise AudioError("frame_ms must be in 20..100")
        self.config = config
        self.frame_ms = frame_ms
        self.frame_bytes = config.sample_rate * frame_ms // 1000 * 2
        max_frames = max(8, max_queue_seconds * 1000 // frame_ms)
        self._frames: queue.Queue[bytes] = queue.Queue(maxsize=max_frames)
        self._process: subprocess.Popen[bytes] | None = None
        self._reader: threading.Thread | None = None
        self._path: Path | None = None
        self._wav: wave.Wave_write | None = None
        self._window = bytearray()
        self._window_capacity = max(1, rolling_seconds) * config.sample_rate * 2
        self._captured_frames = 0
        self._capture_lock = threading.Lock()
        self._stopping = threading.Event()
        self._start_error: Exception | None = None

    @property
    def live_available(self) -> bool:
        return self._process is not None

    def start(self) -> Path:
        if self._process is not None:
            raise AudioError("Recording is already active")
        fd, filename = tempfile.mkstemp(prefix="transcriber-", suffix=".wav")
        os.close(fd)
        self._path = Path(filename)
        self._path.unlink()
        command = [
            "rec", "-q", "-r", str(self.config.sample_rate), "-c", str(self.config.channels),
            "-b", "16", "-e", "signed-integer", "-L", "-t", "raw", "-",
        ]
        env: dict[str, str] | None = None
        device = (self.config.input_device or "").strip() if self.config.input_device else ""
        if device and device != "default":
            env = dict(os.environ)
            env["PULSE_SOURCE"] = device
        self._drain_queue()
        self._window.clear()
        self._captured_frames = 0
        self._stopping.clear()
        self._start_error = None
        try:
            self._wav = wave.open(str(self._path), "wb")
            self._wav.setnchannels(self.config.channels)
            self._wav.setsampwidth(2)
            self._wav.setframerate(self.config.sample_rate)
        except (OSError, wave.Error) as exc:
            self._discard_file()
            raise AudioError("Could not prepare captured audio") from exc
        try:
            self._process = subprocess.Popen(
                command, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env,
            )
        except OSError as exc:
            self._discard_file()
            raise AudioError("Unable to start SoX recorder; is 'rec' installed?") from exc
        assert self._path is not None
        reserved = self._path
        self._reader = threading.Thread(target=self._pump, name="transcriber-capture", daemon=True)
        self._reader.start()
        return reserved

    def read_frame(self, timeout: float | None = None) -> bytes | None:
        """Next 16-bit PCM frame, or ``None`` on timeout/stop."""
        try:
            return self._frames.get(timeout=timeout)
        except queue.Empty:
            return None

    def window_bytes(self, max_seconds: int = 30) -> bytes:
        with self._capture_lock:
            limit = min(self._window_capacity, self.config.sample_rate * max_seconds * 2)
            data = bytes(self._window[-limit:])
        return data

    def captured_seconds(self) -> float:
        with self._capture_lock:
            return self._captured_frames / self.config.sample_rate

    def stop(self) -> Path:
        if self._process is None or self._path is None:
            raise AudioError("No recording is active")
        process, path = self._process, self._path
        self._process = None
        try:
            # SIGINT is SoX's normal graceful stop. Leave the pump running
            # until EOF so the final buffered PCM frames reach the WAV.
            process.send_signal(signal.SIGINT)
            process.wait(timeout=10)
        except subprocess.TimeoutExpired as exc:
            process.kill()
            raise AudioError("Audio recorder did not stop") from exc
        finally:
            # The process closing stdout is the pump's EOF signal. Do not set
            # the stop event first or a final partial read can be discarded.
            self._join_reader()
            self._stopping.set()
            self._finish_wav()
        if self._start_error is not None:
            self._discard_file()
            raise AudioError("Audio capture failed before producing frames")
        if self._captured_frames == 0:
            self._discard_file()
            raise AudioError("Recorder produced no audio")
        self._path = None
        return path

    def cancel(self) -> None:
        if self._process is not None:
            self._process.terminate()
            try:
                self._process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._process.kill()
            self._process = None
        self._stopping.set()
        self._join_reader()
        self._finish_wav()
        self._drain_queue()
        self._discard_file()

    # -- internals ------------------------------------------------------
    def _pump(self) -> None:
        process = self._process
        if process is None or process.stdout is None:
            self._start_error = AudioError("Audio capture produced no stream")
            return
        try:
            while not self._stopping.is_set():
                chunk = process.stdout.read(self.frame_bytes)
                if not chunk:
                    break
                with self._capture_lock:
                    if self._wav is None:
                        raise AudioError("Captured-audio file is not open")
                    self._wav.writeframesraw(chunk)
                    self._captured_frames += len(chunk) // 2
                    self._window += chunk
                    if len(self._window) > self._window_capacity:
                        del self._window[: len(self._window) - self._window_capacity]
                try:
                    self._frames.put_nowait(bytes(chunk))
                except queue.Full:
                    try:
                        self._frames.get_nowait()
                    except queue.Empty:
                        pass
                    try:
                        self._frames.put_nowait(bytes(chunk))
                    except queue.Full:
                        pass
        except Exception as exc:  # keep capture failures visible at stop()
            self._start_error = exc

    def _join_reader(self) -> None:
        reader, self._reader = self._reader, None
        if reader is not None and reader.is_alive():
            reader.join(timeout=5)

    def _drain_queue(self) -> None:
        try:
            while True:
                self._frames.get_nowait()
        except queue.Empty:
            pass

    def _finish_wav(self) -> None:
        wav, self._wav = self._wav, None
        if wav is not None:
            try:
                wav.close()
            except (OSError, wave.Error) as exc:
                self._start_error = self._start_error or exc

    def _discard_file(self) -> None:
        self._finish_wav()
        if self._path is not None:
            self._path.unlink(missing_ok=True)
            self._path = None


def make_recorder(config: AudioConfig, streaming_enabled: bool = True, frame_ms: int = 20,
                   max_queue_seconds: int = 30, rolling_seconds: int = 30):  # type: ignore[no-untyped-def]
    """Select the native capture implementation for the current platform."""
    if os.name == "nt":
        from .platforms.windows.audio import WindowsRecorder

        return WindowsRecorder(
            config, frame_ms=frame_ms, max_queue_seconds=max_queue_seconds,
            rolling_seconds=rolling_seconds,
        )
    if streaming_enabled:
        return StreamingRecorder(
            config, frame_ms=frame_ms, max_queue_seconds=max_queue_seconds,
            rolling_seconds=rolling_seconds,
        )
    return SoxRecorder(config)


def list_sources() -> list[AudioSource]:
    """Enumerate microphones using the current platform's audio API."""
    if os.name == "nt":
        from .platforms.windows.audio import list_sources as windows_sources

        return windows_sources()
    for probe in (_wpctl_sources, _pactl_sources, _arecord_sources):
        try:
            sources = probe()
        except (OSError, subprocess.TimeoutExpired):
            continue
        if sources:
            return sources
    return []


def _wpctl_sources() -> list[AudioSource]:
    if not shutil.which("wpctl"):
        return []
    completed = subprocess.run(["wpctl", "status"], check=True, capture_output=True, text=True, timeout=5)
    return parse_wpctl_status(completed.stdout)


def parse_wpctl_status(output: str) -> list[AudioSource]:
    sources: list[AudioSource] = []
    in_audio = False
    in_sources = False
    for raw in output.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        # Top-level sections start at column 0 in `wpctl status`.
        if stripped in {"Audio", "Video", "Settings"}:
            in_audio = stripped == "Audio"
            in_sources = False
            continue
        if not in_audio:
            continue
        # wpctl uses tree glyphs: "├─ Sources:", "│  *   57. Name  [vol: ...]"
        if stripped.endswith("Sources:"):
            in_sources = True
            continue
        if in_sources:
            if stripped.endswith(("Sinks:", "Filters:", "Streams:", "Devices:")):
                in_sources = False
                continue
            match = re.match(r"\s*[│├└─\s]*(\*?)\s*(\d+)\.\s+(.+?)(?:\s+\[.*\])?\s*$", line)
            if match:
                default = match.group(1) == "*"
                node_id = match.group(2)
                name = match.group(3).strip(" │├└─")
                if name:
                    sources.append(AudioSource(id=node_id, name=name, default=default))
    return sources


def _pactl_sources() -> list[AudioSource]:
    if not shutil.which("pactl"):
        return []
    completed = subprocess.run(
        ["pactl", "list", "short", "sources"], check=True, capture_output=True, text=True, timeout=5,
    )
    sources: list[AudioSource] = []
    default = _pactl_default_source()
    for line in completed.stdout.splitlines():
        parts = line.split()
        if len(parts) < 2:
            continue
        name = parts[1]
        if name.endswith(".monitor"):
            continue
        sources.append(AudioSource(id=name, name=name, default=(name == default)))
    return sources


def _pactl_default_source() -> str:
    try:
        completed = subprocess.run(
            ["pactl", "get-default-source"], check=True, capture_output=True, text=True, timeout=5,
        )
        return completed.stdout.strip()
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return ""


def _arecord_sources() -> list[AudioSource]:
    if not shutil.which("arecord"):
        return []
    completed = subprocess.run(["arecord", "-l"], check=True, capture_output=True, text=True, timeout=5)
    sources: list[AudioSource] = []
    for line in completed.stdout.splitlines():
        match = re.match(r"card\s+(\d+):\s+([^[]+)\[.*\],\s*device\s+(\d+):\s*(.+)", line.strip())
        if match:
            card, card_name, device, dev_name = match.groups()
            hw = f"hw:{card},{device}"
            sources.append(AudioSource(id=hw, name=f"{card_name.strip()} — {dev_name.strip()} ({hw})"))
    return sources
