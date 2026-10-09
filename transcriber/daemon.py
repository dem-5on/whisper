"""Daemon state machine and local IPC services.

Protocol (JSON per line, ``{"command": ...}``):
  toggle, cancel, status (legacy) plus retry/retranscribe, last, mics,
  models, set_backend/set_provider {"backend": ...}, set_model {"model": ...},
  set_mic {"device": ...}, events {"since": revision, "session_id": ...},
  subscribe {"since": revision} (persistent stream).
Backend/model/mic switches are persisted to config.yaml; API keys live in
keys.env (loaded at startup, never logged). Status responses always include
state, timing, backend, model, mic, key presence, last-run metadata, and the
current live session (session_id, live_revision, committed/provisional text,
speech_active) so desktop clients can render partial text from daemon state;
``subscribe`` streams ``partial``/``committed``/``final``/
``speech_started``/``speech_ended`` events with session ID and revision for
push-style clients. Only finalized transcripts are delivered to the focused
app; partials stay in the panel/overlay.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import shutil
import signal
import socket
import socketserver
import stat
import threading
import time
import urllib.error
import urllib.request
from typing import Any
import re

from . import __version__
from .audio import list_sources, make_recorder
from .config import KEY_ENV, Config, load_config, load_keys_env, resolve_config_path, save_config, write_key
from .delivery import make_delivery
from .live_engines import LiveEngine, LiveEngineUnavailable, LocalEngineCallbacks, RemoteLiveCallbacks, make_live_engine
from .processing import process_transcript
from .state import State
from .streaming import LiveSession
from .transcription import TranscriptionError, make_backend, preload_local_model
from .vad import SpeechEndpoint, create_vad

LOG = logging.getLogger("transcriber")

_VALID_BACKENDS = ("local", "groq", "openrouter", "openai")
_LOCAL_MODEL_PRESETS = ("tiny", "base", "small", "medium", "large-v3", "turbo")
_OPENAI_MODEL_PRESETS = ("gpt-transcribe", "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "whisper-1")
_GROQ_MODEL_FALLBACK = ("whisper-large-v3-turbo", "whisper-large-v3", "distil-whisper-large-v3-en")
_OPENROUTER_MODEL_FALLBACK = ("openai/whisper-large-v3-turbo", "openai/whisper-large-v3", "openai/whisper-1")
_MODEL_CACHE_TTL_SECONDS = 24 * 3600


class DaemonAlreadyRunning(RuntimeError):
    """A Tauri sidecar found the daemon left alive by a restarted UI shell."""


def _existing_daemon_exit_code(platform_name: str, ui_token: str | None) -> int:
    """Return the sidecar adoption sentinel only for authenticated Windows UI launches."""
    return 75 if platform_name == "nt" and ui_token else 1


def _is_daemon_status_response(response: Any) -> bool:
    return (
        isinstance(response, dict)
        and response.get("ok") == "true"
        and response.get("state") in {state.value for state in State}
    )


def default_socket_path() -> Path:
    if os.name == "nt":
        from .platforms.windows.paths import socket_path

        return socket_path()
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    return Path(runtime) / "transcriber.sock" if runtime else Path(f"/tmp/transcriber-{os.getuid()}.sock")


def default_data_dir() -> Path:
    if os.name == "nt":
        from .platforms.windows.paths import data_dir

        return data_dir()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "transcriber"


class TranscriberDaemon:
    def __init__(self, config: Config, data_dir: Path | None = None, config_path: Path | None = None) -> None:
        self.config = config
        self.recorder = make_recorder(
            config.audio,
            streaming_enabled=config.streaming.enabled,
            frame_ms=config.streaming.frame_ms,
            max_queue_seconds=config.streaming.max_queue_seconds,
            rolling_seconds=config.streaming.window_seconds,
        )
        self._state = State.IDLE
        self._lock = threading.RLock()
        self._event_condition = threading.Condition(self._lock)
        # A final decode must not race a live rolling decode against the same
        # CTranslate2 model instance.
        self._inference_lock = threading.Lock()
        self._timer: threading.Timer | None = None
        self._generation = 0
        self._error: str | None = None
        self._soft_error = False
        self._recording_started: float | None = None
        self._processing_started: float | None = None
        self._last_transcript = ""
        self._last_backend: str | None = None
        self._last_duration: float | None = None
        self._data_dir = data_dir or default_data_dir()
        self._config_path = config_path
        # Live session: partial/committed/final events with session ID +
        # revision. Partials are panel-only; delivery uses final text only.
        self._session: LiveSession | None = None
        self._live_stop = threading.Event()
        self._live_threads: list[threading.Thread] = []
        self._live_engine: LiveEngine | None = None
        self._hosted_live_session = False
        self._hosted_final_text: str | None = None
        self._hosted_live_error: str | None = None
        self._hosted_final_event = threading.Event()
        self._vad_endpoint = SpeechEndpoint(config.streaming.silence_timeout_seconds)
        self._live_decode_ms = 0.0
        self._live_decode_lag_ms = 0.0
        self._live_decode_error = ""
        self._load_persisted_last()

    @property
    def state(self) -> State:
        with self._lock:
            return self._state

    @property
    def last_audio_path(self) -> Path:
        return self._data_dir / "last.wav"

    # -- public protocol -------------------------------------------------

    def command(self, command: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        if command == "status":
            return self._ok()
        if command == "toggle":
            return self._toggle()
        if command == "cancel":
            return self._cancel()
        if command in {"retry", "retranscribe"}:
            return self._retry()
        if command == "last":
            return self._get_last()
        if command == "mics":
            return self._get_mics()
        if command == "models":
            return self._get_models()
        if command in {"set_backend", "set_provider"}:
            return self._set_backend(str(params.get("backend", params.get("provider", ""))))
        if command == "set_model":
            return self._set_model(str(params.get("model", "")))
        if command == "set_key":
            return self._set_key(params.get("provider"), params.get("key"), params.get("clear", False))
        if command == "set_live_engine":
            return self._set_live_engine(str(params.get("engine", "")), params.get("server_url"))
        if command == "set_live_model":
            return self._set_live_model(str(params.get("model", "")))
        if command in {"set_mic", "set_input"}:
            value = params.get("device", params.get("mic"))
            return self._set_mic(None if value is None else str(value))
        if command == "set_streaming":
            return self._set_streaming(params.get("enabled", None))
        if command == "events":
            since = params.get("since", 0)
            try:
                since_int = int(since)
            except (TypeError, ValueError):
                since_int = 0
            session_filter = params.get("session_id")
            return self._get_events(since_int, str(session_filter) if session_filter else None)
        return {**self._status_locked(), "ok": "false", "error": "Unknown command"}

    def _set_key(self, provider: Any, raw_key: Any, clear: Any = False) -> dict[str, Any]:
        """Persist one provider secret without returning or logging its contents."""
        if not isinstance(provider, str) or provider not in KEY_ENV:
            with self._lock:
                return {**self._status_locked(), "ok": "false", "error": "Choose a supported API-key provider"}
        env_name = KEY_ENV[provider]
        if clear is True:
            value = None
        elif isinstance(raw_key, str):
            value = raw_key.strip()
            if not value:
                with self._lock:
                    return {**self._status_locked(), "ok": "false", "error": "Enter a non-empty API key"}
            if len(value) > 4096 or any(character in value for character in "\r\n\0"):
                with self._lock:
                    return {**self._status_locked(), "ok": "false", "error": "The API key has an invalid format"}
        else:
            with self._lock:
                return {**self._status_locked(), "ok": "false", "error": "Enter a non-empty API key"}

        try:
            with self._lock:
                write_key(None, env_name, value)
                if value is None:
                    os.environ.pop(env_name, None)
                else:
                    os.environ[env_name] = value
                LOG.info("API key %s for %s", "removed" if value is None else "updated", provider)
                return self._ok_locked()
        except OSError:
            with self._lock:
                return {**self._status_locked(), "ok": "false", "error": "Could not save the API key"}

    def status_dict(self) -> dict[str, Any]:
        with self._lock:
            return self._status_locked()

    # -- commands ---------------------------------------------------------

    def _toggle(self) -> dict[str, Any]:
        with self._lock:
            if self._state in (State.IDLE, State.ERROR):
                try:
                    self.recorder.start()
                except Exception:
                    LOG.error("Audio capture could not start")
                    return self._fail_locked("Could not start recording; is rec installed?")
                self._generation += 1
                generation = self._generation
                self._timer = threading.Timer(
                    self.config.audio.max_duration_seconds, self._auto_stop, args=(generation,)
                )
                self._timer.daemon = True
                self._timer.start()
                self._error = None
                self._recording_started = time.monotonic()
                self._processing_started = None
                self._set_state(State.RECORDING)
                self._start_live_session_locked(generation)
                LOG.info("Recording started")
                return self._ok_locked()
            if self._state == State.RECORDING:
                return self._stop_locked()
            return {
                **self._status_locked(),
                "ok": "false",
                "error": "A recording is already being processed",
            }

    def _auto_stop(self, generation: int) -> None:
        with self._lock:
            if generation == self._generation and self._state == State.RECORDING:
                LOG.info("Maximum recording duration reached")
                self._stop_locked()

    def _stop_locked(self) -> dict[str, Any]:
        self._cancel_timer()
        try:
            audio = self.recorder.stop()
        except Exception:
            LOG.error("Audio capture could not stop")
            return self._fail_locked("Could not stop recording")
        # Let queued PCM reach the live engine before enqueueing its final
        # commit and shutdown marker.
        self._stop_live_threads()
        self._error = None
        self._recording_started = None
        self._processing_started = time.monotonic()
        self._set_state(State.PROCESSING)
        LOG.info(
            "Recording stopped; processing with backend=%s model=%s",
            self.config.transcription.backend,
            self.config.transcription.model,
        )
        worker = threading.Thread(
            target=self._process_and_deliver, args=(audio,), name="transcriber-worker", daemon=True
        )
        worker.start()
        return self._ok_locked()

    def _cancel(self) -> dict[str, Any]:
        with self._lock:
            if self._state == State.RECORDING:
                self._cancel_timer()
                self._stop_live_threads()
                try:
                    self.recorder.cancel()
                except Exception:
                    LOG.error("Audio capture could not be cancelled")
                self._generation += 1
                self._recording_started = None
                self._processing_started = None
                self._error = None
                self._session = None
                self._set_state(State.IDLE)
                LOG.info("Recording cancelled")
                return self._ok_locked()
            if self._state == State.ERROR:
                self._error = None
                self._set_state(State.IDLE)
                return self._ok_locked()
            return {**self._status_locked(), "ok": "false", "error": "No recording to cancel"}

    def _retry(self) -> dict[str, Any]:
        with self._lock:
            if self._state in (State.RECORDING, State.PROCESSING, State.DELIVERING):
                return {**self._status_locked(), "ok": "false", "error": "A recording is already in progress"}
            preserved = self.last_audio_path
            if not preserved.exists() or preserved.stat().st_size == 0:
                return {**self._status_locked(), "ok": "false", "error": "No previous recording to retry"}
            self._hosted_live_session = False
            self._error = None
            self._processing_started = time.monotonic()
            self._set_state(State.PROCESSING)
            worker = threading.Thread(
                target=self._process_and_deliver,
                args=(preserved,),
                kwargs={"is_preserved": True},
                name="transcriber-retry",
                daemon=True,
            )
            worker.start()
            return self._ok_locked()

    def _get_last(self) -> dict[str, Any]:
        with self._lock:
            if not self._last_transcript:
                # Recover from disk across restarts when only audio/meta survived.
                self._load_persisted_last()
            if not self._last_transcript:
                return {**self._status_locked(), "ok": "false", "error": "No previous transcript"}
            return {
                **self._status_locked(),
                "ok": "true",
                "transcript": self._last_transcript,
                "last_backend": self._last_backend or "",
                "last_duration": self._last_duration or 0,
            }

    def _get_mics(self) -> dict[str, Any]:
        with self._lock:
            status = self._status_locked()
        try:
            sources = list_sources()
        except Exception:
            LOG.error("Mic enumeration failed")
            sources = []
        return {
            **status,
            "ok": "true",
            "mics": [{"id": s.id, "name": s.name, "default": s.default} for s in sources],
        }

    def _get_models(self) -> dict[str, Any]:
        with self._lock:
            status = self._status_locked()
            backend = self.config.transcription.backend
            current = self.config.transcription.model
        if backend == "local":
            models = list(_LOCAL_MODEL_PRESETS)
            cached = False
        elif backend == "openai":
            models = list(_OPENAI_MODEL_PRESETS)
            cached = False
        elif backend == "groq":
            models, cached = _remote_models(
                self._data_dir, backend, _fetch_groq_models, _GROQ_MODEL_FALLBACK,
            )
        else:
            models, cached = _remote_models(
                self._data_dir, backend, _fetch_openrouter_models, _OPENROUTER_MODEL_FALLBACK,
            )
        return {
            **status,
            "ok": "true",
            "models": [{"id": m, "active": m == current} for m in models],
            "cached": cached,
        }

    def _set_backend(self, backend: str) -> dict[str, Any]:
        backend = backend.strip().lower()
        with self._lock:
            if backend not in _VALID_BACKENDS:
                return {
                    **self._status_locked(),
                    "ok": "false",
                    "error": "Provider must be local, groq, openrouter, or openai",
                }
            if self._state in (State.RECORDING, State.PROCESSING, State.DELIVERING):
                return {**self._status_locked(), "ok": "false", "error": "Cannot switch backend mid-run"}
            if backend != self.config.transcription.backend:
                old = self.config.transcription
                model = old.model
                if backend == "local" and model not in _LOCAL_MODEL_PRESETS:
                    model = "base"
                elif backend == "openai" and model not in _OPENAI_MODEL_PRESETS:
                    model = "gpt-transcribe"
                elif backend in {"groq", "openrouter"} and (model in _LOCAL_MODEL_PRESETS or model in _OPENAI_MODEL_PRESETS):
                    model = _GROQ_MODEL_FALLBACK[0] if backend == "groq" else _OPENROUTER_MODEL_FALLBACK[0]

                if self.config.streaming.engine == "hosted-whisper":
                    # The live transport is independent from the batch
                    # provider, so changing batch provider must preserve it.
                    streaming = self.config.streaming
                elif backend == "local":
                    streaming = dataclasses.replace(self.config.streaming, engine="local", model=model)
                elif backend == "openai":
                    streaming = dataclasses.replace(self.config.streaming, engine="openai-realtime", model="gpt-live-transcribe")
                else:
                    streaming = self.config.streaming if self.config.streaming.engine == "hosted-whisper" else dataclasses.replace(self.config.streaming, enabled=False)
                new_transcription = dataclasses.replace(old, backend=backend, model=model)
                recreate_recorder = streaming.enabled != self.config.streaming.enabled
                self.config = dataclasses.replace(self.config, transcription=new_transcription, streaming=streaming)
                if recreate_recorder:
                    self.recorder = make_recorder(
                        self.config.audio,
                        streaming_enabled=streaming.enabled,
                        frame_ms=streaming.frame_ms,
                        max_queue_seconds=streaming.max_queue_seconds,
                        rolling_seconds=streaming.window_seconds,
                    )
                self._persist_config_locked()
                LOG.info("Provider switched to %s", backend)
                switched = True
            else:
                switched = False
            result = self._ok_locked()
        if switched:
            self._warm_model_async()
        return result

    def _persist_config_locked(self) -> None:
        if self._config_path is None:
            return
        try:
            save_config(self.config, self._config_path)
        except OSError:
            LOG.error("Could not persist configuration")

    def _set_model(self, model: str) -> dict[str, Any]:
        model = model.strip()
        with self._lock:
            if not model or len(model) > 120:
                return {
                    **self._status_locked(),
                    "ok": "false",
                    "error": "Model must be a non-empty name",
                }
            if self._state in (State.RECORDING, State.PROCESSING, State.DELIVERING):
                return {**self._status_locked(), "ok": "false", "error": "Cannot switch model mid-run"}
            if model != self.config.transcription.model:
                new_transcription = dataclasses.replace(self.config.transcription, model=model)
                self.config = dataclasses.replace(self.config, transcription=new_transcription)
                self._persist_config_locked()
                LOG.info("Model switched to %s", model)
                switched = True
            else:
                switched = False
            result = self._ok_locked()
        if switched:
            self._warm_model_async()
        return result

    def _set_mic(self, device: str | None) -> dict[str, Any]:
        normalized = (device or "").strip()
        if normalized.lower() in {"", "default", "none"}:
            normalized = None
        with self._lock:
            if self._state in (State.RECORDING, State.PROCESSING, State.DELIVERING):
                return {**self._status_locked(), "ok": "false", "error": "Cannot switch mic mid-run"}
            current = self.config.audio.input_device
            if normalized != current:
                new_audio = dataclasses.replace(self.config.audio, input_device=normalized)
                self.config = dataclasses.replace(self.config, audio=new_audio)
                self.recorder.config = self.config.audio
                self._persist_config_locked()
                LOG.info("Input device set to %s", normalized or "default")
            return self._ok_locked()

    def _set_streaming(self, raw: Any) -> dict[str, Any]:
        """Flip or set live transcription; takes effect on the next recording."""
        if raw is None or (isinstance(raw, str) and raw.strip().lower() in {"", "toggle", "flip"}):
            enabled: bool | None = None  # flip current
        elif isinstance(raw, bool):
            enabled = raw
        elif isinstance(raw, (int, float)) and not isinstance(raw, bool):
            enabled = bool(raw)
        elif isinstance(raw, str):
            normalized = raw.strip().lower()
            if normalized in {"1", "true", "on", "yes", "enable", "enabled"}:
                enabled = True
            elif normalized in {"0", "false", "off", "no", "disable", "disabled"}:
                enabled = False
            else:
                with self._lock:
                    return {**self._status_locked(), "ok": "false", "error": "enabled must be true or false"}
        else:
            with self._lock:
                return {**self._status_locked(), "ok": "false", "error": "enabled must be true or false"}
        with self._lock:
            if self._state in (State.RECORDING, State.PROCESSING, State.DELIVERING):
                return {**self._status_locked(), "ok": "false", "error": "Cannot switch streaming mid-run"}
            target = (not self.config.streaming.enabled) if enabled is None else enabled
            if target and self.config.transcription.backend in {"groq", "openrouter"} \
                    and self.config.streaming.engine != "hosted-whisper":
                provider = "Groq" if self.config.transcription.backend == "groq" else "OpenRouter"
                return {
                    **self._status_locked(),
                    "ok": "false",
                    "error": f"{provider} supports batch transcription only. Select Local or OpenAI for live transcription.",
                }
            if target != self.config.streaming.enabled:
                new_streaming = dataclasses.replace(self.config.streaming, enabled=target)
                self.config = dataclasses.replace(self.config, streaming=new_streaming)
                # Rebuild capture so the next recording uses the right backend:
                # streaming PCM frames vs. direct-to-WAV.
                self.recorder = make_recorder(
                    self.config.audio,
                    streaming_enabled=target,
                    frame_ms=self.config.streaming.frame_ms,
                    max_queue_seconds=self.config.streaming.max_queue_seconds,
                    rolling_seconds=self.config.streaming.window_seconds,
                )
                self._persist_config_locked()
                LOG.info("Streaming %s", "enabled" if target else "disabled")
            return self._ok_locked()

    def _set_live_engine(self, engine: str, server_url: Any = None) -> dict[str, Any]:
        engine = engine.strip().lower()
        if engine not in {"local", "openai-realtime", "hosted-whisper"}:
            with self._lock:
                return {**self._status_locked(), "ok": "false", "error": "Live engine must be local, openai-realtime, or hosted-whisper"}
        with self._lock:
            if self._state in (State.RECORDING, State.PROCESSING, State.DELIVERING):
                return {**self._status_locked(), "ok": "false", "error": "Cannot switch live engine mid-run"}
            old = self.config.streaming
            model = old.model
            endpoint = old.server_url if server_url is None else str(server_url).strip()
            if engine == "hosted-whisper" and engine != old.engine:
                model = "default"
            elif engine != old.engine and ((engine == "local" and model not in _LOCAL_MODEL_PRESETS)
                                         or (engine == "openai-realtime" and model not in {"gpt-live-transcribe", "gpt-transcribe"})
                                         or (engine == "hosted-whisper" and not model.strip())):
                model = "base" if engine == "local" else "gpt-live-transcribe"
            try:
                new_streaming = dataclasses.replace(old, engine=engine, model=model, server_url=endpoint)
                # Reuse config validation for endpoint safety before persisting.
                from .config import _parse
                _parse({"streaming": dataclasses.asdict(new_streaming)})
            except (TypeError, ValueError) as exc:
                return {**self._status_locked(), "ok": "false", "error": str(exc)}
            self.config = dataclasses.replace(self.config, streaming=new_streaming)
            self._persist_config_locked()
            result = self._ok_locked()
        if engine == "local":
            self._warm_model_async()
        return result

    def _set_live_model(self, model: str) -> dict[str, Any]:
        model = model.strip()
        with self._lock:
            engine = self.config.streaming.engine
            allowed = _LOCAL_MODEL_PRESETS if engine == "local" else ("gpt-live-transcribe", "gpt-transcribe") if engine == "openai-realtime" else None
            if allowed is not None and model not in allowed:
                return {**self._status_locked(), "ok": "false", "error": f"Model must be one of: {', '.join(allowed)}"}
            if engine == "hosted-whisper" and (not model or len(model) > 120 or any(ch.isspace() for ch in model)):
                return {**self._status_locked(), "ok": "false", "error": "Hosted Whisper profile must be a non-empty profile ID without spaces"}
            if self._state in (State.RECORDING, State.PROCESSING, State.DELIVERING):
                return {**self._status_locked(), "ok": "false", "error": "Cannot switch live model mid-run"}
            changed = model != self.config.streaming.model
            if changed:
                self.config = dataclasses.replace(self.config, streaming=dataclasses.replace(self.config.streaming, model=model))
                self._persist_config_locked()
            result = self._ok_locked()
        if changed and self.config.streaming.engine == "local":
            self._warm_model_async()
        return result

    # -- live sessions ----------------------------------------------------

    def _warm_model_async(self) -> None:
        """Preload the local model once so first use isn't pay-per-recording."""
        transcription = self.config.transcription
        streaming = self.config.streaming
        if not streaming.preload_model:
            return
        if transcription.local_engine != "faster-whisper":
            return
        models: set[str] = set()
        if transcription.backend == "local" and not (streaming.enabled and streaming.engine == "hosted-whisper"):
            models.add(transcription.model)
        if streaming.engine == "local":
            models.add(streaming.model)
        if not models:
            return

        def _warm() -> None:
            for model in models:
                if preload_local_model(model, transcription.device, transcription.compute_type):
                    LOG.info("Local model preloaded (%s)", model)
                else:
                    LOG.warning("Local model preload skipped (%s unavailable)", model)

        worker = threading.Thread(target=_warm, name="transcriber-preload", daemon=True)
        worker.start()

    def _recorder_is_streaming(self) -> bool:
        return self.config.streaming.enabled and hasattr(self.recorder, "read_frame")

    def _live_capability_locked(self) -> tuple[bool, str]:
        """Whether this configuration can emit rolling partial hypotheses."""
        if not self.config.streaming.enabled:
            return False, "Live transcription is disabled"
        if not hasattr(self.recorder, "read_frame"):
            return False, "The selected recorder does not support streaming audio"
        if self.config.streaming.engine == "hosted-whisper":
            if not self.config.streaming.server_url:
                return False, "Hosted Whisper URL is not configured"
            if not os.environ.get("WHISPER_SERVICE_TOKEN"):
                return False, "WHISPER_SERVICE_TOKEN is not configured"
            from .hosted_whisper import hosted_dependency_available
            if not hosted_dependency_available():
                return False, "Install universal-transcriber[realtime] for hosted Whisper"
        elif self.config.transcription.backend in {"groq", "openrouter"}:
            provider = "Groq" if self.config.transcription.backend == "groq" else "OpenRouter"
            return False, f"{provider} supports batch transcription only. Select Local or OpenAI for live transcription."
        elif self.config.streaming.engine == "local":
            if self.config.transcription.local_engine != "faster-whisper":
                return False, "Live partials require the faster-whisper engine"
        elif self.config.streaming.engine == "openai-realtime":
            if not os.environ.get("OPENAI_API_KEY"):
                return False, "OPENAI_API_KEY is not configured"
            from .openai_realtime import realtime_dependency_available
            if not realtime_dependency_available():
                return False, "Install universal-transcriber[realtime] for OpenAI Realtime"
        else:
            return False, "Unknown live transcription engine"
        return True, ""

    def _start_live_session_locked(self, generation: int) -> None:
        self._session = LiveSession()
        self._vad_endpoint = SpeechEndpoint(self.config.streaming.silence_timeout_seconds)
        self._live_decode_ms = 0.0
        self._live_decode_lag_ms = 0.0
        self._live_decode_error = ""
        self._live_stop.clear()
        self._live_threads = []
        self._live_engine = None
        self._hosted_live_session = False
        self._hosted_final_text = None
        self._hosted_live_error = None
        self._hosted_final_event.clear()
        if not self._recorder_is_streaming():
            return
        capable, reason = self._live_capability_locked()
        if not capable:
            LOG.info("Live partials unavailable: %s", reason)
            return
        session_id = self._session.session_id
        callbacks = LocalEngineCallbacks(
            is_active=self._live_session_active,
            has_speech=lambda: self._live_has_speech(session_id),
            window_bytes=self._live_window_bytes,
            create_backend=lambda: make_backend(dataclasses.replace(
                self.config.transcription, backend="local", model=self.config.streaming.model,
            )),
            decode=self._decode_live_samples,
            publish_partial=self._publish_live_partial,
            report_metrics=self._report_live_metrics,
        )
        remote_callbacks = RemoteLiveCallbacks(
            publish_partial=self._publish_live_partial,
            publish_final=self._publish_hosted_final,
            report_metrics=self._report_live_metrics,
        )
        try:
            self._live_engine = make_live_engine(self.config.transcription, self.config.streaming, callbacks, remote_callbacks)
        except LiveEngineUnavailable as exc:
            LOG.info("Live partials unavailable: %s", exc)
            return
        self._hosted_live_session = self.config.streaming.engine == "hosted-whisper"
        vad_thread = threading.Thread(
            target=self._live_vad_loop, args=(generation, session_id),
            name="transcriber-vad", daemon=True,
        )
        decode_thread = threading.Thread(
            target=self._run_live_engine, args=(generation, session_id),
            name="transcriber-live-decode", daemon=True,
        )
        self._live_threads = [vad_thread, decode_thread]
        for thread in self._live_threads:
            thread.start()

    def _stop_live_threads(self) -> None:
        # Signal exit and reap in the background: joining here would risk
        # holding the daemon lock while a mid-inference decode thread waits
        # for it. Stale threads are harmless — they check the generation and
        # session ID before touching any shared state.
        self._live_stop.set()
        if self._live_engine is not None:
            try:
                self._live_engine.close()
            except Exception:
                LOG.debug("Live engine close failed", exc_info=True)
        stale, self._live_threads = self._live_threads, []
        current = threading.current_thread()
        targets = [t for t in stale if t is not current and t.is_alive()]
        if not targets:
            return

        def _reap() -> None:
            for target in targets:
                target.join(timeout=5)

        threading.Thread(target=_reap, name="transcriber-reap", daemon=True).start()

    def _push_live_event(self, session_id: str, kind: str, text: str = "", generation: int | None = None) -> None:
        with self._lock:
            if self._session is None or self._session.session_id != session_id:
                return
            # A decode may complete after Stop was pressed. It must never
            # replace the final event or revive an old recording's text.
            if kind in {"partial", "committed"} and (
                self._live_stop.is_set()
                or self._state != State.RECORDING
                or (generation is not None and generation != self._generation)
            ):
                return
            if kind == "speech_started":
                self._session.note_speech(True)
            elif kind == "speech_ended":
                self._session.note_speech(False)
            elif kind == "final":
                self._session.finalize(text)
            else:
                self._session.push_hypothesis(text)
            self._event_condition.notify_all()

    def _live_vad_loop(self, generation: int, session_id: str) -> None:
        vad = create_vad(self.config.streaming.vad_sensitivity)
        recorder = self.recorder
        while not self._live_stop.is_set():
            with self._lock:
                if generation != self._generation or self._state != State.RECORDING:
                    return
            frame = None
            try:
                read = getattr(recorder, "read_frame", None)
                frame = read(timeout=0.2) if callable(read) else None
            except Exception:
                LOG.debug("Live frame read failed", exc_info=True)
                time.sleep(0.2)
                continue
            now = time.monotonic()
            if frame is None:
                with self._lock:
                    if self._vad_endpoint.should_auto_stop(now) and self._state == State.RECORDING:
                        LOG.info("VAD auto-stop after trailing silence")
                        self._stop_locked()
                        return
                continue
            try:
                speech = bool(vad.is_speech(bytes(frame)))
            except Exception:
                continue
            engine = self._live_engine
            if engine is not None:
                try:
                    engine.submit_audio(bytes(frame))
                except Exception:
                    LOG.debug("Live audio submit failed", exc_info=True)
            transition = self._vad_endpoint.update(speech, now)
            if transition in ("speech_started", "speech_ended"):
                self._push_live_event(session_id, transition)
                if transition == "speech_ended" and engine is not None:
                    try:
                        engine.commit_turn()
                    except Exception:
                        LOG.debug("Live turn commit failed", exc_info=True)
            with self._lock:
                auto = self._vad_endpoint.should_auto_stop(now)
                active = self._state == State.RECORDING and generation == self._generation
            if auto and active:
                LOG.info("VAD auto-stop after trailing silence")
                with self._lock:
                    if generation == self._generation and self._state == State.RECORDING:
                        self._stop_locked()
                return

    def _run_live_engine(self, generation: int, session_id: str) -> None:
        engine = self._live_engine
        if engine is None:
            return
        try:
            engine.run(self._live_stop, generation, session_id)
        except Exception:
            LOG.exception("Live transcription engine failed")
            self._report_live_metrics(0.0, 0.0, "Live engine failed")

    def _live_session_active(self, generation: int, session_id: str) -> bool:
        with self._lock:
            return (
                not self._live_stop.is_set()
                and generation == self._generation
                and self._state == State.RECORDING
                and self._session is not None
                and self._session.session_id == session_id
            )

    def _live_has_speech(self, session_id: str) -> bool:
        with self._lock:
            return self._session is not None and self._session.session_id == session_id and self._vad_endpoint.had_speech

    def _live_window_bytes(self, seconds: int) -> bytes | None:
        try:
            getter = getattr(self.recorder, "window_bytes", None)
            return getter(seconds) if callable(getter) else None
        except Exception:
            LOG.debug("Live window read failed", exc_info=True)
            return None

    def _decode_live_samples(self, backend: Any, samples: Any) -> str:
        with self._inference_lock:
            return backend.transcribe_samples(samples)

    def _publish_live_partial(self, session_id: str, text: str, generation: int) -> None:
        self._push_live_event(session_id, "partial", text, generation)

    def _publish_hosted_final(self, session_id: str, text: str, generation: int) -> None:
        with self._lock:
            if self._session is None or self._session.session_id != session_id:
                return
            self._hosted_final_text = text
            self._hosted_final_event.set()
        self._push_live_event(session_id, "final", text, generation)

    def _report_live_metrics(self, elapsed: float, lag: float, error: str) -> None:
        with self._lock:
            self._live_decode_ms = elapsed * 1000
            self._live_decode_lag_ms = lag * 1000
            self._live_decode_error = error
            if self._hosted_live_session and error and self._hosted_final_text is None:
                self._hosted_live_error = error
                self._hosted_final_event.set()

    def _finalize_live(self, text: str) -> None:
        with self._lock:
            if self._session is not None:
                self._session.finalize(text)
                self._event_condition.notify_all()

    def _get_events(self, since: int, session_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            status = self._status_locked()
            events, revision = self._events_since_locked(since, session_id)
        return {
            **status,
            "ok": "true",
            "events": events,
            "revision": revision,
            "session_id": (self._session.session_id if self._session else ""),
        }

    def events_since(self, since: int, session_id: str | None = None) -> tuple[list[dict[str, Any]], int]:
        with self._lock:
            return self._events_since_locked(since, session_id)

    def wait_events_since(self, since: int, session_id: str | None = None,
                          timeout: float = 1.0) -> tuple[list[dict[str, Any]], int]:
        """Wait for a live event instead of polling the daemon on a timer."""
        with self._event_condition:
            events, revision = self._events_since_locked(since, session_id)
            if not events:
                self._event_condition.wait(timeout=max(0.0, timeout))
                events, revision = self._events_since_locked(since, session_id)
            return events, revision

    def _events_since_locked(self, since: int, session_id: str | None = None) -> tuple[list[dict[str, Any]], int]:
        if self._session is None:
            return [], since
        if session_id and session_id != self._session.session_id:
            return [], self._session.revision
        out = [
            {"type": e.type, "session_id": e.session_id, "revision": e.revision, "text": e.text, "at": e.at}
            for e in self._session.events
            if e.revision > since
        ]
        return out, self._session.revision

    # -- pipeline ----------------------------------------------------------

    def _process_and_deliver(self, audio: Path, is_preserved: bool = False) -> None:
        started = time.monotonic()
        backend_name = self.config.transcription.backend
        try:
            if not is_preserved:
                self._preserve_audio(audio)
            try:
                with self._lock:
                    use_hosted_final = self._hosted_live_session and not is_preserved
                if use_hosted_final:
                    if not self._hosted_final_event.wait(timeout=180.0):
                        raise TranscriptionError("Hosted Whisper did not return a final transcript in time")
                    with self._lock:
                        transcript = self._hosted_final_text
                        hosted_error = self._hosted_live_error
                    if transcript is None:
                        raise TranscriptionError(hosted_error or "Hosted Whisper did not return a final transcript")
                    backend_name = "hosted-whisper"
                else:
                    with self._inference_lock:
                        transcript = make_backend(self.config.transcription).transcribe(audio)
            except Exception as exc:
                LOG.error("Transcription failed: %s", _safe_error(exc))
                self._fail(f"Transcription failed ({_short_reason(exc)})")
                return
            try:
                text = process_transcript(transcript, self.config.processing)
            except Exception:
                LOG.error("Text processing failed")
                self._fail("Text processing failed")
                return
            if not text or not text.strip():
                LOG.info("Transcription returned no text")
                self._fail("No speech detected", soft=True)
                return
            duration = time.monotonic() - started
            with self._lock:
                self._last_transcript = text
                self._last_backend = backend_name
                self._last_duration = duration
                self._persist_last_locked()
                self._set_state(State.DELIVERING)
            # The live session reconciles to the final transcript; only this
            # finalized text is delivered to the focused app.
            if not use_hosted_final:
                self._finalize_live(text)
            try:
                delivery = make_delivery(self.config.delivery)
                delivery.insert(text)
                if self.config.delivery.submit_after_insert:
                    delivery.submit()
                LOG.info("Transcript delivered via backend=%s in %.1fs", backend_name, duration)
            except Exception as exc:
                LOG.error("Delivery failed: %s", _safe_error(exc))
                self._fail(f"Delivery failed ({_short_reason(exc)}); transcript kept for copy")
                return
        except Exception:
            LOG.error("Unexpected pipeline failure")
            self._fail("Unexpected failure")
        finally:
            if not is_preserved:
                audio.unlink(missing_ok=True)
            with self._lock:
                if self._state != State.ERROR:
                    self._processing_started = None
                    self._set_state(State.IDLE)

    # -- status helpers -----------------------------------------------------

    def _ok(self) -> dict[str, Any]:
        with self._lock:
            return self._ok_locked()

    def _ok_locked(self) -> dict[str, Any]:
        return {**self._status_locked(), "ok": "true"}

    def _fail(self, reason: str, soft: bool = False) -> dict[str, Any]:
        with self._lock:
            return self._fail_locked(reason, soft)

    def _fail_locked(self, reason: str, soft: bool = False) -> dict[str, Any]:
        self._error = reason
        self._soft_error = soft
        self._recording_started = None
        self._processing_started = None
        self._set_state(State.ERROR)
        return {**self._status_locked(), "ok": "false", "error": reason}

    def _status_locked(self) -> dict[str, Any]:
        now = time.monotonic()
        recording_elapsed = (
            now - self._recording_started if self._state == State.RECORDING and self._recording_started else 0
        )
        processing_elapsed = (
            now - self._processing_started
            if self._state in (State.PROCESSING, State.DELIVERING) and self._processing_started
            else 0
        )
        preview = ""
        if self.config.ui.show_last_transcript and self._last_transcript:
            preview = self._last_transcript[: self.config.ui.preview_chars]
        session = self._session
        live_available, live_unavailable_reason = self._live_capability_locked()
        live_active = self._state in (State.RECORDING, State.PROCESSING, State.DELIVERING) and session is not None
        show_partials = self.config.streaming.show_partials
        committed = session.committed_text if live_active and session else ""
        provisional = session.provisional_text if live_active and session else ""
        live_text = session.live_text if live_active and session else ""
        if not show_partials:
            provisional = ""
            live_text = committed
        return {
            "state": self._state.value,
            "error": self._error or "",
            "soft_error": self._soft_error,
            "backend": self.config.transcription.backend,
            "local_engine": self.config.transcription.local_engine,
            "model": self.config.transcription.model,
            "mic": self.config.audio.input_device or "default",
            "recording_elapsed": round(recording_elapsed, 1),
            "processing_elapsed": round(processing_elapsed, 1),
            "last_audio": bool(self.last_audio_path.exists()),
            "last_backend": self._last_backend or "",
            "last_duration": round(self._last_duration, 1) if self._last_duration is not None else 0,
            "last_preview": preview,
            "has_groq_key": bool(os.environ.get("GROQ_API_KEY")),
            "has_openrouter_key": bool(os.environ.get("OPENROUTER_API_KEY")),
            "has_openai_key": bool(os.environ.get("OPENAI_API_KEY")),
            "has_whisper_service_token": bool(os.environ.get("WHISPER_SERVICE_TOKEN")),
            "version": __version__,
            "session_id": session.session_id if session else "",
            "live_revision": session.revision if live_active and session else 0,
            "committed_text": committed,
            "provisional_text": provisional,
            "live_text": live_text,
            "speech_active": bool(session.speech_active) if live_active and session else False,
            "streaming": self.config.streaming.enabled,
            "live_engine": self.config.streaming.engine,
            "live_model": self.config.streaming.model,
            # Endpoint configuration isn't secret; expose it so desktop
            # settings UIs can edit a hosted service without reading YAML.
            "live_server_url": self.config.streaming.server_url,
            "live_available": live_available,
            "live_unavailable_reason": live_unavailable_reason,
            "live_decode_ms": round(self._live_decode_ms, 1),
            "live_decode_lag_ms": round(self._live_decode_lag_ms, 1),
            "live_decode_error": self._live_decode_error,
        }

    # -- persistence ----------------------------------------------------------

    def _preserve_audio(self, src: Path) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, self.last_audio_path)
        except OSError:
            LOG.error("Could not preserve last recording")

    def _persist_last_locked(self) -> None:
        try:
            self._data_dir.mkdir(parents=True, exist_ok=True)
            (self._data_dir / "last.txt").write_text(self._last_transcript)
            (self._data_dir / "meta.json").write_text(
                json.dumps(
                    {"backend": self._last_backend, "duration": self._last_duration},
                )
            )
        except OSError:
            LOG.error("Could not persist last transcript")

    def _load_persisted_last(self) -> None:
        try:
            text_file = self._data_dir / "last.txt"
            meta_file = self._data_dir / "meta.json"
            if text_file.exists() and not self._last_transcript:
                self._last_transcript = text_file.read_text()[:4000]
            if meta_file.exists():
                meta = json.loads(meta_file.read_text())
                self._last_backend = self._last_backend or meta.get("backend")
                duration = meta.get("duration")
                if isinstance(duration, (int, float)):
                    self._last_duration = self._last_duration if self._last_duration is not None else float(duration)
        except (OSError, json.JSONDecodeError, ValueError):
            pass

    # -- internals --------------------------------------------------------------

    def _set_state(self, new_state: State) -> None:
        if self._state != new_state:
            LOG.info("State transition %s -> %s", self._state.value, new_state.value)
            self._state = new_state
            if new_state != State.ERROR:
                self._soft_error = False

    def _cancel_timer(self) -> None:
        if self._timer:
            self._timer.cancel()
            self._timer = None

    def shutdown(self) -> None:
        with self._lock:
            self._cancel_timer()
            self._live_stop.set()
            if self._state == State.RECORDING:
                try:
                    self.recorder.cancel()
                except Exception:
                    pass
            self._recording_started = None
            self._processing_started = None
            self._set_state(State.IDLE)
        self._stop_live_threads()


