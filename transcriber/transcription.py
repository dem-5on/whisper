"""Replaceable local and remote transcription backends."""

from __future__ import annotations

import json
import mimetypes
import os
from pathlib import Path
import subprocess
import threading
import urllib.error
import urllib.request
import uuid
import wave
from typing import Any, Protocol

from .config import TranscriptionConfig


class TranscriptionError(RuntimeError):
    pass


class TranscriptionBackend(Protocol):
    def transcribe(self, audio: Path) -> str: ...


# Process-wide WhisperModel cache keyed by (model, device, compute_type).
# ``FasterWhisperBackend.transcribe()`` used to construct a WhisperModel on
# every recording (multi-second startup latency); wrappers are still cheap
# to construct per call, but the heavy model is now loaded once per key for
# the daemon's lifetime and warmed with a silent sample at startup.
_MODEL_CACHE: dict[tuple[str, str, str], Any] = {}
_MODEL_CACHE_LOCK = threading.Lock()


def preload_local_model(model: str, device: str = "cpu", compute_type: str = "int8") -> bool:
    """Load (and warm) a local model now; returns False when unavailable."""
    try:
        backend = FasterWhisperBackend(model, None, device, compute_type)
        backend.ensure_loaded()
        backend.warmup()
    except Exception:
        return False
    return True


def clear_model_cache() -> None:
    with _MODEL_CACHE_LOCK:
        _MODEL_CACHE.clear()


class FasterWhisperBackend:
    def __init__(self, model: str, language: str | None = None, device: str = "cpu", compute_type: str = "int8") -> None:
        self.model = model
        self.language = language
        self.device = device
        self.compute_type = compute_type

    def _cache_key(self) -> tuple[str, str, str]:
        return (self.model, self.device, self.compute_type)

    def ensure_loaded(self) -> Any:
        try:
            from faster_whisper import WhisperModel
        except Exception as exc:
            raise TranscriptionError("faster-whisper is not installed; install universal-transcriber[local]") from exc
        key = self._cache_key()
        with _MODEL_CACHE_LOCK:
            cached = _MODEL_CACHE.get(key)
        if cached is not None:
            return cached
        try:
            loaded = WhisperModel(self.model, device=self.device, compute_type=self.compute_type)
        except Exception as exc:
            detail = str(exc).replace("\n", " ")[:500]
            raise TranscriptionError(f"Local transcription failed: {detail or type(exc).__name__}") from exc
        with _MODEL_CACHE_LOCK:
            _MODEL_CACHE.setdefault(key, loaded)
            return _MODEL_CACHE[key]

    def warmup(self) -> None:
        """Run a tiny silent decode so the first real utterance isn't slow."""
        model = self.ensure_loaded()
        try:
            import numpy as np
        except ImportError:
            return
        try:
            silence = np.zeros(8000, dtype=np.float32)  # 0.5 s of silence
            kwargs = {"language": self.language} if self.language else {}
            segments, _ = model.transcribe(silence, **kwargs)
            for _ in segments:
                break
        except Exception:
            pass  # warmup is best-effort; real errors surface on transcribe

    def transcribe_samples(self, samples: Any) -> str:
        try:
            model = self.ensure_loaded()
            kwargs = {"language": self.language} if self.language else {}
            segments, _ = model.transcribe(samples, **kwargs)
            return "".join(segment.text for segment in segments).strip()
        except TranscriptionError:
            raise
        except Exception as exc:
            detail = str(exc).replace("\n", " ")[:500]
            raise TranscriptionError(f"Local transcription failed: {detail or type(exc).__name__}") from exc

    def transcribe(self, audio: Path) -> str:
        try:
            from faster_whisper import WhisperModel  # noqa: F401  (keeps import error messaging local)
        except Exception as exc:
            raise TranscriptionError("faster-whisper is not installed; install universal-transcriber[local]") from exc
        try:
            # The capture contract is PCM WAV at a known rate, so decode it here
            # rather than through PyAV. This also keeps the local path robust
            # across incompatible PyAV/FFmpeg releases.
            samples = read_standard_wav(audio)
            # faster-whisper interprets NumPy audio at its native 16 kHz rate.
            # The capture contract and ``read_standard_wav`` enforce that rate.
            return self.transcribe_samples(samples)
        except TranscriptionError:
            raise
        except Exception as exc:
            # Local backend failures contain useful audio/model diagnostics and do
            # not carry API credentials. The daemon still redacts remote errors.
            detail = str(exc).replace("\n", " ")[:500]
            raise TranscriptionError(f"Local transcription failed: {detail or type(exc).__name__}") from exc


