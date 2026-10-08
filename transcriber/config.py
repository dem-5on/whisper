"""Configuration loading and validation; credentials deliberately do not live here."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
import os
from typing import Any
from urllib.parse import urlsplit

import yaml


class ConfigurationError(ValueError):
    """A user-facing configuration error that is safe to log."""


#: Provider name -> environment variable holding its API key.
KEY_ENV = {
    "groq": "GROQ_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "openai": "OPENAI_API_KEY",
    "hosted-whisper": "WHISPER_SERVICE_TOKEN",
}


@dataclass(frozen=True)
class AudioConfig:
    sample_rate: int = 16_000
    channels: int = 1
    format: str = "wav"
    max_duration_seconds: int = 120
    input_device: str | None = None


@dataclass(frozen=True)
class TranscriptionConfig:
    backend: str = "local"
    model: str = "base"
    local_engine: str = "faster-whisper"
    language: str | None = None
    device: str = "cpu"
    compute_type: str = "int8"


@dataclass(frozen=True)
class ProcessingConfig:
    cleanup: bool = False
    replacements: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class DeliveryConfig:
    backend: str = "keyboard"
    submit_after_insert: bool = False


@dataclass(frozen=True)
class UiConfig:
    show_last_transcript: bool = False
    preview_chars: int = 120


@dataclass(frozen=True)
class StreamingConfig:
    # Live-transcription pipeline tuning. All live text stays in the panel
    # until finalization; delivery still inserts only the final transcript
    # unless live_insert_experimental is explicitly enabled.
    enabled: bool = True
    # Live engine/model are independent from the final batch transcription
    # provider and model (e.g. OpenAI live captions + OpenRouter final pass).
    engine: str = "local"
    model: str = "base"
    server_url: str = ""
    # PCM frame size emitted by the capture stream (20-100 ms). Twenty ms is
    # also a native WebRTC-VAD frame duration.
    frame_ms: int = 20
    # How often the rolling window is re-decoded for partial text.
    chunk_ms: int = 500
    # Most recent seconds re-decoded on each chunk (with overlap). A smaller
    # default avoids repeatedly decoding 15 seconds every half second.
    window_seconds: int = 8
    # Trailing silence after speech before VAD auto-stops the recording.
    silence_timeout_seconds: float = 1.8
    # 0.0 (only loud speech) .. 1.0 (very sensitive). Maps to an RMS threshold.
    vad_sensitivity: float = 0.5
    # Upper bound for buffered capture so slow inference can't grow memory.
    max_queue_seconds: int = 30
    show_partials: bool = True
    preload_model: bool = True
    live_insert_experimental: bool = False


@dataclass(frozen=True)
class Config:
    audio: AudioConfig = field(default_factory=AudioConfig)
    transcription: TranscriptionConfig = field(default_factory=TranscriptionConfig)
    processing: ProcessingConfig = field(default_factory=ProcessingConfig)
    delivery: DeliveryConfig = field(default_factory=DeliveryConfig)
    ui: UiConfig = field(default_factory=UiConfig)
    streaming: StreamingConfig = field(default_factory=StreamingConfig)


def default_config_path() -> Path:
    if os.name == "nt":
        from .platforms.windows.paths import config_dir

        return config_dir() / "config.yaml"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "transcriber" / "config.yaml"


def resolve_config_path(path: str | Path | None = None) -> Path:
    return Path(path or os.environ.get("TRANSCRIBER_CONFIG", default_config_path()))


def default_keys_path() -> Path:
    if os.name == "nt":
        from .platforms.windows.paths import config_dir

        return config_dir() / "keys.env"
    return Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "transcriber" / "keys.env"


def load_keys_env(path: str | Path | None = None) -> dict[str, str]:
    """Load API keys from a 0600 env file without overriding real environment.

    Returns the keys that were applied. The file holds KEY=VALUE lines;
    blank lines and # comments are ignored. Anything already present in
    ``os.environ`` wins, so explicit exports always take precedence.
    """
    location = Path(path or os.environ.get("TRANSCRIBER_KEYS_FILE", default_keys_path()))
    applied: dict[str, str] = {}
    try:
        text = location.read_text()
    except OSError:
        return applied
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip("\"'")
        if not key or not value or not key.replace("_", "").isalnum() or key != key.upper():
            continue
        if key not in os.environ:
            os.environ[key] = value
            applied[key] = value
    return applied


def write_key(keys_path: str | Path | None, name: str, value: str | None) -> None:
    """Store or remove a single KEY in the keys file (created 0600)."""
    location = Path(keys_path or os.environ.get("TRANSCRIBER_KEYS_FILE", default_keys_path()))
    location.parent.mkdir(parents=True, exist_ok=True)
    entries: dict[str, str] = {}
    order: list[str] = []
    try:
        text = location.read_text()
    except OSError:
        text = ""
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, val = stripped.partition("=")
        key = key.strip()
        if key and key not in entries:
            order.append(key)
        entries[key] = val.strip()
    if value is None:
        entries.pop(name, None)
        order = [key for key in order if key != name]
    else:
        if name not in entries:
            order.append(name)
        entries[name] = value
    location.write_text("".join(f"{key}={entries[key]}\n" for key in order))
    if os.name != "nt":
        try:
            os.chmod(location, 0o600)
        except OSError:
            pass


def load_config(path: str | Path | None = None) -> Config:
    location = resolve_config_path(path)
    if not location.exists():
        return Config()
    try:
        raw = yaml.safe_load(location.read_text()) or {}
    except (OSError, yaml.YAMLError) as exc:
        raise ConfigurationError(f"Cannot read configuration: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigurationError("Configuration root must be a mapping")
    return _parse(raw)


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name, {})
    if not isinstance(value, dict):
        raise ConfigurationError(f"{name} must be a mapping")
    return value


def save_config(config: Config, path: str | Path | None = None) -> Path:
    """Persist the whole config; every daemon-side change lands here."""
    location = resolve_config_path(path)
    location.parent.mkdir(parents=True, exist_ok=True)
    location.write_text(yaml.safe_dump(asdict(config), default_flow_style=False, sort_keys=False))
    return location


def _parse(raw: dict[str, Any]) -> Config:
    audio = _section(raw, "audio")
    transcription = _section(raw, "transcription")
    processing = _section(raw, "processing")
    delivery = _section(raw, "delivery")
    ui = _section(raw, "ui")
    streaming = _section(raw, "streaming")
    try:
        result = Config(
            audio=AudioConfig(**audio),
            transcription=TranscriptionConfig(**transcription),
            processing=ProcessingConfig(**processing),
            delivery=DeliveryConfig(**delivery),
            ui=UiConfig(**ui),
            streaming=StreamingConfig(**streaming),
        )
    except TypeError as exc:
        raise ConfigurationError(f"Unknown or invalid setting: {exc}") from exc
    if (not isinstance(result.audio.sample_rate, int) or isinstance(result.audio.sample_rate, bool)
            or not isinstance(result.audio.channels, int) or isinstance(result.audio.channels, bool)
            or not isinstance(result.audio.format, str)):
        raise ConfigurationError("audio sample_rate, channels, and format have invalid types")
    if result.audio.sample_rate != 16_000 or result.audio.channels != 1 or result.audio.format != "wav":
        raise ConfigurationError("audio must use 16 kHz, mono, WAV")
    if (not isinstance(result.audio.max_duration_seconds, int) or isinstance(result.audio.max_duration_seconds, bool)
            or result.audio.max_duration_seconds <= 0):
        raise ConfigurationError("audio.max_duration_seconds must be positive")
    if result.audio.input_device is not None and (not isinstance(result.audio.input_device, str) or not result.audio.input_device):
        raise ConfigurationError("audio.input_device must be a device name or null")
    if result.transcription.backend not in {"local", "groq", "openrouter", "openai"}:
        raise ConfigurationError("transcription.backend must be local, groq, openrouter, or openai")
    if result.transcription.local_engine not in {"faster-whisper", "whisper.cpp"}:
        raise ConfigurationError("transcription.local_engine must be faster-whisper or whisper.cpp")
    if result.delivery.backend not in {"keyboard", "clipboard"}:
        raise ConfigurationError("delivery.backend must be keyboard or clipboard")
    if not isinstance(result.transcription.model, str) or not result.transcription.model:
        raise ConfigurationError("transcription.model must be a non-empty string")
    if result.transcription.language is not None and (not isinstance(result.transcription.language, str) or not result.transcription.language):
        raise ConfigurationError("transcription.language must be a language code or null")
    if (not isinstance(result.transcription.device, str) or not result.transcription.device
            or not isinstance(result.transcription.compute_type, str) or not result.transcription.compute_type):
        raise ConfigurationError("transcription.device and compute_type must be non-empty strings")
    if not isinstance(result.processing.cleanup, bool) or not isinstance(result.delivery.submit_after_insert, bool):
        raise ConfigurationError("cleanup and submit_after_insert must be booleans")
    if not isinstance(result.processing.replacements, dict) or not all(isinstance(key, str) and isinstance(value, str) for key, value in result.processing.replacements.items()):
        raise ConfigurationError("processing.replacements must map strings to strings")
    if not isinstance(result.ui.show_last_transcript, bool):
        raise ConfigurationError("ui.show_last_transcript must be a boolean")
    if (not isinstance(result.ui.preview_chars, int) or isinstance(result.ui.preview_chars, bool)
            or result.ui.preview_chars <= 0 or result.ui.preview_chars > 500):
        raise ConfigurationError("ui.preview_chars must be a positive integer (max 500)")
    stream = result.streaming
    if not isinstance(stream.enabled, bool) or not isinstance(stream.show_partials, bool) \
            or not isinstance(stream.preload_model, bool) or not isinstance(stream.live_insert_experimental, bool):
        raise ConfigurationError("streaming enabled/show_partials/preload_model/live_insert_experimental must be booleans")
    if stream.engine not in {"local", "openai-realtime", "hosted-whisper"}:
        raise ConfigurationError("streaming.engine must be local, openai-realtime, or hosted-whisper")
    if not isinstance(stream.model, str) or not stream.model or len(stream.model) > 120:
        raise ConfigurationError("streaming.model must be a non-empty model id (max 120 characters)")
    if not isinstance(stream.server_url, str):
        raise ConfigurationError("streaming.server_url must be a WebSocket URL")
    if stream.server_url:
        try:
            parsed_url = urlsplit(stream.server_url)
            valid_scheme = parsed_url.scheme == "wss" or (
                parsed_url.scheme == "ws" and parsed_url.hostname in {"localhost", "127.0.0.1", "::1"}
            )
            _ = parsed_url.port  # Force malformed ports to fail validation.
            valid_url = valid_scheme and bool(parsed_url.hostname) and parsed_url.path == "/v1/live" \
                and not parsed_url.username and not parsed_url.password and not parsed_url.query and not parsed_url.fragment
        except ValueError:
            valid_url = False
        if not valid_url:
            raise ConfigurationError("streaming.server_url must be wss://host/v1/live (ws is allowed only on localhost)")
    if stream.engine == "openai-realtime" and stream.model not in {"gpt-live-transcribe", "gpt-transcribe"}:
        raise ConfigurationError("OpenAI Realtime model must be gpt-live-transcribe or gpt-transcribe")
    if stream.engine == "local" and stream.model not in {"tiny", "base", "small", "medium", "large-v3", "turbo"}:
        raise ConfigurationError("Local live model must be tiny, base, small, medium, large-v3, or turbo")
    if stream.engine == "hosted-whisper" and (not stream.model.strip() or any(ch.isspace() for ch in stream.model)):
        raise ConfigurationError("Hosted Whisper profile must be a non-empty profile ID without spaces")
    if (not isinstance(stream.frame_ms, int) or isinstance(stream.frame_ms, bool)
            or stream.frame_ms < 20 or stream.frame_ms > 100):
        raise ConfigurationError("streaming.frame_ms must be an integer in 20..100")
    if (not isinstance(stream.chunk_ms, int) or isinstance(stream.chunk_ms, bool)
            or stream.chunk_ms < 200 or stream.chunk_ms > 2000):
        raise ConfigurationError("streaming.chunk_ms must be an integer in 200..2000")
    if (not isinstance(stream.window_seconds, int) or isinstance(stream.window_seconds, bool)
            or stream.window_seconds < 4 or stream.window_seconds > 30):
        raise ConfigurationError("streaming.window_seconds must be an integer in 4..30")
    if (not isinstance(stream.silence_timeout_seconds, (int, float)) or isinstance(stream.silence_timeout_seconds, bool)
            or not 0.5 <= float(stream.silence_timeout_seconds) <= 5.0):
        raise ConfigurationError("streaming.silence_timeout_seconds must be a number in 0.5..5.0")
    if (not isinstance(stream.vad_sensitivity, (int, float)) or isinstance(stream.vad_sensitivity, bool)
            or not 0.0 <= float(stream.vad_sensitivity) <= 1.0):
        raise ConfigurationError("streaming.vad_sensitivity must be a number in 0.0..1.0")
    if (not isinstance(stream.max_queue_seconds, int) or isinstance(stream.max_queue_seconds, bool)
            or stream.max_queue_seconds < 5 or stream.max_queue_seconds > 120):
        raise ConfigurationError("streaming.max_queue_seconds must be an integer in 5..120")
    return result