def _model_cache_path(data_dir: Path, backend: str) -> Path:
    return data_dir / f"models-{backend}.json"


def _read_model_cache(data_dir: Path, backend: str) -> tuple[list[str], bool]:
    """Return (models, fresh) from cache; fresh means within TTL."""
    try:
        payload = json.loads(_model_cache_path(data_dir, backend).read_text())
        models = payload.get("models", [])
        fetched_at = payload.get("fetched_at", 0)
    except (OSError, json.JSONDecodeError, ValueError):
        return [], False
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        return [], False
    fresh = isinstance(fetched_at, (int, float)) and (time.time() - fetched_at) < _MODEL_CACHE_TTL_SECONDS
    return models, fresh


def _write_model_cache(data_dir: Path, backend: str, models: list[str]) -> None:
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        _model_cache_path(data_dir, backend).write_text(json.dumps({"fetched_at": time.time(), "models": models}))
    except OSError:
        LOG.error("Could not cache model list")


def _remote_models(data_dir: Path, backend: str, fetch: Any, fallback: tuple[str, ...]) -> tuple[list[str], bool]:
    """Fetch provider model list with TTL cache; fall back gracefully offline."""
    cached, fresh = _read_model_cache(data_dir, backend)
    if fresh:
        return cached, True
    try:
        models = fetch()
    except Exception as exc:
        LOG.error("Model list fetch failed: %s", _safe_error(exc))
        models = []
    if not models:
        LOG.error("Model list unavailable; using fallback")
        return (cached, True) if cached else (list(fallback), False)
    _write_model_cache(data_dir, backend, models)
    return models, False


