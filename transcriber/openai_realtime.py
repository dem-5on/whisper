"""OpenAI Realtime API transcription engine.

This module handles only the live OpenAI WebSocket session. Batch
transcription providers remain implemented in ``transcription.py``.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
import json
import os
import queue
import threading
import time
from typing import Callable
from urllib.parse import quote


REALTIME_URL = "wss://api.openai.com/v1/realtime?model={model}"
_CLOSE = object()
_COMMIT = object()


@dataclass(frozen=True)
class OpenAIRealtimeCallbacks:
    publish_partial: Callable[[str, str, int], None]
    report_metrics: Callable[[float, float, str], None]


def realtime_dependency_available() -> bool:
    try:
        from websockets.sync.client import connect as _connect  # noqa: F401
    except ImportError:
        return False
    return True


def resample_pcm16_16k_to_24k(pcm: bytes) -> bytes:
    """Resample mono signed little-endian PCM16 from 16 kHz to 24 kHz."""
    if not pcm:
        return b""
    if len(pcm) % 2:
        pcm = pcm[:-1]
    try:
        import numpy as np
    except ImportError as exc:
        raise RuntimeError("NumPy is required for OpenAI Realtime audio resampling") from exc
    source = np.frombuffer(pcm, dtype="<i2").astype(np.float32)
    if source.size == 0:
        return b""
    output_size = source.size * 3 // 2
    positions = np.arange(output_size, dtype=np.float32) * (2.0 / 3.0)
    result = np.interp(positions, np.arange(source.size, dtype=np.float32), source)
    return np.clip(np.rint(result), -32768, 32767).astype("<i2").tobytes()


class OpenAIRealtimeEngine:
    """Push 16 kHz microphone frames and publish OpenAI transcript deltas."""

    def __init__(self, model: str, language: str | None, callbacks: OpenAIRealtimeCallbacks) -> None:
        self.model = model
        self.language = language
        self.callbacks = callbacks
        self._audio: queue.Queue[bytes | object] = queue.Queue(maxsize=500)
        self._sender_done = threading.Event()
        self._closing = threading.Event()
        self._lock = threading.Lock()
        self._socket = None
        self._send_error: str = ""

    def submit_audio(self, pcm_16k: bytes) -> None:
        if not pcm_16k or self._closing.is_set():
            return
        try:
            self._audio.put(bytes(pcm_16k), timeout=0.1)
        except queue.Full as exc:
            raise RuntimeError("OpenAI Realtime audio queue is behind; stopping to avoid silent audio loss") from exc

    def commit_turn(self) -> None:
        if self._closing.is_set():
            return
        try:
            self._audio.put(_COMMIT, timeout=0.1)
        except queue.Full as exc:
            raise RuntimeError("OpenAI Realtime audio queue is full") from exc

    def close(self) -> None:
        if self._closing.is_set():
            return
        self._closing.set()
        # Flush the final utterance before ending the sender. If a VAD turn
        # was already committed, an empty commit is harmlessly avoided by the
        # remote API error handler.
        try:
            self._audio.put(_COMMIT, timeout=0.1)
        except queue.Full:
            pass
        try:
            self._audio.put(_CLOSE, timeout=0.5)
        except queue.Full:
            self._send_error = "OpenAI Realtime audio queue could not close cleanly"

    def run(self, stop: threading.Event, generation: int, session_id: str) -> None:
        key = os.environ.get("OPENAI_API_KEY")
        if not key:
            self.callbacks.report_metrics(0.0, 0.0, "OPENAI_API_KEY is not configured")
            return
        try:
            from websockets.sync.client import connect
        except ImportError:
            self.callbacks.report_metrics(0.0, 0.0, "Install universal-transcriber[realtime]")
            return

        uri = REALTIME_URL.format(model=quote(self.model, safe="-_."))
        try:
            with connect(
                uri,
                additional_headers={"Authorization": f"Bearer {key}"},
                open_timeout=10,
                close_timeout=2,
                max_size=2**20,
            ) as websocket:
                with self._lock:
                    self._socket = websocket
                websocket.send(json.dumps(self._session_update()))
                if not self._await_session_ready(websocket):
                    return
                sender = threading.Thread(
                    target=self._send_audio, args=(websocket,), name="openai-realtime-audio", daemon=True,
                )
                sender.start()
                self._receive_events(websocket, sender, stop, generation, session_id)
                self._closing.set()
                try:
                    self._audio.put_nowait(_CLOSE)
                except queue.Full:
                    pass
                sender.join(timeout=2)
                with self._lock:
                    self._socket = None
        except Exception as exc:
            if not stop.is_set() or self._send_error:
                message = self._send_error or _safe_ws_error(exc)
                self.callbacks.report_metrics(0.0, 0.0, message)

    def _session_update(self) -> dict[str, object]:
        transcription: dict[str, object] = {"model": self.model, "delay": "low"}
        if self.language:
            transcription["languages"] = [self.language]
        return {
            "type": "session.update",
            "session": {
                "type": "transcription",
                "audio": {
                    "input": {
                        "format": {"type": "audio/pcm", "rate": 24000},
                        "transcription": transcription,
                        "turn_detection": None,
                    }
                },
            },
        }

    @staticmethod
    def _await_session_ready(websocket) -> bool:  # type: ignore[no-untyped-def]
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            message = websocket.recv(timeout=max(0.1, deadline - time.monotonic()))
            if not isinstance(message, str):
                continue
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "session.updated":
                return True
            if event.get("type") == "error":
                raise RuntimeError(_event_error(event))
        raise TimeoutError("OpenAI Realtime session configuration timed out")

    def _send_audio(self, websocket) -> None:  # type: ignore[no-untyped-def]
        try:
            while True:
                item = self._audio.get()
                if item is _CLOSE:
                    return
                if item is _COMMIT:
                    try:
                        websocket.send(json.dumps({"type": "input_audio_buffer.commit"}))
                    except Exception as exc:
                        # Empty turns can occur if Stop follows a VAD commit.
                        # OpenAI rejects these; continue unless transport died.
                        message = _safe_ws_error(exc)
                        if "empty" not in message.lower() and "audio" not in message.lower():
                            self._send_error = message
                            return
                    continue
                pcm24 = resample_pcm16_16k_to_24k(item)  # type: ignore[arg-type]
                if not pcm24:
                    continue
                websocket.send(json.dumps({
                    "type": "input_audio_buffer.append",
                    "audio": base64.b64encode(pcm24).decode("ascii"),
                }))
        except Exception as exc:
            self._send_error = _safe_ws_error(exc)
        finally:
            self._sender_done.set()

    def _receive_events(self, websocket, sender: threading.Thread, stop: threading.Event,
                        generation: int, session_id: str) -> None:  # type: ignore[no-untyped-def]
        turn_order: list[str] = []
        turn_text: dict[str, str] = {}
        started = time.monotonic()
        close_deadline: float | None = None
        while True:
            if stop.is_set() and close_deadline is None:
                self.close()
                close_deadline = time.monotonic() + 3.0
            if self._sender_done.is_set() and close_deadline is None:
                close_deadline = time.monotonic() + 2.0
            if close_deadline is not None and time.monotonic() >= close_deadline:
                return
            try:
                message = websocket.recv(timeout=0.2)
            except TimeoutError:
                continue
            if not isinstance(message, str):
                continue
            try:
                event = json.loads(message)
            except json.JSONDecodeError:
                continue
            kind = event.get("type")
            if kind == "conversation.item.input_audio_transcription.delta":
                item_id = str(event.get("item_id", "current"))
                if item_id not in turn_text:
                    turn_order.append(item_id)
                    turn_text[item_id] = ""
                turn_text[item_id] += str(event.get("delta", ""))
                self.callbacks.publish_partial(session_id, _join_turns(turn_order, turn_text), generation)
                self.callbacks.report_metrics(time.monotonic() - started, 0.0, "")
            elif kind == "conversation.item.input_audio_transcription.completed":
                item_id = str(event.get("item_id", "current"))
                if item_id not in turn_text:
                    turn_order.append(item_id)
                turn_text[item_id] = str(event.get("transcript", ""))
                self.callbacks.publish_partial(session_id, _join_turns(turn_order, turn_text), generation)
            elif kind == "error":
                self.callbacks.report_metrics(0.0, 0.0, _event_error(event))
            if self._send_error:
                self.callbacks.report_metrics(0.0, 0.0, self._send_error)
                return
            if sender.is_alive() is False and not stop.is_set() and self._sender_done.is_set():
                # Sender exits only on explicit close or transport failure.
                return


def _join_turns(order: list[str], text: dict[str, str]) -> str:
    return " ".join(text[item].strip() for item in order if text.get(item, "").strip())


def _event_error(event: dict[str, object]) -> str:
    detail = event.get("error")
    if isinstance(detail, dict):
        message = str(detail.get("message", "OpenAI Realtime request failed"))
        # Keep API errors useful without leaking request payloads or tokens.
        return message.replace("\n", " ")[:160]
    return "OpenAI Realtime request failed"


def _safe_ws_error(exc: Exception) -> str:
    message = str(exc).replace("\n", " ")
    if "OPENAI_API_KEY" in message or "Bearer " in message:
        return "OpenAI Realtime connection failed"
    return message[:160] or "OpenAI Realtime connection failed"
