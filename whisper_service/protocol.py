"""Validation and serialization helpers for the v1 live WebSocket protocol."""

from __future__ import annotations

import json
import re
from typing import Any

from .sessions import ServiceError


_LANGUAGE_RE = re.compile(r"^[A-Za-z][A-Za-z-]{0,34}$")
_AUDIO_FORMAT = {"encoding": "pcm_s16le", "sample_rate_hz": 16000, "channels": 1}


def parse_session_start(raw: str) -> tuple[str, str | None]:
    try:
        message = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ServiceError("invalid_request", "Expected a JSON session.start message.") from exc
    if not isinstance(message, dict) or message.get("type") != "session.start":
        raise ServiceError("invalid_request", "The first message must be session.start.")
    profile = message.get("profile")
    if not isinstance(profile, str) or not profile or len(profile) > 80:
        raise ServiceError("invalid_profile", "A valid transcription profile is required.")
    audio = message.get("audio")
    if not isinstance(audio, dict) or any(audio.get(key) != value for key, value in _AUDIO_FORMAT.items()):
        raise ServiceError("unsupported_audio", "Audio must be 16 kHz mono PCM16.")
    language = message.get("language", "auto")
    if language == "auto":
        language = None
    elif not isinstance(language, str) or not _LANGUAGE_RE.fullmatch(language):
        raise ServiceError("invalid_language", "The language value is invalid.")
    return profile, language


def parse_control(raw: str) -> str:
    try:
        message: Any = json.loads(raw)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ServiceError("invalid_request", "Expected a JSON control message.") from exc
    if not isinstance(message, dict) or message.get("type") not in {"session.finish", "session.cancel"}:
        raise ServiceError("invalid_request", "Expected session.finish or session.cancel.")
    return message["type"]


def encode_event(event: dict[str, object]) -> str:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":"))