def read_standard_wav(audio: Path) -> Any:
    """Decode the application's required 16kHz, mono, 16-bit PCM WAV format."""
    try:
        with wave.open(str(audio), "rb") as source:
            if (source.getnchannels(), source.getframerate(), source.getsampwidth(), source.getcomptype()) != (1, 16_000, 2, "NONE"):
                raise TranscriptionError("Audio is not 16 kHz mono 16-bit PCM WAV")
            frames = source.readframes(source.getnframes())
    except (OSError, EOFError, wave.Error) as exc:
        raise TranscriptionError("Could not read captured WAV audio") from exc
    if not frames:
        raise TranscriptionError("Captured WAV contains no audio")
    try:
        import numpy as np
    except ImportError as exc:
        raise TranscriptionError("numpy is required for the faster-whisper local backend") from exc
    return np.frombuffer(frames, dtype="<i2").astype(np.float32) / 32768.0


class WhisperCppBackend:
    def __init__(self, model: str, language: str | None = None) -> None:
        self.model = model
        self.language = language

    def transcribe(self, audio: Path) -> str:
        # Model is a filesystem path for whisper.cpp, retaining the same config interface.
        try:
            completed = subprocess.run(
                ["whisper-cli", "-m", self.model, "-f", str(audio), "--no-timestamps"]
                + (["-l", self.language] if self.language else []),
                check=True, capture_output=True, text=True,
            )
        except (OSError, subprocess.CalledProcessError) as exc:
            raise TranscriptionError("whisper.cpp transcription failed") from exc
        return completed.stdout.strip()


class OpenAICompatibleBackend:
    """Multipart transcription client used for opt-in remote backends."""

    def __init__(self, endpoint: str, api_key_env: str, model: str) -> None:
        self.endpoint = endpoint
        self.api_key_env = api_key_env
        self.model = model

    def transcribe(self, audio: Path) -> str:
        key = os.environ.get(self.api_key_env)
        if not key:
            raise TranscriptionError(f"{self.api_key_env} is required for this transcription backend")
        boundary = f"----transcriber-{uuid.uuid4().hex}"
        body = _multipart(boundary, audio, self.model)
        request = urllib.request.Request(
            self.endpoint, data=body,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
                "Accept": "application/json",
            }, method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:
                payload = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            # Status code only; never includes credentials or audio.
            raise TranscriptionError(f"Remote transcription request failed (HTTP {exc.code})") from exc
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise TranscriptionError("Remote transcription request failed") from exc
        text = payload.get("text")
        if not isinstance(text, str):
            raise TranscriptionError("Remote transcription response did not contain text")
        return text.strip()


def _multipart(boundary: str, audio: Path, model: str) -> bytes:
    content_type = mimetypes.guess_type(audio.name)[0] or "audio/wav"
    chunks = [
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"model\"\r\n\r\n{model}\r\n".encode(),
        (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{audio.name}\"\r\n"
         f"Content-Type: {content_type}\r\n\r\n").encode(),
        audio.read_bytes(),
        f"\r\n--{boundary}--\r\n".encode(),
    ]
    return b"".join(chunks)


def make_backend(config: TranscriptionConfig) -> TranscriptionBackend:
    if config.backend == "local":
        return (FasterWhisperBackend(config.model, config.language, config.device, config.compute_type)
                if config.local_engine == "faster-whisper" else WhisperCppBackend(config.model, config.language))
    if config.backend == "groq":
        return OpenAICompatibleBackend("https://api.groq.com/openai/v1/audio/transcriptions", "GROQ_API_KEY", config.model)
    if config.backend == "openai":
        return OpenAICompatibleBackend("https://api.openai.com/v1/audio/transcriptions", "OPENAI_API_KEY", config.model)
    return OpenAICompatibleBackend("https://openrouter.ai/api/v1/audio/transcriptions", "OPENROUTER_API_KEY", config.model)