def _fetch_json(url: str, headers: dict[str, str] | None = None, timeout: int = 15) -> Any:
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def _fetch_groq_models() -> list[str]:
    key = os.environ.get("GROQ_API_KEY")
    if not key:
        raise TranscriptionError("GROQ_API_KEY is not set")
    payload = _fetch_json(
        "https://api.groq.com/openai/v1/models", {"Authorization": f"Bearer {key}"},
    )
    models = [
        str(item.get("id", ""))
        for item in payload.get("data", [])
        if isinstance(item, dict) and "whisper" in str(item.get("id", "")).lower()
    ]
    return sorted(set(models))


def _fetch_openrouter_models() -> list[str]:
    # Server-side filter: only transcription-capable models (verified:
    # output_modalities=transcription lists whisper/STT endpoints).
    payload = _fetch_json("https://openrouter.ai/api/v1/models?output_modalities=transcription")
    models = [
        str(item.get("id", ""))
        for item in payload.get("data", [])
        if isinstance(item, dict) and item.get("id")
    ]
    return sorted(set(models))


def _short_reason(exc: Exception) -> str:
    detail = _safe_error(exc).strip()
    if not detail:
        return type(exc).__name__
    return detail[:120]



def _safe_error(exc: Exception) -> str:
    """Keep diagnostics useful without risking API credentials in daemon logs."""
    message = str(exc).replace("\n", " ")[:600]
    return re.sub(
        r"(?i)\b(?:bearer\s+|api[_-]?key\s*[=:]\s*|authorization\s*[:=]\s*(?:bearer\s+)?)\S+",
        "[redacted]",
        message,
    )


class _RequestHandler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            request = json.loads(self.rfile.readline(8192))
            if not isinstance(request, dict):
                raise ValueError
            command = request.get("command")
            if not isinstance(command, str):
                raise ValueError
            if command == "subscribe":
                self._handle_subscribe(request)
                return
            if command == "shutdown":
                self.wfile.write(b'{"ok":"true","state":"STOPPING"}\n')
                _schedule_server_shutdown(self.server)
                return
            params = {key: value for key, value in request.items() if key != "command"}
            response = self.server.daemon.command(command, params)  # type: ignore[attr-defined]
        except (ValueError, json.JSONDecodeError):
            response = {"ok": "false", "error": "Invalid request"}
        try:
            self.wfile.write((json.dumps(response) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _handle_subscribe(self, request: dict[str, Any]) -> None:
        """Persistent event stream: one JSON object per line until disconnect."""
        daemon: TranscriberDaemon = self.server.daemon  # type: ignore[attr-defined]
        try:
            since = int(request.get("since", 0))
        except (TypeError, ValueError):
            since = 0
        session_filter = request.get("session_id")
        session_filter = str(session_filter) if session_filter else None
        hello = {"ok": "true", "subscribed": True, "since": since}
        try:
            self.wfile.write((json.dumps(hello) + "\n").encode())
        except (BrokenPipeError, ConnectionResetError):
            return
        last = since
        while True:
            try:
                events, revision = daemon.wait_events_since(last, session_filter, timeout=1.0)
            except Exception:
                return
            for event in events:
                try:
                    self.wfile.write((json.dumps(event) + "\n").encode())
                except (BrokenPipeError, ConnectionResetError):
                    return
                last = int(event.get("revision", last))
            _ = revision


class UnixServer(socketserver.ThreadingUnixStreamServer):
    daemon: TranscriberDaemon
    daemon_threads = True


def _schedule_server_shutdown(server: UnixServer) -> None:
    """Stop serving from another thread to avoid socketserver's deadlock."""
    threading.Thread(
        target=server.shutdown,
        name="transcriber-shutdown-request",
        daemon=True,
    ).start()


def run_server(
    config: Config,
    socket_path: Path,
    config_path: Path | None = None,
    ui_token: str | None = None,
) -> None:
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    if socket_path.exists():
        # Do not blindly delete a live daemon's IPC endpoint.
        if os.name != "nt" and not stat.S_ISSOCK(socket_path.stat().st_mode):
            raise RuntimeError(f"Refusing to replace non-socket path {socket_path}")
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(1.0)
            probe.connect(str(socket_path))
        except OSError:
            socket_path.unlink()
        else:
            try:
                probe.sendall(b'{"command":"status"}\n')
                response = json.loads(probe.makefile("rb").readline(8192))
            except (OSError, ValueError) as exc:
                raise RuntimeError(f"IPC endpoint is in use but did not return Whisper status: {socket_path}") from exc
            if not _is_daemon_status_response(response):
                raise RuntimeError(f"IPC endpoint is in use but did not return Whisper status: {socket_path}")
            message = f"Daemon already appears to be running at {socket_path}"
            if _existing_daemon_exit_code(os.name, ui_token) == 75:
                # Tauri may have restarted while its daemon child survived. The
                # UI uses the same per-user token and can reconnect to it.
                raise DaemonAlreadyRunning(message)
            raise RuntimeError(message)
        finally:
            probe.close()
    daemon = TranscriberDaemon(config, config_path=config_path)
    daemon._warm_model_async()  # preload + warm the local model for the daemon's lifetime
    server = UnixServer(str(socket_path), _RequestHandler)
    server.daemon = daemon
    if os.name != "nt":
        os.chmod(socket_path, 0o600)
    else:
        # Keep the Unix-socket command protocol unchanged; expose the same
        # daemon state/commands to the Windows WebView over loopback WebSocket.
        from .platforms.windows.websocket import start_server as start_ui_websocket

        start_ui_websocket(daemon, auth_token=ui_token)

    def stop_server(_signal: int, _frame: Any) -> None:
        # ``shutdown`` must run from another thread than ``serve_forever``;
        # calling it in this main-thread signal handler deadlocks Ctrl-C/SIGTERM.
        threading.Thread(target=server.shutdown, name="transcriber-shutdown", daemon=True).start()

    signal.signal(signal.SIGTERM, stop_server)
    signal.signal(signal.SIGINT, stop_server)
    LOG.info("Daemon listening on %s", socket_path)
    try:
        server.serve_forever()
    finally:
        daemon.shutdown()
        server.server_close()
        socket_path.unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Whisper transcription daemon")
    parser.add_argument("--config")
    parser.add_argument("--socket", type=Path, default=default_socket_path())
    parser.add_argument("--ui-token", help=argparse.SUPPRESS)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    _configure_logging(
        verbose=args.verbose,
        log_to_file=os.name == "nt" and bool(args.ui_token),
    )
    load_keys_env()  # keys.env fills missing env vars; values never logged
    try:
        config_path = resolve_config_path(args.config)
        run_server(load_config(config_path), args.socket, config_path, args.ui_token)
    except DaemonAlreadyRunning:
        raise SystemExit(75) from None
    except Exception as exc:
        # Configuration/socket errors are safe summaries; never print exception chains containing request data.
        LOG.error("Daemon could not start: %s", exc)
        raise SystemExit(1) from None


def _configure_logging(*, verbose: bool, log_to_file: bool = False) -> None:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    log_setup_error: OSError | None = None
    if log_to_file:
        log_path = default_data_dir() / "logs" / "daemon.log"
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            handlers.append(
                RotatingFileHandler(
                    log_path,
                    maxBytes=2 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
            )
        except OSError as exc:
            log_setup_error = exc
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
    )
    if log_setup_error is not None:
        LOG.warning("Could not open the packaged daemon log file: %s", log_setup_error)


if __name__ == "__main__":
    main()
